#!/usr/bin/env bash
#
# pr-helper.sh collects everything the create-pr skill needs to draft a pull
# request in one call, instead of a dozen shell round-trips that each recompute
# the same merge-base. Read-only apart from the language record.
#
#   prepare [zh|en] [base-branch]
#
#       Language: an explicit `zh` or `en` wins and is recorded in this repo's
#       .git/config, so later runs in the same repo default to it. With no
#       argument the recorded value wins, falling back to `en`. The key is
#       shared with the commit skill, so setting it once covers both.
#
#       Base: an explicit base-branch overrides the repo's default branch.
#
#       Prints one STATE line the model can parse, then the existing PR (if
#       any), the branch's commits, the diffstat, a capped branch diff, and the
#       repo's PR template.
#
# STATE fields worth knowing:
#   repo_slug / head_sha   build GitHub permalinks from these:
#                          https://github.com/<repo_slug>/blob/<head_sha>/<path>#L1-L9
#   merge_base             the base commit; use it in follow-up git commands
#   dirty / commits        whether there is work to commit before the PR
#
# Env knobs (all optional):
#   PR_DIFF_MAX_LINES   how many diff lines to print (default 600).
#   SKILLS_LANG_KEY     git config key holding the language record
#                       (default: skills.lang), shared with commit.
#
set -euo pipefail

DIFF_CAP="${PR_DIFF_MAX_LINES:-600}"
case "$DIFF_CAP" in
  *[!0-9]*|'') DIFF_CAP=600 ;;
esac
LANG_KEY="${SKILLS_LANG_KEY:-skills.lang}"

die() { printf '%s\n' "$*" >&2; exit 1; }

in_repo() { git rev-parse --is-inside-work-tree >/dev/null 2>&1; }

# resolve_lang [zh|en] -> prints the language to write the body in.
resolve_lang() {
  local want="${1-}" rec=""
  case "$want" in
    zh|en)
      git config --local "$LANG_KEY" "$want" >/dev/null 2>&1 \
        || die "failed to persist language preference ($LANG_KEY=$want)"
      printf '%s' "$want"
      return 0
      ;;
    "") ;;
    *) die "unknown language '$want' (expected zh or en)" ;;
  esac
  rec=$(git config --local --get "$LANG_KEY" 2>/dev/null || true)
  case "$rec" in
    zh|en) printf '%s' "$rec" ;;
    *)     printf 'en' ;;
  esac
}

find_pr_template() {
  local f
  for f in .github/PULL_REQUEST_TEMPLATE.md .github/pull_request_template.md \
           PULL_REQUEST_TEMPLATE.md pull_request_template.md \
           docs/PULL_REQUEST_TEMPLATE.md docs/pull_request_template.md; do
    if [ -f "$f" ]; then printf '%s' "$f"; return 0; fi
  done
  find .github/PULL_REQUEST_TEMPLATE -name '*.md' -type f 2>/dev/null | head -1
}

cmd_prepare() {
  if ! in_repo; then
    echo "STATE repo=no"
    return 0
  fi

  local msg_lang base_arg
  msg_lang=$(resolve_lang "${1-}")
  base_arg="${2-}"

  local branch detached=no
  branch=$(git branch --show-current 2>/dev/null || true)
  [ -n "$branch" ] || detached=yes

  # One gh call for both facts, so an unauthenticated gh costs one timeout
  # rather than two.
  local meta="" repo_slug="unknown" default_base="main" gh_ok=yes
  meta=$(gh repo view --json nameWithOwner,defaultBranchRef \
           -q '.nameWithOwner + " " + .defaultBranchRef.name' 2>/dev/null || true)
  if [ -n "$meta" ]; then
    repo_slug="${meta%% *}"
    default_base="${meta##* }"
  else
    gh_ok=no
  fi

  local base="${base_arg:-$default_base}"

  local mb=""
  mb=$(git merge-base HEAD "origin/$base" 2>/dev/null \
       || git merge-base HEAD "$base" 2>/dev/null \
       || true)
  if [ -z "$mb" ]; then
    mb=$(git rev-list --max-parents=0 HEAD 2>/dev/null | tail -1 || true)
  fi
  local rng="${mb:+$mb..}HEAD"

  local upstream ahead
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{push}' 2>/dev/null || true)
  if [ -n "$upstream" ]; then
    ahead=$(git rev-list --count '@{push}..HEAD' 2>/dev/null || echo 0)
  else
    upstream=none
    ahead=n/a
  fi

  local dirty=no
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then dirty=yes; fi

  local on_base=no
  if [ -n "$branch" ] && [ "$branch" = "$base" ]; then on_base=yes; fi

  local ncommits head_sha
  ncommits=$(git rev-list --count "$rng" 2>/dev/null || echo 0)
  head_sha=$(git rev-parse HEAD 2>/dev/null || echo "")

  echo "STATE repo=yes branch=${branch:-<detached>} detached=${detached} lang=${msg_lang} base=${base} default_base=${default_base} on_base=${on_base} repo_slug=${repo_slug} head_sha=${head_sha} merge_base=${mb:-none} upstream=${upstream} ahead=${ahead} dirty=${dirty} commits=${ncommits} gh=${gh_ok}"
  echo

  echo "## Existing PR (NONE means create a new one)"
  gh pr view --json number,state,isDraft,title,url,baseRefName,body 2>/dev/null || echo "NONE"
  echo

  echo "## Commits on this branch (oldest first, subjects and bodies)"
  git log --reverse --format='>>> %h %s%n%b' "$rng" 2>/dev/null || true
  echo

  echo "## Files changed"
  git diff --stat "$rng" 2>/dev/null || true
  echo

  local total
  total=$(git diff "$rng" 2>/dev/null | wc -l | tr -d ' ')
  echo "## Branch diff (first ${DIFF_CAP} of ${total} lines; read more with: git diff ${mb:-HEAD}..HEAD -- <path>)"
  git diff "$rng" 2>/dev/null | head -n "$DIFF_CAP" || true
  echo

  local tmpl
  tmpl=$(find_pr_template || true)
  if [ -n "$tmpl" ]; then
    echo "## PR template ($tmpl), its sections are required"
    cat "$tmpl"
  else
    echo "## PR template: NONE"
  fi
}

case "${1-}" in
  prepare) shift; cmd_prepare "$@" ;;
  *)       die "usage: pr-helper.sh prepare [zh|en] [base-branch]" ;;
esac
