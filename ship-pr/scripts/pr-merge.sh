#!/usr/bin/env bash
# Land a PR safely and idempotently. This bundles the merge dance every ship-pr
# session hand-rolled: re-confirm the terminal gate, preserve the DCO sign-off,
# merge through the transient-retry wrapper, and verify the result so a flaky
# "EOF" or a local-checkout "Not possible to fast-forward" never looks like a
# failure (or causes a double-merge).
#
# Usage: pr-merge.sh <PR> [OWNER/REPO] [flags]
#   --subject S        squash commit subject (default: "<PR title> (#<PR>)")
#   --body B           squash commit body (a DCO Signed-off-by from the PR's
#                      commits is appended automatically if missing)
#   --strategy S       squash (default) | merge | rebase
#   --no-delete-branch keep the head branch
#   --force            skip the readiness gate (use ONLY when you have already
#                      confirmed state out-of-band; normally never pass this)
#
# Exit codes: 0 merged (or already merged); 3 refused (not ready); 1 error.
# Safety: REFUSES unless pr-state.sh reports ready_to_merge AND threads_fetched
# (a degraded snapshot can never green-light a merge). It runs against the remote
# (-R) so gh never tries to fast-forward your local checkout.
#
# Requires: gh (authenticated), jq, sibling pr-state.sh + gh-retry.sh.
set -uo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$dir/gh-retry.sh"

pr=""; repo="${GH_REPO:-}"; subject=""; body=""; strategy="squash"
delete_branch=1; force=0
while [ $# -gt 0 ]; do
  case "$1" in
    --subject) [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; subject="$2"; shift 2 ;;
    --body) [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; body="$2"; shift 2 ;;
    --strategy) [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; strategy="$2"; shift 2 ;;
    --no-delete-branch) delete_branch=0; shift ;;
    --force) force=1; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    */*) repo="$1"; shift ;;
    *) [ -z "$pr" ] && pr="$1" || repo="$1"; shift ;;
  esac
done

R=(); [ -n "$repo" ] && R=(-R "$repo")
if [ -z "$pr" ]; then
  pr="$(gh pr view "${R[@]+"${R[@]}"}" --json number -q .number 2>/dev/null)" || true
fi
if [ -z "$pr" ]; then
  echo "pr-merge: no PR (pass a number, and OWNER/REPO unless run inside the repo)" >&2
  exit 1
fi
if [ -n "$repo" ]; then
  owner="${repo%%/*}"; name="${repo##*/}"
else
  ownername="$(gh repo view --json owner,name -q '.owner.login + " " + .name' 2>/dev/null)" || true
  owner="${ownername%% *}"; name="${ownername##* }"
fi
slug="$owner/$name"

# Idempotent verify helper: prints MERGED commit oid (or empty) without changing anything.
merged_oid() {
  gh_retry gh pr view "$pr" -R "$slug" --json state,mergeCommit 2>/dev/null \
    | jq -r 'if .state == "MERGED" then (.mergeCommit.oid // "merged") else "" end' 2>/dev/null
}

# Already merged? Report success and stop (never re-merge).
oid="$(merged_oid)"
if [ -n "$oid" ]; then
  echo "already merged: $slug #$pr ($oid)"
  exit 0
fi

# --- Safety gate: re-confirm the terminal condition from a NON-degraded snapshot ---
if [ "$force" -ne 1 ]; then
  state_json="$(bash "$dir/pr-state.sh" "$pr" "$slug" 2>/dev/null)" || true
  ready="$(printf '%s' "$state_json" | jq -r '.ready_to_merge // false' 2>/dev/null)"
  if [ "$ready" != "true" ]; then
    echo "pr-merge: REFUSING — PR #$pr is not ready to merge." >&2
    printf '%s' "$state_json" | jq -r '
      "  threads_fetched: \(.threads_fetched)",
      "  ci_all_pass:     \(.ci_all_pass)   failing: \(.ci_failing)",
      "  review_bot:      \(.review_bot_checks|map(.bucket)|join("/"))",
      "  unresolved:      \(.review_threads_unresolved)",
      "  mergeable:       \(.mergeable) / \(.mergeStateStatus)"' 2>/dev/null >&2
    echo "  (if threads_fetched is false this is a transient read — retry; otherwise resolve the blocker)" >&2
    exit 3
  fi
  [ -z "$subject" ] && subject="$(printf '%s' "$state_json" | jq -r '.title' 2>/dev/null) (#$pr)"
fi
[ -z "$subject" ] && subject="$(gh_retry gh pr view "$pr" -R "$slug" --json title -q .title) (#$pr)"

# --- Preserve DCO: append a Signed-off-by trailer from the PR's commits if absent ---
if ! printf '%s' "$body" | grep -qi '^Signed-off-by:'; then
  commits="$(gh_retry gh api "repos/$slug/pulls/$pr/commits")" || true
  signoff="$(printf '%s' "$commits" \
    | jq -r '[ .[].commit.message | scan("(?m)^Signed-off-by:.*$") ] | unique | join("\n")' 2>/dev/null)" || true
  if [ -n "$signoff" ]; then
    if [ -n "$body" ]; then body="$body

$signoff"; else body="$signoff"; fi
  fi
fi

# --- Merge (remote-only via -R, through the transient-retry wrapper) ---
margs=(gh pr merge "$pr" -R "$slug" "--$strategy")
[ "$delete_branch" -eq 1 ] && margs+=(--delete-branch)
case "$strategy" in
  squash|merge) margs+=(--subject "$subject" --body "$body") ;;
esac
gh_retry "${margs[@]}" >/dev/null 2>&1 || true

# --- Idempotent verify-after: the merge is a server op; trust the resulting state,
#     not the merge call's exit code (it can fatal on local fast-forward yet land). ---
oid="$(merged_oid)"
if [ -n "$oid" ]; then
  echo "merged: $slug #$pr ($oid)"
  exit 0
fi
echo "pr-merge: merge did not land for $slug #$pr — re-run pr-state.sh and inspect." >&2
exit 1
