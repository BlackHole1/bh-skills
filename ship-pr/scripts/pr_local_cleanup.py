#!/usr/bin/env python3
"""pr_local_cleanup — return the user's LOCAL checkout to a clean base after a
PR has merged.

ship-pr does all its babysitting in an isolated worktree and merges remotely
(pr-merge runs with -R), so it deliberately never touches the user's own
checkout, branch, or index. That leaves one nicety undone: after the PR lands,
the user is often still sitting on the now-merged branch with a stale local
base. This is the one sanctioned moment to touch their checkout — AFTER the
merge is confirmed — and tidy it the way `gh pr merge --delete-branch` would
for a local merge (the cleanup pr-merge skips by being remote-only): switch
off the merged branch to the base branch, fast-forward it, delete the merged
local branch.

It is conservative on purpose. Every step is gated, and on anything it can't do
safely it changes NOTHING and exits 3 (a deliberate skip, not an error) with a
one-line reason — so an unattended run can never lose work or yank the rug from
under a user who's mid-task.

Usage: pr_local_cleanup.py <PR> [OWNER/REPO] [--base BRANCH] [--dry-run]
  --base BRANCH   override the base branch (default: the PR's baseRefName)
  --dry-run       print what it would do, change nothing (read-only — no fetch,
                   no writes; it still reads the PR's state to decide)

Exit codes:
  0  cleaned up, or already clean (nothing left to do)
  3  skipped: a precondition wasn't met — not the target repo / PR not MERGED /
     dirty or mid-operation tree / detached HEAD / on an unrelated branch / no
     base branch to return to / a fork branch it can't match. The reason is
     printed. This means "left as-is", NOT a failure.
  2  usage error
  1  unexpected error

Requires: gh (authenticated), git, sibling gh_retry.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

from gh_retry import RunResult, force_utf8, gh_retry, loads_json, run_command

USAGE = "usage: pr_local_cleanup.py <PR> [OWNER/REPO] [--base BRANCH] [--dry-run]"

IN_PROGRESS_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)


# --- gh seams (tests monkeypatch these; everything else is git or pure) ---

def _gh_detect_pr(repo: str) -> str:
    """Seam: the current branch's PR number (plain gh, no retry — "no PR for
    this branch" is a normal outcome here, not a transient failure)."""
    cmd = ["gh", "pr", "view"]
    if repo:
        cmd += ["-R", repo]
    cmd += ["--json", "number", "-q", ".number"]
    return run_command(cmd).out.rstrip("\n")


def _gh_current_repo() -> str:
    """Seam: `gh repo view` (no -R) reports the repo of the current
    directory's remote."""
    return run_command(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    ).out.rstrip("\n")


def _gh_pr_json(pr: str, slug: str) -> str:
    """Seam: the PR fields the gates decide on, read with retry — a transient
    read must not be mistaken for an unreadable PR."""
    return gh_retry(
        [
            "gh", "pr", "view", pr, "-R", slug,
            "--json", "state,headRefName,baseRefName,headRefOid,isCrossRepository",
        ]
    ).out


# --- git helper: real subprocesses in the current working directory ---

def _git(*args: str) -> RunResult:
    """Run git in the process cwd (the checkout being tidied)."""
    return run_command(["git", *args])


