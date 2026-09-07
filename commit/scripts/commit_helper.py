#!/usr/bin/env python3
"""commit_helper — owns every mutating git step for the commit skill, so the
model never has to chain `git add`, branch creation, and `git commit` by hand
(each chained command is a round-trip and a chance to get the order wrong).

Two subcommands:

  prepare [zh|en]
      Resolve the body language, make the working tree ready to describe,
      and read back a compact, token-lean snapshot.

      Language: an explicit `zh` or `en` wins and is recorded in this
      repo's .git/config, so later runs in the same repo default to it. With
      no argument the recorded value wins, falling back to `en`. The key is
      shared with the create-pr skill, so setting it once covers both.

      Staging: if nothing is staged yet, stage everything (`git add -A`).
      Running the skill is the user's go-ahead, so an empty index means
      "commit what I have", not "give up". Prints a single STATE line the
      model can parse, then the staged stat and diff.

  commit <branch-slug>
      Read a commit message from stdin and land exactly one commit. If HEAD
      is on a protected branch (main/master by default) or detached, first
      carve off a new branch named from <branch-slug> so the protected
      branch is never committed to directly — including an unborn protected
      branch (fresh repo, no commits yet): `git switch -c` renames the unborn
      tip so the first commit lands on the feature branch and main/master
      stays empty. Creating a branch from the current commit keeps the
      staged/unstaged split byte-for-byte intact (verified: `git switch -c`
      touches neither index nor worktree), so there is no stash dance and
      nothing can be lost. Prints a COMMITTED line with the resulting branch
      and short SHA.

Env knobs (all optional):
  COMMIT_PROTECTED_BRANCHES   space-separated branch names to auto-branch
                              away from (default: "main master"). Set empty
                              to allow committing straight onto any branch.
  COMMIT_DIFF_MAX_LINES       how many diff lines `prepare` prints (default 500).
  SKILLS_LANG_KEY             git config key holding the language record
                              (default: skills.lang), shared with create-pr.

Requires: Python 3.9+, git on PATH. Stdlib only. This skill installs
standalone, so force_utf8/run_command/read_stdin_bytes are small local copies,
not imports from a sibling skill directory.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List, NamedTuple, Optional, Sequence


def force_utf8() -> None:
    """Make stdout/stderr UTF-8 so emoji-bearing diffs survive Windows
    consoles (whose default encoding is often cp1252/GBK, not UTF-8)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_stdin_bytes() -> bytes:
    """Read stdin as RAW BYTES, like the shell's `... | git commit -F -` pipe.

    Deliberate deviation from the "read sys.stdin.read()" porting rule.
    force_utf8() can only reconfigure stdout/stderr; sys.stdin keeps
    locale.getpreferredencoding() with errors="strict". On Windows
    (cp936/cp1252/cp932) a text read of a Chinese commit body — the primary
    body language of this bilingual skill — either mangles it into mojibake or
    raises UnicodeDecodeError, and that traceback lands AFTER the feature
    branch was carved, leaving it checked out and empty. So the message is
    never decoded: its bytes go straight to git.
    """
    stream = sys.stdin
    if stream is None:  # no stdin at all (pythonw and friends)
        return b""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        return buffer.read()
    # No binary view (a text stream someone swapped in): take the text and
    # encode it as UTF-8, which is what a byte-transparent read would have got.
    data = stream.read()
    return data if isinstance(data, bytes) else data.encode("utf-8", "replace")


class RunResult(NamedTuple):
    out: str
    err: str
    rc: int


