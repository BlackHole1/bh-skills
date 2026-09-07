#!/usr/bin/env python3
"""pr_helper — collects everything the create-pr skill needs to draft a pull
request in one call, instead of a dozen shell round-trips that each recompute
the same merge-base. Read-only apart from the language record.

    prepare [zh|en] [base-branch]

        Language: an explicit `zh` or `en` wins and is recorded in this repo's
        .git/config, so later runs in the same repo default to it. With no
        argument the recorded value wins, falling back to `en`. The key is
        shared with the commit skill, so setting it once covers both.

        Base: an explicit base-branch overrides the repo's default branch.

        Prints one STATE line the model can parse, then the existing PR (if
        any), the branch's commits, the diffstat, a capped branch diff, and the
        repo's PR template.

STATE fields worth knowing:
    repo_slug           https://github.com/<repo_slug>/blob/<sha>/<path>#L1-L9
    head_sha            the branch tip; cite it to point at this PR's own code
    merge_base          the base commit; cite it to point at code as it stood
                        before the branch, and use it in follow-up git commands
    dirty / commits     whether there is work to commit before the PR

Env knobs (all optional):
    PR_DIFF_MAX_LINES   how many diff lines to print (default 600).
    SKILLS_LANG_KEY     git config key holding the language record
                        (default: skills.lang), shared with commit.

Requires: Python 3.9+ (stdlib only) and git on PATH. gh is optional: a missing
or unauthenticated gh degrades to gh=no in STATE, never a crash.

create-pr is a standalone installable skill, so this file must not import from
sibling skill directories; force_utf8/run_command below are intentional small
local copies of the ship-pr gh_retry helpers.

Deliberate deviations from pr-helper.sh (each one is argued at its site):
    - an unborn HEAD prints every section and exits 0, where the shell aborted
      with 128 mid-output (see the Branch diff section);
    - an unresolvable @{push} reports upstream=none, where the shell reported
      the literal string "@{push}" with ahead=0 (see the upstream probe);
    - a repo whose defaultBranchRef is null keeps gh=yes and the repo_slug and
      falls back to `main`, where the shell produced an empty base;
    - the mirror-image answer — a usable defaultBranchRef but NO nameWithOwner —
      reports gh=no with repo_slug=unknown (the branch that did come back is
      still used as default_base), where jq's `null + " " + "trunk"` handed the
      shell a non-empty " trunk" and so gh=yes with an EMPTY repo_slug, a slug
      it would have pasted into permalinks. Unreachable in practice (gh does not
      drop a field it was asked for), and gh=no is the honest answer when the
      one field gh=yes promises is the missing one;
    - the PR template is copied to stdout as bytes, like `cat`, so a CRLF or
      non-UTF-8 template survives untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional, Sequence


def force_utf8() -> None:
    """Make stdout/stderr UTF-8 so emoji-bearing PR content survives Windows
    consoles (whose default encoding is often cp1252/GBK, not UTF-8)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def write_stdout_bytes(data: bytes) -> None:
    """Copy bytes to stdout unchanged, the way the shell's `cat` did. The text
    layer is flushed first so the bytes land in order; a stdout without a
    binary buffer (a captured or wrapped stream) falls back to a decoded
    write."""
    sys.stdout.flush()
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:
        sys.stdout.write(data.decode("utf-8", "replace"))
        return
    buf.write(data)
    buf.flush()


class RunResult(NamedTuple):
    out: str
    err: str
    rc: int


def run_command(
    cmd: Sequence[str],
    *,
    stdin: Optional[str] = None,
    cwd: Optional[str] = None,
) -> RunResult:
    """Run cmd without a shell, decoding output as UTF-8. A missing binary maps
    to rc 127 (like a shell), so "gh not installed" reads as an ordinary
    failure instead of a Python traceback."""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            input=stdin,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return RunResult("", "{}: command not found".format(cmd[0]), 127)
    except OSError as exc:
        return RunResult("", "{}: {}".format(cmd[0], exc), 126)
    return RunResult(proc.stdout or "", proc.stderr or "", proc.returncode)