def _git_pull_combined(remote: str, base: str) -> Tuple[str, int]:
    """git pull --ff-only with stdout+stderr merged into one stream, like the
    shell's `2>&1` capture — the first line of whatever git said is what gets
    reported when the fast-forward fails."""
    try:
        proc = subprocess.run(
            ["git", "pull", "--ff-only", remote, base],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return "git: command not found", 127
    except OSError as exc:
        return "git: {}".format(exc), 126
    return proc.stdout or "", proc.returncode


# --- pure gate helpers ---

def parse_pr_fields(prj: str) -> Tuple[str, str, str, str, bool]:
    """(state, head, base, headoid, cross) from gh's JSON. Unreadable JSON
    degrades to empty fields, exactly like the shell's failed jq filters —
    the later gates then skip rather than act on garbage. A leading UTF-8 BOM
    is NOT unreadable (jq skipped it, and so does loads_json): degrading on one
    would turn a legitimate ACT run into a silent SKIP."""
    try:
        data = loads_json(prj)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}

    def field(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    # jq printed `.isCrossRepository // false` and the shell compared the text
    # to "true" — only boolean true (or a literal "true" string) passes.
    raw_cross = data.get("isCrossRepository")
    cross = raw_cross is True or raw_cross == "true"
    return (
        field("state"),
        field("headRefName"),
        field("baseRefName"),
        field("headRefOid"),
        cross,
    )


def classify_porcelain(porcelain: str) -> Optional[str]:
    """None = clean, "dirty" = staged/unstaged changes, "untracked" = only
    '??' lines. "Clean" means what the user means by "no changes": nothing
    staged, unstaged, or untracked. The advice is split so the caller can
    relay a remedy that actually works — `git stash` (no -u) does NOT clear
    untracked files, so naming it for an untracked-only tree loops."""
    if not porcelain:
        return None
    if any(not line.startswith("??") for line in porcelain.split("\n")):
        return "dirty"
    return "untracked"


def pull_did_entry(pull_out: str, pull_rc: int, base: str) -> Optional[str]:
    """What the pull step contributes to the report: None for the benign
    "already up to date" no-op (both git spellings, checked before the exit
    code exactly like the shell's case statement — keeps a benign rerun a true
    no-op), a fast-forward note on success, else the FIRST line of git's
    complaint."""
    if "Already up to date" in pull_out or "Already up-to-date" in pull_out:
        return None
    if pull_rc == 0:
        return "fast-forwarded '{}'".format(base)
    return "left '{}' as-is (couldn't fast-forward: {})".format(
        base, pull_out.split("\n", 1)[0]
    )


def url_matches_slug(url: str, slug: str) -> bool:
    """The shell's case patterns *[:/]slug | *[:/]slug.git | *[:/]slug/ —
    a remote URL whose path component ends in exactly the slug, whatever the
    protocol or host."""
    for sep in (":", "/"):
        for suffix in (sep + slug, sep + slug + ".git", sep + slug + "/"):
            if url.endswith(suffix):
                return True
    return False


# --- git-backed helpers (mirror the shell functions of the same name) ---

def resolve_remote(base: str, slug: str) -> str:
    """Which remote points at the PR's repo? Don't assume "origin": fork
    workflows clone the canonical repo as `upstream` (origin = the user's
    fork), and people rename remotes. Resolve it once — prefer the base
    branch's configured upstream, else the remote whose URL matches the slug,
    else origin, else the sole remote."""
    remotes = _git("remote").out.split()
    cfg = _git("config", "--get", "branch.{}.remote".format(base)).out.rstrip("\n")
    if cfg and cfg in remotes:
        return cfg
    for r in remotes:
        url = _git("remote", "get-url", r).out.rstrip("\n")
        if url_matches_slug(url, slug):
            return r
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    return ""


def switch_to(branch: str, remote: str) -> bool:
    """Move to `branch`: use the local branch, else create it tracking the
    remote."""
    if _git("show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch)).rc == 0:
        return (
            _git("switch", branch).rc == 0
            or _git("checkout", branch).rc == 0
        )
    if remote and _git(
        "show-ref", "--verify", "--quiet",
        "refs/remotes/{}/{}".format(remote, branch),
    ).rc == 0:
        track = "{}/{}".format(remote, branch)
        return (
            _git("switch", "-c", branch, "--track", track).rc == 0
            or _git("checkout", "-b", branch, "--track", track).rc == 0
        )
    return False


def _skip(reason: str) -> int:
    """A deliberate skip: say why, change nothing, exit 3 (not a failure)."""
    print("SKIPPED: {}".format(reason))
    return 3


def main(argv: Sequence[str]) -> int:
    force_utf8()

    pr = ""
    repo = os.environ.get("GH_REPO", "")
    base_override = ""
    dry = False

    args = list(argv)
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--base":
            if i + 1 >= len(args):
                sys.stderr.write("missing value for --base\n")
                return 2
            base_override = args[i + 1]
            i += 2
        elif a == "--dry-run":
            dry = True
            i += 1
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        elif a.startswith("-"):
            sys.stderr.write("unknown flag: {}\n".format(a))
            return 2
        elif "/" in a:
            repo = a
            i += 1
        else:
            if not pr:
                pr = a
            else:
                repo = a
            i += 1

    if not pr:
        pr = _gh_detect_pr(repo)
    if not pr:
        sys.stderr.write(USAGE + "\n")
        return 2

    # --- Gate: we must be inside a git checkout of the PR's repo ---
    if _git("rev-parse", "--is-inside-work-tree").rc != 0:
        return _skip("not inside a git working tree — nothing local to tidy up.")

    cur = _gh_current_repo()
    if not cur:
        return _skip(
            "this directory has no resolvable GitHub repo — leaving the local side alone."
        )
    # GitHub repo slugs are case-insensitive, so compare lower-cased — a caller
    # passing a different casing than gh's canonical owner/repo is still a match.
    if repo and cur.lower() != repo.lower():
        return _skip(
            "current repo ({}) isn't the PR's repo ({}) — leaving your checkout untouched.".format(
                cur, repo
            )
        )
    slug = cur

    # --- Gate: the PR must actually be MERGED (never delete a branch for an open PR) ---
    prj = _gh_pr_json(pr, slug)
    # An all-whitespace body is a failed read, not a PR. The shell captured this
    # with `$( )`, which strips trailing newlines, so a reply of just a newline
    # was EMPTY and took this branch. Keep that honesty for every whitespace
    # shape: a body that can never parse must not slip past here and degrade
    # every field to "" — that reads as "no base branch" and exits 3 (a
    # deliberate skip) when the truth is "the read failed", which is exit 1.
    if not prj.strip():
        sys.stderr.write(
            "pr-local-cleanup: could not read PR #{} (transient?) — not touching local branches.\n".format(
                pr
            )
        )
        return 1
    state, head, base, headoid, cross = parse_pr_fields(prj)
    if base_override:
        base = base_override
    if not base:
        return _skip("couldn't resolve the base branch for #{}.".format(pr))
    if state != "MERGED":
        return _skip(
            "PR #{} is {}, not MERGED — local branch left in place.".format(pr, state)
        )

    remote = resolve_remote(base, slug)

    # --- Gate: the working tree must be pristine ---
    porcelain = _git("status", "--porcelain").out.rstrip("\n")
    verdict = classify_porcelain(porcelain)
    if verdict == "dirty":
        return _skip(
            "working tree has uncommitted changes — commit or stash them first, then I can tidy up."
        )
    if verdict == "untracked":
        return _skip(
            "working tree has untracked files — commit them, 'git stash -u', or remove them first, then I can tidy up."
        )
    gd = _git("rev-parse", "--git-dir").out.rstrip("\n")
    for marker in IN_PROGRESS_MARKERS:
        if os.path.exists(os.path.join(gd, marker)):
            return _skip(
                "a git operation ({}) is in progress — leaving your checkout alone.".format(
                    marker
                )
            )

    # --- Gate: a named branch we recognise (the merged branch, or the base) ---
    curbranch = _git("symbolic-ref", "--quiet", "--short", "HEAD").out.rstrip("\n")
    if not curbranch:
        return _skip("HEAD is detached — not auto-switching. (#{} is merged.)".format(pr))

    on_head = curbranch == head
    on_base = curbranch == base

    do_delete = True
    if cross:
        # Fork PR: headRefName lives on the fork, so a local branch of that
        # name is not reliably the merged branch. Do only the safe half
        # (return to base + pull) and never delete a branch we can't prove is
        # the one that merged.
        do_delete = False
        on_head = False

    if not on_head and not on_base:
        if cross:
            return _skip(
                "you're on '{}'; #{} merged from a fork and can't be matched to a local branch. "
                "Switch to '{}' yourself if you want a pull. (#{} is merged.)".format(
                    curbranch, pr, base, pr
                )
            )
        return _skip(
            "you're on '{}', neither the merged branch ('{}') nor the base ('{}') — not moving you. (#{} is merged.)".format(
                curbranch, head, base, pr
            )
        )

    local_head_exists = (
        _git("show-ref", "--verify", "--quiet", "refs/heads/{}".format(head)).rc == 0
    )

    # --- Dry run: report intent, touch nothing (read-only — no fetch, no writes) ---
    if dry:
        print("DRY-RUN — {} #{} is merged; would:".format(slug, pr))
        if on_head:
            print("  - switch '{}' -> '{}'".format(curbranch, base))
        print("  - fast-forward '{}'".format(base))
        if do_delete and local_head_exists and head != base:
            print(
                "  - delete local branch '{}' (only if proven fully merged; otherwise keep it)".format(
                    head
                )
            )
        return 0

    # --- Prove the local branch is fully merged before we force-delete it ---
    # Squash-merge makes `git branch -d` always cry "not fully merged", so
    # deleting needs -D (force). Force is only safe once we've shown the local
    # tip is contained in the exact commit GitHub merged: equal to it, or an
    # ancestor of it (e.g. ship-pr pushed fixes past the user's stale local
    # copy). If the local tip has commits the merge never saw — work the user
    # committed but didn't push — we KEEP the branch. `unverified`
    # distinguishes "couldn't check" (offline / no matching remote) from
    # "checked and the branch is genuinely ahead", so the kept-branch message
    # is honest.
    safe_to_delete = False
    unverified = False
    if do_delete and local_head_exists and head != base:
        localsha = _git(
            "rev-parse", "--verify", "--quiet", "refs/heads/{}".format(head)
        ).out.rstrip("\n")
        if headoid and localsha == headoid:
            safe_to_delete = True  # exact match — no network needed
        else:
            # Local tip differs: could be behind the merged tip (safe) or
            # ahead (unsafe). Fetch the merge's commit so merge-base can
            # settle which. If we can't fetch it, we can't prove safety, so we
            # keep the branch and say so.
            merged_ref = ""
            attempt = 1
            while remote and attempt <= 3:
                if _git("fetch", "-q", remote, "pull/{}/head".format(pr)).rc == 0:
                    merged_ref = _git(
                        "rev-parse", "--verify", "--quiet", "FETCH_HEAD"
                    ).out.rstrip("\n")
                    if merged_ref:
                        break
                attempt += 1
            if not merged_ref:
                unverified = True
            elif (
                localsha
                and _git("merge-base", "--is-ancestor", localsha, merged_ref).rc == 0
            ):
                safe_to_delete = True

    # --- Act ---
    did: List[str] = []

    # 1) Step off the merged branch (you can't delete the branch you're on).
    if on_head:
        if switch_to(base, remote):
            did.append("switched to '{}'".format(base))
        else:
            return _skip(
                "couldn't switch to base '{}' (no local branch and no {}/{}) — left on '{}'. (#{} is merged.)".format(
                    base, remote, base, curbranch, pr
                )
            )

    # 2) Fast-forward the base. --ff-only so we never create a merge commit or
    #    pick a fight with a conflict in the user's checkout; a diverged base
    #    is reported. Pull from the RESOLVED remote, not the branch's
    #    configured upstream — a non-origin checkout or a base we just
    #    recreated may have no upstream set. An already-up-to-date pull adds
    #    nothing — keeps a benign rerun a true no-op.
    if remote:
        pull_out, pull_rc = _git_pull_combined(remote, base)
        pull_out = pull_out.rstrip("\n")
    else:
        pull_out, pull_rc = "no remote resolved", 1
    entry = pull_did_entry(pull_out, pull_rc, base)
    if entry is not None:
        did.append(entry)

    # 3) Delete the merged local branch — only when proven safe above.
    if do_delete and local_head_exists and head != base:
        if safe_to_delete:
            if _git("branch", "-D", head).rc == 0:
                did.append("deleted merged local branch '{}'".format(head))
            else:
                did.append("could not delete '{}'".format(head))
        elif unverified:
            did.append(
                "kept '{}' — couldn't verify it's fully merged (left as-is; delete it yourself once you're sure)".format(
                    head
                )
            )
        else:
            did.append(
                "kept '{}' — it has commits the merge never saw (nothing lost; delete it yourself once you're sure)".format(
                    head
                )
            )

    print("Post-merge local cleanup for {} #{}:".format(slug, pr))
    if not did:
        print("  - nothing to do (already on '{}', merged branch gone)".format(base))
    else:
        for d in did:
            print("  - {}".format(d))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
