#!/usr/bin/env bash
# Manage an isolated git worktree (or clone) that mirrors a PR's head commit, so
# ship-pr can read / diagnose / fix PR code without touching the user's working
# tree, index, or branch. It is always (re)synced to the *current* PR head, so
# triage and fixes act on the real PR code — never a stale local branch.
#
# Usage:
#   pr-worktree.sh ensure <PR> [OWNER/REPO]   # create-or-refresh; prints the path
#   pr-worktree.sh remove <PR> [OWNER/REPO]   # tear down
#   pr-worktree.sh path   <PR> [OWNER/REPO]   # print the path only (no changes)
#
# Uses a detached worktree when run inside a local clone of the target repo
# (cheap — shares the object store); otherwise clones the repo into a temp dir.
# The head is checked out DETACHED so it never collides with the user's own
# checkout of the same branch. Pushing a fix back is the caller's job:
#   same-repo PR: git -C "$wt" push origin HEAD:<headRefName>
#   fork PR:      push to the fork's repo (needs write access to the fork)
#
# Requires: gh (authenticated), git.
set -uo pipefail

cmd="${1:-}"; pr="${2:-}"; repo="${3:-${GH_REPO:-}}"
if [ -z "$cmd" ] || [ -z "$pr" ]; then
  echo "usage: pr-worktree.sh ensure|remove|path <PR> [OWNER/REPO]" >&2
  exit 2
fi
# gh repo view takes the repo as a positional arg (not -R), so use the explicit
# OWNER/REPO directly when given, otherwise infer from the current directory.
if [ -n "$repo" ]; then
  target="$repo"
else
  target="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
fi
if [ -z "$target" ]; then
  echo "cannot resolve repo (pass OWNER/REPO, or run inside the target repo)" >&2
  exit 1
fi
slug="${target//\//-}"
base="${TMPDIR:-/tmp}/ship-pr-worktrees"
wt="$base/${slug}-pr-${pr}"

case "$cmd" in
  path)
    echo "$wt"; exit 0 ;;
  remove)
    git worktree remove --force "$wt" 2>/dev/null
    rm -rf "$wt" 2>/dev/null
    git worktree prune 2>/dev/null
    echo "removed $wt"; exit 0 ;;
  ensure) : ;;
  *)
    echo "unknown command: $cmd" >&2; exit 2 ;;
esac

mkdir -p "$base"

# Worktree mode when the current directory IS the target repo; else clone mode.
cur="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
if [ "$cur" = "$target" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git fetch -q origin "pull/${pr}/head" || { echo "fetch pull/${pr}/head failed" >&2; exit 1; }
  sha="$(git rev-parse FETCH_HEAD)"
  if [ -f "$wt/.git" ]; then
    # Reuse: hard-reset the existing worktree to the current head.
    git -C "$wt" reset -q --hard "$sha" && git -C "$wt" clean -qfd
  else
    git worktree prune 2>/dev/null; rm -rf "$wt" 2>/dev/null
    git worktree add -q --detach "$wt" "$sha" || { echo "worktree add failed" >&2; exit 1; }
  fi
else
  if [ ! -d "$wt/.git" ]; then
    rm -rf "$wt"
    gh repo clone "$target" "$wt" -- -q || { echo "clone failed" >&2; exit 1; }
  fi
  git -C "$wt" fetch -q origin "pull/${pr}/head" || { echo "fetch pull/${pr}/head failed" >&2; exit 1; }
  git -C "$wt" checkout -q --detach FETCH_HEAD 2>/dev/null
  git -C "$wt" reset -q --hard FETCH_HEAD && git -C "$wt" clean -qfd
fi
echo "$wt"