# Injectable seam (tests monkeypatch it): every gh call goes through _gh, so no
# test needs the gh binary or auth. git keeps calling run_command directly —
# git behavior is tested against real throwaway repos.
_gh = run_command


# Spelled as an escape so this source file never carries an invisible one.
BOM = "\ufeff"


class Fatal(Exception):
    """Shell `die`: the message goes to stderr and the script exits 1."""


def _diff_cap() -> int:
    """PR_DIFF_MAX_LINES, with the shell's guard: unset, empty, or anything
    containing a non-[0-9] character falls back to 600."""
    raw = os.environ.get("PR_DIFF_MAX_LINES", "") or "600"
    return int(raw) if raw.isascii() and raw.isdigit() else 600


def _lang_key() -> str:
    return os.environ.get("SKILLS_LANG_KEY", "") or "skills.lang"


def resolve_lang(want: str) -> str:
    """Return the language to write the body in. An explicit value is recorded
    for this repo; otherwise the record wins, and a repo with no record falls
    back to `en`."""
    key = _lang_key()
    if want in ("zh", "en"):
        r = run_command(["git", "config", "--local", key, want])
        if r.rc != 0:
            raise Fatal(
                "failed to persist language preference ({}={})".format(key, want)
            )
        return want
    if want != "":
        raise Fatal("unknown language '{}' (expected zh or en)".format(want))
    rec = run_command(["git", "config", "--local", "--get", key]).out.rstrip("\n")
    return rec if rec in ("zh", "en") else "en"


_TEMPLATE_CANDIDATES = (
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
)


def find_pr_template() -> str:
    """First existing candidate, else the first *.md under
    .github/PULL_REQUEST_TEMPLATE/. The shell used unordered `find | head -1`;
    sorting the glob for determinism is a sanctioned tiny deviation."""
    for cand in _TEMPLATE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    hits = sorted(
        p.as_posix()
        for p in Path(".github/PULL_REQUEST_TEMPLATE").rglob("*.md")
        if p.is_file()
    )
    return hits[0] if hits else ""


def _head_lines(text: str, n: int) -> str:
    """First n lines like `head -n`: split on \\n only (str.splitlines would
    also split on \\x0b/\\u2028 etc., which head does not)."""
    if n <= 0 or not text:
        return ""
    pieces = text.split("\n")
    nlines = len(pieces) - 1 if text.endswith("\n") else len(pieces)
    if n >= nlines:
        return text
    return "\n".join(pieces[:n]) + "\n"


