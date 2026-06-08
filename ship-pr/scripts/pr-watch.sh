#!/usr/bin/env bash
# Emit a single line whenever a PR's CI / reviewer-bot / review-thread state
# changes. Built for the Monitor tool (run it with persistent: true): each
# printed line becomes one notification, so you are woken on the *transition*
# (CI finished, review posted, new comments, ready to merge) instead of on a
# timer. It seeds silently — the first emission is the first real change.
#
# Usage (inside Monitor): pr-watch.sh [PR_NUMBER] [OWNER/REPO]
#
# Requires: gh, jq, and the sibling pr-state.sh.
set -uo pipefail

pr="${1:-}"
repo="${2:-${GH_REPO:-}}"
if [ -z "$pr" ]; then
  pr="$(gh pr view ${repo:+-R "$repo"} --json number -q .number 2>/dev/null)" || true
fi
dir="$(cd "$(dirname "$0")" && pwd)"

snapshot() {
  "$dir/pr-state.sh" "$pr" "$repo" 2>/dev/null | jq -r '
    if .error then "error"
    else "ci_pass=\(.ci_all_pass) ci_fail=\(.ci_failing|length) bot=\(.review_bot_checks|map(.bucket)|join("/")) unresolved=\(.review_threads_unresolved) comments=\(.review_comment_count) merge=\(.mergeStateStatus) ready=\(.ready_to_merge)"
    end' 2>/dev/null
}

prev="$(snapshot)"
while true; do
  sleep 30
  cur="$(snapshot)"
  # Skip transient read failures so they don't flap the state line.
  [ -z "$cur" ] && continue
  [ "$cur" = "error" ] && continue
  if [ "$cur" != "$prev" ]; then
    echo "$cur"
    prev="$cur"
  fi
done
