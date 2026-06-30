#!/usr/bin/env bash
# Return the user's LOCAL checkout to a clean base after a PR has merged.
#
# ship-pr does all its babysitting in an isolated worktree and merges remotely
# (pr-merge.sh runs with -R), so it deliberately never touches the user's own
# checkout, branch, or index. That leaves one nicety undone: after the PR lands,
# the user is often still sitting on the now-merged branch with a stale local
# base. This is the one sanctioned moment to touch their checkout — AFTER the
# merge is confirmed — and tidy it the way `gh pr merge --delete-branch` would
# for a local merge (the cleanup pr-merge.sh skips by being remote-only): switch
# off the merged branch to the base branch, fast-forward it, delete the merged
# local branch.
#
# It is conservative on purpose. Every step is gated, and on anything it can't do
# safely it changes NOTHING and exits 3 (a deliberate skip, not an error) with a
# one-line reason — so an unattended run can never lose work or yank the rug from
# under a user who's mid-task.
#
# Usage: pr-local-cleanup.sh <PR> [OWNER/REPO] [--base BRANCH] [--dry-run]
#   --base BRANCH   override the base branch (default: the PR's baseRefName)
#   --dry-run       print what it would do, change nothing (read-only — no fetch,
#                    no writes; it still reads the PR's state to decide)
#
# Exit codes:
#   0  cleaned up, or already clean (nothing left to do)
#   3  skipped: a precondition wasn't met — not the target repo / PR not MERGED /
#      dirty or mid-operation tree / detached HEAD / on an unrelated branch / no
#      base branch to return to / a fork branch it can't match. The reason is
#      printed. This means "left as-is", NOT a failure.
#   2  usage error
#   1  unexpected error
#
# Requires: gh (authenticated), git, jq, sibling gh-retry.sh.
set -uo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$dir/gh-retry.sh"

pr=""; repo="${GH_REPO:-}"; base_override=""; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) [ $# -ge 2 ] || { echo "missing value for --base" >&2; exit 2; }; base_override="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) echo "usage: pr-local-cleanup.sh <PR> [OWNER/REPO] [--base BRANCH] [--dry-run]"; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    */*) repo="$1"; shift ;;
    *) [ -z "$pr" ] && pr="$1" || repo="$1"; shift ;;
  esac
done

# Build the -R flag as an array; the ${R[@]+...} guard keeps the expansion safe
# under `set -u` on macOS's Bash 3.2 (a bare "${R[@]}" on an empty array aborts).
R=()
[ -n "$repo" ] && R=(-R "$repo")
if [ -z "$pr" ]; then
  pr="$(gh pr view "${R[@]+"${R[@]}"}" --json number -q .number 2>/dev/null)" || true
fi
if [ -z "$pr" ]; then
  echo "usage: pr-local-cleanup.sh <PR> [OWNER/REPO] [--base BRANCH] [--dry-run]" >&2
  exit 2
fi

skip() { echo "SKIPPED: $*"; exit 3; }

# Which remote points at the PR's repo? Don't assume "origin": fork workflows
# clone the canonical repo as `upstream` (origin = the user's fork), and people
# rename remotes. Resolve it once — prefer the base branch's configured upstream,
# else the remote whose URL matches the slug, else origin, else the sole remote.
resolve_remote() {
  local r url cfg only n
  cfg="$(git config --get "branch.$base.remote" 2>/dev/null || true)"
  if [ -n "$cfg" ] && git remote 2>/dev/null | grep -Fxq "$cfg"; then echo "$cfg"; return 0; fi
  for r in $(git remote 2>/dev/null); do
    url="$(git remote get-url "$r" 2>/dev/null || true)"
    case "$url" in
      *[:/]"$slug"|*[:/]"$slug".git|*[:/]"$slug"/) echo "$r"; return 0 ;;
    esac
  done
  if git remote 2>/dev/null | grep -Fxq origin; then echo origin; return 0; fi
  only="$(git remote 2>/dev/null)"; n="$(printf '%s\n' "$only" | grep -c .)"
  [ "$n" = "1" ] && echo "$only"
}

# Move to branch $1: use the local branch, else create it tracking the remote.
switch_to() {
  if git show-ref --verify --quiet "refs/heads/$1"; then
    git switch "$1" >/dev/null 2>&1 || git checkout "$1" >/dev/null 2>&1
  elif [ -n "$remote" ] && git show-ref --verify --quiet "refs/remotes/$remote/$1"; then
    git switch -c "$1" --track "$remote/$1" >/dev/null 2>&1 || git checkout -b "$1" --track "$remote/$1" >/dev/null 2>&1
  else
    return 1
  fi
}