def cmd_prepare(args: Sequence[str]) -> int:
    if run_command(["git", "rev-parse", "--is-inside-work-tree"]).rc != 0:
        print("STATE repo=no")
        return 0

    msg_lang = resolve_lang(args[0] if len(args) > 0 else "")
    base_arg = args[1] if len(args) > 1 else ""

    branch = run_command(["git", "branch", "--show-current"]).out.rstrip("\n")
    detached = "no" if branch else "yes"

    # One gh call for both facts, so an unauthenticated gh costs one timeout
    # rather than two. (jq is gone: the JSON is parsed here instead of gh -q.)
    repo_slug, default_base, gh_ok = "unknown", "main", "yes"
    meta = _gh(["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"])
    slug = name = None
    # The rc is not gated on, matching the shell's `|| true` capture: gh can
    # exit non-zero after printing perfectly usable JSON (a partial GraphQL
    # error, an update-check failure), and an answer that parses is an answer.
    if meta.out.strip():
        try:
            # One leading BOM is skipped the way jq did. gh output can arrive
            # BOM-prefixed (a proxy, a re-encoding), and without this the whole
            # answer is discarded: gh=no and repo_slug=unknown, which costs the
            # permalink citations this script exists to enable. Anything beyond
            # a single BOM is still malformed and still raises.
            data = json.loads(meta.out[1:] if meta.out[:1] == BOM else meta.out)
        except ValueError:
            data = None
        if isinstance(data, dict):
            slug = data.get("nameWithOwner")
            ref = data.get("defaultBranchRef")
            name = ref.get("name") if isinstance(ref, dict) else None
    # The two fields are read INDEPENDENTLY, because they fail independently:
    #   1. both usable -> the real slug and the real default branch;
    #   2. slug but no branch name -> a repo with nothing pushed yet reports
    #      "defaultBranchRef": null. jq's `.nameWithOwner + " " + null` is just
    #      the slug, so the shell saw a non-empty meta, kept gh=yes and the
    #      slug, and ended up with an EMPTY default_base (hence an empty base).
    #      Keep the gh=yes and the slug — permalink citations are built from
    #      repo_slug, and reporting gh=no for a call gh answered would throw it
    #      away — but fall back to `main` for the base, which is usable where
    #      the shell's empty string was not;
    #   3. no usable slug -> gh is missing, unauthenticated, or said something
    #      unparseable: gh=no and repo_slug=unknown, since repo_slug is the
    #      field gh=yes is a promise about. default_base is NOT dragged down
    #      with it — it is read on its own, so a defaultBranchRef that did come
    #      back still wins over guessing `main` (see the deviation note in the
    #      module docstring); only a missing branch name falls back.
    if isinstance(slug, str) and slug:
        repo_slug = slug
    else:
        gh_ok = "no"
    if isinstance(name, str) and name:
        default_base = name

    base = base_arg or default_base

    mb = ""
    for cand in (
        ["git", "merge-base", "HEAD", "origin/" + base],
        ["git", "merge-base", "HEAD", base],
    ):
        r = run_command(cand)
        if r.rc == 0 and r.out.strip():
            mb = r.out.rstrip("\n")
            break
    if not mb:
        # No merge base at all (e.g. the base branch does not exist): fall back
        # to the branch's root commit so the range still means "this branch's
        # own work". rev-list can print several roots; take the LAST line, as
        # the shell's `tail -1` did.
        lines = run_command(
            ["git", "rev-list", "--max-parents=0", "HEAD"]
        ).out.rstrip("\n").splitlines()
        mb = lines[-1] if lines else ""
    rng = "{}..HEAD".format(mb) if mb else "HEAD"

    # DEVIATION (deliberate, and a fix): when @{push} cannot be resolved this
    # rev-parse ECHOES the literal string "@{push}" to stdout while exiting
    # 128 (reproducible with branch.<name>.merge pointing at a pruned ref).
    # The shell captured stdout regardless of the rc and so reported
    # upstream=@{push} with ahead=0 — which reads as "this branch is already
    # pushed and has nothing new", exactly the conclusion that makes a caller
    # skip the push. Gating on the rc reports upstream=none / ahead=n/a, the
    # truth, so a bogus upstream can never suppress a needed push.
    r = run_command(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"]
    )
    upstream = r.out.rstrip("\n") if r.rc == 0 else ""
    if upstream:
        r = run_command(["git", "rev-list", "--count", "@{push}..HEAD"])
        ahead = r.out.rstrip("\n") if r.rc == 0 else "0"
    else:
        upstream, ahead = "none", "n/a"

    dirty = "yes" if run_command(["git", "status", "--porcelain"]).out.rstrip("\n") else "no"

    on_base = "yes" if branch and branch == base else "no"

    r = run_command(["git", "rev-list", "--count", rng])
    ncommits = r.out.rstrip("\n") if r.rc == 0 else "0"

    # Shell: $(git rev-parse HEAD 2>/dev/null || echo "") — rev-parse echoes an
    # unresolvable arg to stdout, so on an unborn HEAD this captured the
    # literal string "HEAD"; keeping the stdout passthrough preserves that.
    head_sha = run_command(["git", "rev-parse", "HEAD"]).out.rstrip("\n")

    print(
        "STATE repo=yes branch={} detached={} lang={} base={} default_base={} "
        "on_base={} repo_slug={} head_sha={} merge_base={} upstream={} "
        "ahead={} dirty={} commits={} gh={}".format(
            branch or "<detached>",
            detached,
            msg_lang,
            base,
            default_base,
            on_base,
            repo_slug,
            head_sha,
            mb or "none",
            upstream,
            ahead,
            dirty,
            ncommits,
            gh_ok,
        )
    )
    print()

    print("## Existing PR (NONE means create a new one)")
    pr = _gh(
        ["gh", "pr", "view", "--json", "number,state,isDraft,title,url,baseRefName,body"]
    )
    sys.stdout.write(pr.out)  # the shell passed gh's stdout through verbatim
    if pr.rc != 0:
        print("NONE")
    print()

    print("## Commits on this branch (oldest first, subjects and bodies)")
    sys.stdout.write(
        run_command(["git", "log", "--reverse", "--format=>>> %h %s%n%b", rng]).out
    )
    print()

    print("## Files changed")
    sys.stdout.write(run_command(["git", "diff", "--stat", rng]).out)
    print()

    # DEVIATION (deliberate): the rc of this git diff is ignored. In the shell
    # the equivalent `git diff "$rng" | wc -l` was the ONE unguarded pipeline
    # under `set -euo pipefail`, so on an unborn HEAD (where `git diff HEAD`
    # fails) the whole script aborted with exit 128 right after the Files
    # changed section. Printing the remaining sections and exiting 0 is
    # strictly more useful — a fresh `git init` with no commits is exactly a
    # state this skill is meant to describe — and the STATE line the caller
    # routes on has already been printed either way.
    cap = _diff_cap()
    diff = run_command(["git", "diff", rng]).out
    total = diff.count("\n")  # wc -l counts newlines
    print(
        "## Branch diff (first {} of {} lines; read more with: "
        "git diff {}..HEAD -- <path>)".format(cap, total, mb or "HEAD")
    )
    sys.stdout.write(_head_lines(diff, cap))
    print()

    tmpl = find_pr_template()
    if tmpl:
        print("## PR template ({}), its sections are required".format(tmpl))
        # `cat`, not a text read: a text-mode read would rewrite a CRLF
        # template (normal on Windows) to LF and re-encode the content, and a
        # template that is not UTF-8 would come out mangled. The model is
        # supposed to fill in this file's sections verbatim, so pass the bytes
        # through untouched.
        with open(tmpl, "rb") as fh:
            write_stdout_bytes(fh.read())
    else:
        print("## PR template: NONE")
    return 0


