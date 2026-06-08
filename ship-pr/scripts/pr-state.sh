#!/usr/bin/env bash
# One-shot health snapshot of a GitHub PR as compact JSON: CI checks,
# reviewer-bot status checks, review-thread counts (incl. unresolved),
# mergeability, and a single ready_to_merge boolean.
#
# Usage: pr-state.sh [PR_NUMBER] [OWNER/REPO]
#   PR_NUMBER  defaults to the current branch's PR (requires running in the repo)
#   OWNER/REPO defaults to $GH_REPO, else the repo of the current directory
#
# Requires: gh (authenticated), jq.
set -uo pipefail

pr="${1:-}"
repo="${2:-${GH_REPO:-}}"
R=()
[ -n "$repo" ] && R=(-R "$repo")

if [ -z "$pr" ]; then
  pr="$(gh pr view "${R[@]}" --json number -q .number 2>/dev/null)" || true
fi
if [ -z "$pr" ]; then
  echo '{"error":"no PR (pass a PR number, and OWNER/REPO unless run inside the repo)"}'
  exit 0
fi

# Owner/name for the GraphQL query.
if [ -n "$repo" ]; then
  owner="${repo%%/*}"
  name="${repo##*/}"
else
  ownername="$(gh repo view --json owner,name -q '.owner.login + " " + .name' 2>/dev/null)" || true
  owner="${ownername%% *}"
  name="${ownername##* }"
fi

# Status checks. bucket: pass|fail|pending|skipping|cancel. `gh pr checks` exits
# non-zero when checks fail/pend but still prints JSON, so ignore the exit code.
checks="$(gh pr checks "$pr" "${R[@]}" --json name,bucket,state 2>/dev/null)" || true
[ -z "$checks" ] && checks='[]'

prview="$(gh pr view "$pr" "${R[@]}" --json mergeable,mergeStateStatus,reviewDecision,state,title,headRefName 2>/dev/null)" || true
[ -z "$prview" ] && prview='{}'

threads="$(gh api graphql -F owner="$owner" -F repo="$name" -F pr="$pr" -f query='
query($owner:String!,$repo:String!,$pr:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      reviewThreads(first:100){nodes{isResolved isOutdated comments(first:1){totalCount}}}
    }
  }
}' 2>/dev/null)" || true
[ -z "$threads" ] && threads='{}'

# Status-check names treated as "review bot" rather than CI. Extend as needed.
botre='coderabbit|sourcery|codium|qodo|greptile|ellipsis'

jq -n \
  --argjson checks "$checks" \
  --argjson pr "$prview" \
  --argjson threads "$threads" \
  --arg botre "$botre" \
  --arg num "$pr" '
  ($checks // []) as $c |
  ($c | map(select(.name | ascii_downcase | test($botre)))) as $bot |
  ($c | map(select(.name | ascii_downcase | test($botre) | not))) as $ci |
  (($threads.data.repository.pullRequest.reviewThreads.nodes) // []) as $t |
  ($ci | (length > 0) and all(.bucket == "pass" or .bucket == "skipping")) as $cipass |
  ($bot | all(.bucket == "pass" or .bucket == "skipping")) as $botpass |
  ($t | map(select(.isResolved == false)) | length) as $unresolved |
  {
    pr: ($num | tonumber),
    state: $pr.state,
    title: $pr.title,
    mergeable: $pr.mergeable,
    mergeStateStatus: $pr.mergeStateStatus,
    reviewDecision: $pr.reviewDecision,
    checks: ($c | map({name, bucket})),
    ci_all_pass: $cipass,
    ci_failing: ($ci | map(select(.bucket == "fail")) | map(.name)),
    ci_pending: ($ci | map(select(.bucket == "pending")) | map(.name)),
    review_bot_checks: ($bot | map({name, bucket})),
    review_threads_total: ($t | length),
    review_threads_unresolved: $unresolved,
    review_comment_count: ($t | map(.comments.totalCount) | add // 0),
    ready_to_merge: (
      $cipass and $botpass and ($unresolved == 0)
      and ($pr.mergeable == "MERGEABLE")
      and ($pr.mergeStateStatus == "CLEAN")
    )
  }'
