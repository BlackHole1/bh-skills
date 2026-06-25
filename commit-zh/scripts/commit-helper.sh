#!/usr/bin/env bash
#
# commit-helper.sh — one place that owns every mutating git step for the
# commit-zh / commit-en skills, so the model never has to chain `git add`,
# branch creation, and `git commit` by hand (each chained command is a
# round-trip and a chance to get the order wrong).
#
# Two subcommands:
#
#   prepare
#       Make the working tree ready to describe and read back a compact,
#       token-lean snapshot. If nothing is staged yet, stage everything
#       (`git add -A`) — running the skill is the user's go-ahead, so an
#       empty index means "commit what I have", not "give up". Prints a
#       single STATE line the model can parse, then the staged stat + diff.
#
#   commit <branch-slug>
#       Read a commit message from stdin and land exactly one commit. If
#       HEAD is on a protected branch (main/master by default) or detached,
#       first carve off a new branch named from <branch-slug> so the
#       protected branch is never committed to directly. Creating a branch
#       from the current commit keeps the staged/unstaged split byte-for-byte
#       intact (verified: `git switch -c` touches neither index nor worktree),
#       so there is no stash dance and nothing can be lost. Prints a COMMITTED
#       line with the resulting branch and short SHA.
#
# Env knobs (all optional):
#   COMMIT_PROTECTED_BRANCHES   space-separated branch names to auto-branch
#                               away from (default: "main master"). Set empty
#                               to allow committing straight onto any branch.
#   COMMIT_DIFF_MAX_LINES       how many diff lines `prepare` prints (default 500).
#
set -euo pipefail

PROTECTED="${COMMIT_PROTECTED_BRANCHES-main master}"
DIFF_CAP="${COMMIT_DIFF_MAX_LINES:-500}"

die() { printf '%s\n' "$*" >&2; exit 1; }

in_repo()      { git rev-parse --is-inside-work-tree >/dev/null 2>&1; }
current_branch() { git branch --show-current 2>/dev/null || true; }
has_commits()  { git rev-parse --verify -q HEAD >/dev/null 2>&1; }
worktree_dirty() { [ -n "$(git status --porcelain 2>/dev/null)" ]; }

staged_count() {
  # number of paths in the index that differ from HEAD
  git diff --staged --name-only 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' '
}

is_protected() {
  local b="$1"
  # an empty branch name means detached HEAD — always carve off a branch
  # there, otherwise the commit would be stranded with no ref pointing at it
  [ -z "$b" ] && return 0
  local p
  for p in $PROTECTED; do [ "$b" = "$p" ] && return 0; done
  return 1
}

cmd_prepare() {
  if ! in_repo; then
    echo "STATE repo=no"
    return 0
  fi

  local auto_staged=no
  if [ "$(staged_count)" -eq 0 ] && worktree_dirty; then
    git add -A
    auto_staged=yes
  fi

  local branch protected=no unborn=no sc
  branch=$(current_branch)
  is_protected "$branch" && protected=yes
  has_commits || unborn=yes
  sc=$(staged_count)

  echo "STATE repo=yes branch=${branch:-<detached>} protected=${protected} unborn=${unborn} auto_staged=${auto_staged} staged_files=${sc}"
  echo

  if [ "$sc" -eq 0 ]; then
    echo "NO_CHANGES (working tree is clean — nothing to commit)"
    return 0
  fi

  echo "## Staged stat"
  git diff --staged --stat --stat-count="$DIFF_CAP"
  echo

  local total
  total=$(git diff --staged 2>/dev/null | wc -l | tr -d ' ')
  echo "## Staged diff (first ${DIFF_CAP} of ${total} lines; read more with: git diff --staged -- <path>)"
  git diff --staged 2>/dev/null | head -n "$DIFF_CAP" || true
}

cmd_commit() {
  in_repo || die "Not a git repository."
  local slug="${1-}"

  [ "$(staged_count)" -eq 0 ] && die "Nothing staged to commit (run 'prepare' first)."

  local branch created=""
  branch=$(current_branch)

  # Auto-branch only when there is real history to protect. On an unborn
  # branch (fresh repo, no commits) there is nothing to push off of, so let
  # the first commit land where it is rather than orphaning the base name.
  if is_protected "$branch" && has_commits; then
    [ -n "$slug" ] || die "On protected branch '${branch:-<detached>}' but no branch slug was given."
    local name="$slug" n=2
    while git show-ref --verify -q "refs/heads/$name"; do
      name="${slug}-${n}"; n=$((n + 1))
    done
    git switch -c "$name" >/dev/null 2>&1 \
      || git checkout -b "$name" >/dev/null 2>&1 \
      || die "Failed to create branch '$name'."
    created="$name"
  fi

  git commit -s -F - >/dev/null

  local sha now
  sha=$(git rev-parse --short HEAD)
  now=$(current_branch)
  if [ -n "$created" ]; then
    echo "COMMITTED branch=${now:-<detached>} created_branch=${created} sha=${sha}"
  else
    echo "COMMITTED branch=${now:-<detached>} sha=${sha}"
  fi
}

case "${1-}" in
  prepare) shift; cmd_prepare "$@" ;;
  commit)  shift; cmd_commit "$@" ;;
  *)       die "usage: commit-helper.sh {prepare | commit <branch-slug>}" ;;
esac