def _mute_stdout() -> None:
    """Point stdout at the null device so the interpreter's exit-time flush
    cannot raise a second time on a pipe that is already gone.

    The devnull descriptor is closed on every path: dup2 DUPLICATES it, so the
    original is redundant once installed, and a stdout with no real fileno (a
    captured or wrapped stream — every in-process caller) never installs it at
    all. Leaving it open leaked one descriptor per call."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(fd, sys.stdout.fileno())
    except (OSError, ValueError):  # no real fd (captured/wrapped stdout)
        pass
    finally:
        os.close(fd)


def main(argv: Sequence[str]) -> int:
    force_utf8()
    try:
        if argv and argv[0] == "prepare":
            rc = cmd_prepare(argv[1:])
            # Flush inside the guard: when the pipe dies while output is still
            # buffered, the failure must surface here and not in the
            # interpreter's exit-time flush, which is past every handler.
            sys.stdout.flush()
            return rc
        raise Fatal("usage: pr_helper.py prepare [zh|en] [base-branch]")
    except Fatal as exc:
        sys.stderr.write("{}\n".format(exc))
        return 1
    except BrokenPipeError:
        # This output is meant to be piped (the skill documents it), and a
        # consumer that stops reading early — `| head`, a model that got its
        # STATE line — must not be answered with a traceback plus Python's
        # "Exception ignored ... BrokenPipeError" shutdown noise. Behave like
        # the shell did when SIGPIPE killed it: say nothing, exit 141.
        _mute_stdout()
        return 141
    except OSError as exc:
        # Anything else the filesystem or a pipe can throw (a template deleted
        # between the isfile check and the read, a full disk). The shell died
        # with one tidy line, so do that instead of a traceback.
        sys.stderr.write("pr_helper.py: {}\n".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