def run_command(
    cmd: Sequence[str],
    *,
    stdin: Optional[str] = None,
    stdin_bytes: Optional[bytes] = None,
    cwd: Optional[str] = None,
) -> RunResult:
    """Run cmd without a shell, decoding output as UTF-8. A missing binary maps
    to rc 127 (like a shell), so "git not installed" reads as an ordinary
    failure instead of a Python traceback.

    `stdin_bytes` runs the child in BINARY mode and feeds it those bytes
    verbatim — the commit message takes that path so its exact bytes reach
    `git commit -F -`, the way the shell's pipe delivered them. Output is still
    handed back as str (UTF-8, errors="replace"), so callers see one RunResult
    shape either way.

    The str `stdin` path has NO caller in this module and is kept only so this
    local copy keeps the same shape as the sibling ship-pr/scripts/gh_retry.py
    run_command (this skill installs standalone, so the helper is duplicated
    rather than imported). Everything in here that writes to a child's stdin
    goes through `stdin_bytes`."""
    raw = stdin_bytes is not None
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            input=stdin_bytes if raw else stdin,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not raw,
            encoding=None if raw else "utf-8",
            errors=None if raw else "replace",
        )
    except FileNotFoundError:
        return RunResult("", "{}: command not found".format(cmd[0]), 127)
    except OSError as exc:
        return RunResult("", "{}: {}".format(cmd[0], exc), 126)
    if raw:
        return RunResult(
            (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"),
            proc.returncode,
        )
    return RunResult(proc.stdout or "", proc.stderr or "", proc.returncode)


class Die(Exception):
    """Raised where the shell called die(): message to stderr, exit 1."""


def die(msg: str) -> None:
    raise Die(msg)


# --- env knobs (read at call time so tests and importers can vary them) -----


def protected_branches() -> List[str]:
    # Shell `${COMMIT_PROTECTED_BRANCHES-main master}`: unset falls back to
    # the default, but set-and-EMPTY stays empty — that is the documented way
    # to allow committing straight onto any branch.
    raw = os.environ.get("COMMIT_PROTECTED_BRANCHES")
    if raw is None:
        raw = "main master"
    return raw.split()


def diff_cap() -> int:
    # Shell `${COMMIT_DIFF_MAX_LINES:-500}` plus an all-digits guard:
    # unset, empty, or non-numeric all mean 500.
    raw = os.environ.get("COMMIT_DIFF_MAX_LINES") or "500"
    if not re.fullmatch(r"[0-9]+", raw):
        return 500
    return int(raw)


def lang_key() -> str:
    # Shell `${SKILLS_LANG_KEY:-skills.lang}`: empty falls back too.
    return os.environ.get("SKILLS_LANG_KEY") or "skills.lang"


# --- git helpers ------------------------------------------------------------


def _git(
    *args: str,
    stdin: Optional[str] = None,
    stdin_bytes: Optional[bytes] = None,
) -> RunResult:
    return run_command(["git", *args], stdin=stdin, stdin_bytes=stdin_bytes)


def in_repo() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").rc == 0


def current_branch() -> str:
    return _git("branch", "--show-current").out.strip()


def has_commits() -> bool:
    return _git("rev-parse", "--verify", "-q", "HEAD").rc == 0


def worktree_dirty() -> bool:
    return _git("status", "--porcelain").out.strip() != ""


def staged_count() -> int:
    # number of paths in the index that differ from HEAD
    out = _git("diff", "--staged", "--name-only").out
    return sum(1 for line in out.split("\n") if line)


def is_protected(branch: str) -> bool:
    # an empty branch name means detached HEAD, always carve off a branch
    # there, otherwise the commit would be stranded with no ref pointing at it
    if not branch:
        return True
    return branch in protected_branches()


def resolve_lang(want: str) -> str:
    """resolve_lang [zh|en] -> the language to write the body in.
    An explicit value is recorded for this repo; otherwise the record wins,
    and a repo with no record falls back to `en`."""
    if want in ("zh", "en"):
        if _git("config", "--local", lang_key(), want).rc != 0:
            die("failed to persist language preference")
        return want
    if want != "":
        die("unknown language '{}' (expected zh or en)".format(want))
    rec = _git("config", "--local", "--get", lang_key()).out.strip()
    if rec in ("zh", "en"):
        return rec
    return "en"


def head_lines(text: str, n: int) -> str:
    """Emulate `head -n n` on a captured string byte-for-byte: split on "\\n"
    only. (str.splitlines would also split on form feeds / U+2028, which can
    appear inside diff content and would skew the count versus head/wc -l.)"""
    if n <= 0:
        return ""
    pos = 0
    for _ in range(n):
        idx = text.find("\n", pos)
        if idx == -1:
            return text  # fewer than n newline-terminated lines: whole text
        pos = idx + 1
    return text[:pos]


# --- subcommands ------------------------------------------------------------


def cmd_prepare(lang_arg: str) -> int:
    if not in_repo():
        print("STATE repo=no")
        return 0

    msg_lang = resolve_lang(lang_arg)

    # If nothing is staged yet, stage everything: running the skill is the
    # user's go-ahead, so an empty index means "commit what I have".
    auto_staged = "no"
    if staged_count() == 0 and worktree_dirty():
        add = _git("add", "-A")
        if add.rc != 0:
            # set -e semantics: a failing `git add -A` aborts prepare with
            # git's own error and exit code.
            sys.stdout.write(add.out)
            sys.stderr.write(add.err)
            return add.rc
        auto_staged = "yes"

    branch = current_branch()
    protected = "yes" if is_protected(branch) else "no"
    unborn = "no" if has_commits() else "yes"
    sc = staged_count()

    print(
        "STATE repo=yes branch={} lang={} protected={} unborn={} "
        "auto_staged={} staged_files={}".format(
            branch or "<detached>", msg_lang, protected, unborn, auto_staged, sc
        )
    )
    print()

    if sc == 0:
        print("NO_CHANGES (working tree is clean, nothing to commit)")
        return 0

    cap = diff_cap()

    print("## Recent commits (type and scope style to match)")
    # stderr suppressed and failure tolerated, like the shell's
    # `git log ... 2>/dev/null || true` — an unborn repo has no log yet.
    sys.stdout.write(_git("log", "--oneline", "-8").out)
    print()

    print("## Staged stat")
    stat = _git("diff", "--staged", "--stat", "--stat-count={}".format(cap))
    sys.stdout.write(stat.out)
    if stat.err:
        sys.stderr.write(stat.err)  # the shell did not redirect stderr here
    if stat.rc != 0:
        return stat.rc  # set -e: the stat command was not `|| true`-guarded
    print()

    # The shell ran `git diff --staged` twice (once through wc -l for the
    # total, once through head for the capped body); nothing mutates between,
    # so one captured run feeds both. wc -l counts newline characters.
    diff = _git("diff", "--staged")
    if diff.rc != 0:
        # set -e + pipefail: a failing diff read aborted before the header.
        return diff.rc
    total = diff.out.count("\n")
    print(
        "## Staged diff (first {} of {} lines; read more with: "
        "git diff --staged -- <path>)".format(cap, total)
    )
    sys.stdout.write(head_lines(diff.out, cap))
    return 0


def cmd_commit(slug: str) -> int:
    if not in_repo():
        die("Not a git repository.")

    if staged_count() == 0:
        die("Nothing staged to commit (run 'prepare' first).")

    branch = current_branch()
    created = ""

    # Always carve off a feature branch on protected/detached HEAD, including
    # an unborn main/master: switch -c renames the unborn tip so the first
    # commit never lands on the protected name.
    if is_protected(branch):
        if not slug:
            die(
                "On protected branch '{}' but no branch slug was given.".format(
                    branch or "<detached>"
                )
            )
        name, n = slug, 2
        while _git("show-ref", "--verify", "-q", "refs/heads/{}".format(name)).rc == 0:
            name = "{}-{}".format(slug, n)
            n += 1
        if _git("switch", "-c", name).rc != 0:
            # older gits lack `switch`; fall back to checkout -b
            if _git("checkout", "-b", name).rc != 0:
                die("Failed to create branch '{}'.".format(name))
        created = name

    # The commit message arrives on stdin (`git commit -s -F -`) and is piped
    # through as RAW BYTES — see read_stdin_bytes: decoding it here would
    # mojibake or crash a Chinese body on a Windows codepage, right after the
    # branch above was carved. git is the one that rejects an empty or
    # whitespace-only message ("Aborting commit due to empty commit message."),
    # exactly as it did for the shell. The shell silenced only stdout; stderr
    # still flowed through.
    message = read_stdin_bytes()
    res = _git("commit", "-s", "-F", "-", stdin_bytes=message)
    if res.err:
        sys.stderr.write(res.err)
    if res.rc != 0:
        return res.rc  # set -e: a failing commit aborts with git's exit code

    sha = _git("rev-parse", "--short", "HEAD").out.strip()
    now = current_branch()
    if created:
        print(
            "COMMITTED branch={} created_branch={} sha={}".format(
                now or "<detached>", created, sha
            )
        )
    else:
        print("COMMITTED branch={} sha={}".format(now or "<detached>", sha))
    return 0


def main(argv: Sequence[str]) -> int:
    force_utf8()
    try:
        cmd = argv[0] if argv else ""
        if cmd == "prepare":
            return cmd_prepare(argv[1] if len(argv) > 1 else "")
        if cmd == "commit":
            return cmd_commit(argv[1] if len(argv) > 1 else "")
        die("usage: commit_helper.py {prepare [zh|en] | commit <branch-slug>}")
    except Die as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    return 0  # unreachable; dispatch above always returns or dies


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
