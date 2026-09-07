#!/usr/bin/env python3
"""pr_worktree — manage an isolated git worktree (or clone) that mirrors a PR's
head commit, so ship-pr can read / diagnose / fix PR code without touching the
user's working tree, index, or branch. It is always (re)synced to the *current*
PR head, so triage and fixes act on the real PR code — never a stale local
branch.

Usage:
  pr_worktree.py ensure <PR> [OWNER/REPO]   # create-or-refresh; prints the path
  pr_worktree.py remove <PR> [OWNER/REPO]   # tear down
  pr_worktree.py path   <PR> [OWNER/REPO]   # print the path only (no changes)

Uses a detached worktree when run inside a local clone of the target repo
(cheap — shares the object store); otherwise clones the repo into a temp dir.
The head is checked out DETACHED so it never collides with the user's own
checkout of the same branch. Pushing a fix back is the caller's job:
  same-repo PR: git -C <wt> push origin HEAD:<headRefName>
  fork PR:      push to the fork's repo (needs write access to the fork)

Requires: gh (authenticated), git.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from typing import Sequence

from gh_retry import RunResult, force_utf8, run_command

# Injectable seams (tests monkeypatch these): gh calls vs git calls.
_gh = run_command
_git = run_command


def _loud(res: RunResult) -> RunResult:
    """Forward a command's stderr like the shell did for calls it ran without
    2>/dev/null (run_command captures it; silence would hide git's diagnostics)."""
    if res.err:
        sys.stderr.write(res.err)
    return res


def _unlink_force(path: str) -> None:
    """rm -f for a non-directory (plain file, or a symlink of either kind), and
    silent when the path is simply absent. Windows needs the read-only bit
    cleared before a delete, and needs rmdir — not remove — for a directory
    symlink / junction."""
    try:
        os.remove(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    # Clear the read-only bit and retry — but NEVER on a symlink. os.chmod
    # follows symlinks (always on POSIX, and on Windows before 3.13), so
    # chmod'ing the link would change the mode of its TARGET: a write OUTSIDE
    # the worktree that rm -rf never performs, and the opposite of this
    # function's promise to unlink a link without following it. A symlink that
    # cannot be unlinked is left exactly as it was found, silently.
    if not os.path.islink(path):
        try:
            os.chmod(path, 0o700)
            os.remove(path)
            return
        except OSError:
            pass
    try:
        os.rmdir(path)  # Windows: directory symlink / junction
    except OSError:
        pass


def _rmtree_force(path: str) -> None:
    """rm -rf equivalent: removes whatever sits at the path, and never raises.

    shutil.rmtree alone is NOT rm -rf — it refuses any non-directory, so a stale
    FILE or SYMLINK left at the worktree path would survive it. That cost us two
    behaviors the shell had: `remove` printed its "removed <path>" success line
    (which a consumer parses) while the path still existed, and `ensure` lost its
    self-heal — the shell deleted the stale entry and recreated the worktree or
    clone, instead of dying with "worktree add failed" / "clone failed".

    On Windows, clone-mode .git objects are read-only and plain rmtree refuses to
    delete them — the error handler chmods the entry writable and retries
    (sanctioned portability addition)."""

    def _onerror(func, target, _exc_info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    try:
        # os.path.isdir() follows symlinks, so test islink() first: rm -rf
        # unlinks a symlink-to-a-directory, it never descends through one.
        if os.path.islink(path) or not os.path.isdir(path):
            _unlink_force(path)
        elif sys.version_info >= (3, 12):  # onerror is deprecated from 3.12 on
            shutil.rmtree(path, onexc=_onerror)
        else:
            shutil.rmtree(path, onerror=_onerror)
    except OSError:
        pass


def worktree_path(target: str, pr: str) -> str:
    """<tempdir>/ship-pr-worktrees/<owner-repo>-pr-<PR> (every '/' in the slug
    becomes '-'; the shell used ${TMPDIR:-/tmp}).

    Deliberate deviation: the shell pasted "${TMPDIR:-/tmp}/ship-pr-worktrees"
    together, so on macOS — whose TMPDIR ends in '/' — it printed a doubled
    separator (/var/.../T//ship-pr-worktrees). os.path.join collapses that; the
    clean path names the same directory and is the output we want."""
    slug = target.replace("/", "-")
    base = os.path.join(tempfile.gettempdir(), "ship-pr-worktrees")
    return os.path.join(base, "{}-pr-{}".format(slug, pr))


def _guard_base(base: str) -> str:
    """Refuse a base another local user could have planted. On Linux
    tempfile.gettempdir() is the shared, world-writable /tmp, so a pre-existing
    symlink there (or a directory someone else owns, or one anyone can write
    into) would redirect every clone, reset --hard, rm -rf — and the fixes
    pushed from that checkout — to a location of their choosing.

    Returns '' when base is a real directory of ours that only we can write,
    else a one-line diagnostic. A missing or occupied (non-directory) base is
    not a hijack: makedirs already reported it and, like the shell, the clone
    decides the rc. Windows has per-user temp dirs and no POSIX ownership
    bits, so only the symlink check applies there."""
    try:
        st = os.lstat(base)
    except OSError:
        return ""
    if stat.S_ISLNK(st.st_mode):
        return "{} is a symlink (refusing: another user may have planted it)".format(base)
    if not stat.S_ISDIR(st.st_mode):
        return ""
    if hasattr(os, "getuid"):
        if st.st_uid != os.getuid():
            return "{} is owned by uid {}, not you (refusing a foreign base)".format(
                base, st.st_uid
            )
        if stat.S_IMODE(st.st_mode) & 0o022:
            try:
                os.chmod(base, 0o700)
            except OSError as exc:
                return "{} is group/world writable and chmod 700 failed: {}".format(
                    base, exc.strerror or exc
                )
    return ""


def main(argv: Sequence[str]) -> int:
    force_utf8()

    cmd = argv[0] if len(argv) > 0 else ""
    pr = argv[1] if len(argv) > 1 else ""
    repo = argv[2] if len(argv) > 2 and argv[2] else os.environ.get("GH_REPO", "")
    if not cmd or not pr:
        sys.stderr.write("usage: pr_worktree.py ensure|remove|path <PR> [OWNER/REPO]\n")
        return 2

    # gh repo view takes the repo as a positional arg (not -R), so use the
    # explicit OWNER/REPO directly when given, otherwise infer from the current
    # directory.
    if repo:
        target = repo
    else:
        res = _gh(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
        target = res.out.rstrip("\n")
    if not target:
        sys.stderr.write("cannot resolve repo (pass OWNER/REPO, or run inside the target repo)\n")
        return 1
    wt = worktree_path(target, pr)
    base = os.path.dirname(wt)

    if cmd == "path":
        print(wt)
        return 0
    if cmd not in ("remove", "ensure"):
        sys.stderr.write("unknown command: {}\n".format(cmd))
        return 2

    if cmd == "ensure":
        try:
            os.makedirs(base, mode=0o700, exist_ok=True)
        except OSError as exc:
            # The shell had no `set -e`: a failing `mkdir -p` printed one
            # diagnostic and the run carried on, letting the following git/gh
            # call decide the exit code. Keep that — an occupied or unwritable
            # base must not become an unhandled traceback.
            sys.stderr.write("mkdir -p {} failed: {}\n".format(base, exc.strerror or exc))
    # After makedirs, not before: a check-then-create window is exactly what a
    # planted symlink would slip through (exist_ok accepts a link to a dir).
    problem = _guard_base(base)
    if problem:
        sys.stderr.write(problem + "\n")
        return 1

    if cmd == "remove":
        _git(["git", "worktree", "remove", "--force", wt])
        _rmtree_force(wt)
        _git(["git", "worktree", "prune"])
        print("removed {}".format(wt))
        return 0

    # Worktree mode when the current directory IS the target repo; else clone
    # mode. (Short-circuit like the shell: rev-parse only runs on a slug match.)
    cur = _gh(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).out.rstrip("\n")
    if cur == target and _git(["git", "rev-parse", "--is-inside-work-tree"]).rc == 0:
        if _loud(_git(["git", "fetch", "-q", "origin", "pull/{}/head".format(pr)])).rc != 0:
            sys.stderr.write("fetch pull/{}/head failed\n".format(pr))
            return 1
        sha = _loud(_git(["git", "rev-parse", "FETCH_HEAD"])).out.rstrip("\n")
        # A worktree's .git is a FILE (a gitdir pointer), unlike a clone's dir.
        if os.path.isfile(os.path.join(wt, ".git")):
            # Reuse: hard-reset the existing worktree to the current head.
            if _loud(_git(["git", "-C", wt, "reset", "-q", "--hard", sha])).rc == 0:
                _loud(_git(["git", "-C", wt, "clean", "-qfd"]))
        else:
            _git(["git", "worktree", "prune"])
            _rmtree_force(wt)
            if _loud(_git(["git", "worktree", "add", "-q", "--detach", wt, sha])).rc != 0:
                sys.stderr.write("worktree add failed\n")
                return 1
    else:
        # A clone's .git is a DIRECTORY (unlike a worktree's pointer file).
        if not os.path.isdir(os.path.join(wt, ".git")):
            _rmtree_force(wt)
            if os.path.lexists(wt):
                # The shell silenced `rm -rf` in the remove path and in worktree
                # mode, but NOT here: a cleanup that failed printed rm's
                # diagnostic before `gh repo clone` died on the occupied path.
                # _rmtree_force is unconditionally silent, so restore that one
                # breadcrumb — otherwise the "clone failed" line below states no
                # cause. The exit contract is unchanged: the clone still decides
                # the rc.
                sys.stderr.write("rm -rf {} failed (path still exists)\n".format(wt))
            if _loud(_gh(["gh", "repo", "clone", target, wt, "--", "-q"])).rc != 0:
                sys.stderr.write("clone failed\n")
                return 1
        if _loud(_git(["git", "-C", wt, "fetch", "-q", "origin", "pull/{}/head".format(pr)])).rc != 0:
            sys.stderr.write("fetch pull/{}/head failed\n".format(pr))
            return 1
        # Detached checkout so it never collides with a checkout of the branch.
        _git(["git", "-C", wt, "checkout", "-q", "--detach", "FETCH_HEAD"])
        if _loud(_git(["git", "-C", wt, "reset", "-q", "--hard", "FETCH_HEAD"])).rc == 0:
            _loud(_git(["git", "-C", wt, "clean", "-qfd"]))
    print(wt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
