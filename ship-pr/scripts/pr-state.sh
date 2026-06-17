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

# Retry transient GitHub API failures (EOF/5xx) so a flaky read never degrades the
# snapshot. gh_retry captures stderr to detect transients, so wrapped calls drop
# their own `2>/dev/null`.
dir="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$dir/gh-retry.sh"

pr="${1:-}"
repo="${2:-${GH_REPO:-}}"
# Build the -R flag as an array. The `${R[@]+...}` guard keeps the expansion
# safe under `set -u` on macOS's Bash 3.2, where a bare "${R[@]}" on an empty
# array aborts with "R[@]: unbound variable".
R=()
[ -n "$repo" ] && R=(-R "$repo")

if [ -z "$pr" ]; then
  pr="$(gh pr view "${R[@]+"${R[@]}"}" --json number -q .number 2>/dev/null)" || true
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
checks="$(gh_retry gh pr checks "$pr" "${R[@]+"${R[@]}"}" --json name,bucket,state)" || true
[ -z "$checks" ] && checks='[]'

prview="$(gh_retry gh pr view "$pr" "${R[@]+"${R[@]}"}" --json mergeable,mergeStateStatus,reviewDecision,state,title,headRefName,headRefOid)" || true
[ -z "$prview" ] && prview='{}'

threads="$(gh_retry gh api graphql -F owner="$owner" -F repo="$name" -F pr="$pr" -f query='
query($owner:String!,$repo:String!,$pr:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      reviewThreads(first:100){nodes{isResolved isOutdated comments(first:1){totalCount}}}
    }
  }
}')" || true

# A failed/partial threads fetch (e.g. a transient GraphQL EOF) must NOT be read
# as "0 unresolved" — that would fabricate ready_to_merge=true and green-light a
# merge while real unresolved review threads are hidden. A genuinely empty PR
# still returns a non-null (possibly empty) nodes array, so this only trips on an
# actual fetch failure, never on a real zero-thread PR.
threads_ok=true
if [ -z "$threads" ] \
   || ! printf '%s' "$threads" | jq -e '.data.repository.pullRequest.reviewThreads.nodes != null' >/dev/null 2>&1; then
  threads_ok=false
fi
[ -z "$threads" ] && threads='{}'

# Status-check names treated as "review bot" rather than CI. Extend as needed.
botre='coderabbit|sourcery|codium|qodo|greptile|ellipsis'

jq -n \
  --argjson checks "$checks" \
  --argjson pr "$prview" \
  --argjson threads "$threads" \
  --argjson threads_ok "$threads_ok" \
  --arg botre "$botre" \
  --arg num "$pr" '
  ($checks // []) as $c |
  ($c | map(select(.name | ascii_downcase | test($botre)))) as $bot |
  ($c | map(select(.name | ascii_downcase | test($botre) | not))) as $ci |
  (($threads.data.repository.pullRequest.reviewThreads.nodes) // []) as $t |
  # Vacuously true when the repo has no non-bot CI checks (jq all over an empty
  # array is true). No length>0 requirement: a repo can legitimately have zero
  # CI, and ready_to_merge still gates on mergeStateStatus==CLEAN, which GitHub
  # reports as UNSTABLE/BLOCKED while checks are pending or failing.
  ($ci | all(.bucket == "pass" or .bucket == "skipping")) as $cipass |
  ($bot | all(.bucket == "pass" or .bucket == "skipping")) as $botpass |
  ($t | map(select(.isResolved == false)) | length) as $unresolved |
  {
    pr: ($num | tonumber),
    state: $pr.state,
    title: $pr.title,
    mergeable: $pr.mergeable,
    mergeStateStatus: $pr.mergeStateStatus,
    head: $pr.headRefOid,
    reviewDecision: $pr.reviewDecision,
    checks: ($c | map({name, bucket})),
    ci_check_count: ($ci | length),
    ci_all_pass: $cipass,
    ci_failing: ($ci | map(select(.bucket == "fail")) | map(.name)),
    ci_pending: ($ci | map(select(.bucket == "pending")) | map(.name)),
    review_bot_checks: ($bot | map({name, bucket})),
    threads_fetched: $threads_ok,
    review_threads_total: (if $threads_ok then ($t | length) else null end),
    review_threads_unresolved: (if $threads_ok then $unresolved else null end),
    review_comment_count: (if $threads_ok then ($t | map(.comments.totalCount) | add // 0) else null end),
    ready_to_merge: (
      $threads_ok and $cipass and $botpass and ($unresolved == 0)
      and ($pr.mergeable == "MERGEABLE")
      and ($pr.mergeStateStatus == "CLEAN")
    )
  }'