# --- Gate: we must be inside a git checkout of the PR's repo ---
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || skip "not inside a git working tree — nothing local to tidy up."

# `gh repo view` (no -R) reports the repo of the current directory's remote.
cur="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
[ -z "$cur" ] && skip "this directory has no resolvable GitHub repo — leaving the local side alone."
# GitHub repo slugs are case-insensitive, so compare lower-cased — a caller
# passing a different casing than gh's canonical owner/repo is still a match.
if [ -n "$repo" ] && [ "$(printf '%s' "$cur" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$repo" | tr '[:upper:]' '[:lower:]')" ]; then
  skip "current repo ($cur) isn't the PR's repo ($repo) — leaving your checkout untouched."
fi
slug="$cur"

# --- Gate: the PR must actually be MERGED (never delete a branch for an open PR) ---
prj="$(gh_retry gh pr view "$pr" -R "$slug" --json state,headRefName,baseRefName,headRefOid,isCrossRepository)" || true
[ -z "$prj" ] && { echo "pr-local-cleanup: could not read PR #$pr (transient?) — not touching local branches." >&2; exit 1; }
state="$(printf '%s' "$prj" | jq -r '.state // empty')"
head="$(printf '%s' "$prj" | jq -r '.headRefName // empty')"
base="$(printf '%s' "$prj" | jq -r '.baseRefName // empty')"
headoid="$(printf '%s' "$prj" | jq -r '.headRefOid // empty')"
cross="$(printf '%s' "$prj" | jq -r '.isCrossRepository // false')"
[ -n "$base_override" ] && base="$base_override"
[ -z "$base" ] && skip "couldn't resolve the base branch for #$pr."
[ "$state" = "MERGED" ] || skip "PR #$pr is $state, not MERGED — local branch left in place."

remote="$(resolve_remote)"

# --- Gate: the working tree must be pristine ---
# "Clean" means what the user means by "no changes": nothing staged, unstaged, or
# untracked, and no half-finished operation we'd be moving off of. The advice is
# split so the caller can relay a remedy that actually works — `git stash` (no -u)
# does NOT clear untracked files, so naming it for an untracked-only tree loops.
porcelain="$(git status --porcelain 2>/dev/null)"
if [ -n "$porcelain" ]; then
  if printf '%s\n' "$porcelain" | grep -qv '^??'; then
    skip "working tree has uncommitted changes — commit or stash them first, then I can tidy up."
  else
    skip "working tree has untracked files — commit them, 'git stash -u', or remove them first, then I can tidy up."
  fi
fi
gd="$(git rev-parse --git-dir 2>/dev/null)"
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG rebase-merge rebase-apply; do
  [ -e "$gd/$marker" ] && skip "a git operation ($marker) is in progress — leaving your checkout alone."
done

# --- Gate: a named branch we recognise (the merged branch, or the base) ---
curbranch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[ -z "$curbranch" ] && skip "HEAD is detached — not auto-switching. (#$pr is merged.)"

on_head=0; on_base=0
[ "$curbranch" = "$head" ] && on_head=1
[ "$curbranch" = "$base" ] && on_base=1

do_delete=1
if [ "$cross" = "true" ]; then
  # Fork PR: headRefName lives on the fork, so a local branch of that name is not
  # reliably the merged branch. Do only the safe half (return to base + pull) and
  # never delete a branch we can't prove is the one that merged.
  do_delete=0
  on_head=0
fi

if [ "$on_head" -ne 1 ] && [ "$on_base" -ne 1 ]; then
  if [ "$cross" = "true" ]; then
    skip "you're on '$curbranch'; #$pr merged from a fork and can't be matched to a local branch. Switch to '$base' yourself if you want a pull. (#$pr is merged.)"
  else
    skip "you're on '$curbranch', neither the merged branch ('$head') nor the base ('$base') — not moving you. (#$pr is merged.)"
  fi
fi

local_head_exists=0
git show-ref --verify --quiet "refs/heads/$head" && local_head_exists=1

# --- Dry run: report intent, touch nothing (read-only — no fetch, no writes) ---
if [ "$dry" -eq 1 ]; then
  echo "DRY-RUN — $slug #$pr is merged; would:"
  [ "$on_head" -eq 1 ] && echo "  - switch '$curbranch' -> '$base'"
  echo "  - fast-forward '$base'"
  if [ "$do_delete" -eq 1 ] && [ "$local_head_exists" -eq 1 ] && [ "$head" != "$base" ]; then
    echo "  - delete local branch '$head' (only if proven fully merged; otherwise keep it)"
  fi
  exit 0
fi

# --- Prove the local branch is fully merged before we force-delete it ---
# Squash-merge makes `git branch -d` always cry "not fully merged", so deleting
# needs -D (force). Force is only safe once we've shown the local tip is contained
# in the exact commit GitHub merged: equal to it, or an ancestor of it (e.g. ship-pr
# pushed fixes past the user's stale local copy). If the local tip has commits the
# merge never saw — work the user committed but didn't push — we KEEP the branch.
# `unverified` distinguishes "couldn't check" (offline / no matching remote) from
# "checked and the branch is genuinely ahead", so the kept-branch message is honest.
safe_to_delete=0; unverified=0
if [ "$do_delete" -eq 1 ] && [ "$local_head_exists" -eq 1 ] && [ "$head" != "$base" ]; then
  localsha="$(git rev-parse --verify --quiet "refs/heads/$head" 2>/dev/null || true)"
  if [ -n "$headoid" ] && [ "$localsha" = "$headoid" ]; then
    safe_to_delete=1                                   # exact match — no network needed
  else
    # Local tip differs: could be behind the merged tip (safe) or ahead (unsafe).
    # Fetch the merge's commit so merge-base can settle which. If we can't fetch
    # it, we can't prove safety, so we keep the branch and say so.
    merged_ref=""; attempt=1
    while [ -n "$remote" ] && [ "$attempt" -le 3 ]; do
      if git fetch -q "$remote" "pull/$pr/head" 2>/dev/null; then
        merged_ref="$(git rev-parse --verify --quiet FETCH_HEAD 2>/dev/null || true)"
        [ -n "$merged_ref" ] && break
      fi
      attempt=$((attempt + 1))
    done
    if [ -z "$merged_ref" ]; then
      unverified=1
    elif [ -n "$localsha" ] && git merge-base --is-ancestor "$localsha" "$merged_ref" 2>/dev/null; then
      safe_to_delete=1
    fi
  fi
fi

# --- Act ---
did=()

# 1) Step off the merged branch (you can't delete the branch you're on).
if [ "$on_head" -eq 1 ]; then
  if switch_to "$base"; then did+=("switched to '$base'")
  else skip "couldn't switch to base '$base' (no local branch and no $remote/$base) — left on '$curbranch'. (#$pr is merged.)"; fi
fi

# 2) Fast-forward the base. --ff-only so we never create a merge commit or pick a
#    fight with a conflict in the user's checkout; a diverged base is reported.
#    Pull from the RESOLVED remote, not the branch's configured upstream — a
#    non-origin checkout or a base we just recreated may have no upstream set.
#    An already-up-to-date pull adds nothing — keeps a benign rerun a true no-op.
if [ -n "$remote" ]; then
  pull_out="$(git pull --ff-only "$remote" "$base" 2>&1)"; pull_rc=$?
else
  pull_out="no remote resolved"; pull_rc=1
fi
case "$pull_out" in
  *"Already up to date"*|*"Already up-to-date"*) : ;;
  *)
    if [ "$pull_rc" -eq 0 ]; then did+=("fast-forwarded '$base'")
    else did+=("left '$base' as-is (couldn't fast-forward: ${pull_out%%$'\n'*})"); fi ;;
esac

# 3) Delete the merged local branch — only when proven safe above.
if [ "$do_delete" -eq 1 ] && [ "$local_head_exists" -eq 1 ] && [ "$head" != "$base" ]; then
  if [ "$safe_to_delete" -eq 1 ]; then
    if git branch -D "$head" >/dev/null 2>&1; then did+=("deleted merged local branch '$head'")
    else did+=("could not delete '$head'"); fi
  elif [ "$unverified" -eq 1 ]; then
    did+=("kept '$head' — couldn't verify it's fully merged (left as-is; delete it yourself once you're sure)")
  else
    did+=("kept '$head' — it has commits the merge never saw (nothing lost; delete it yourself once you're sure)")
  fi
fi

echo "Post-merge local cleanup for $slug #$pr:"
if [ ${#did[@]} -eq 0 ]; then
  echo "  - nothing to do (already on '$base', merged branch gone)"
else
  for d in ${did[@]+"${did[@]}"}; do echo "  - $d"; done
fi
exit 0
