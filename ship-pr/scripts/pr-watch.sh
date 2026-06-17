#!/usr/bin/env bash
# Emit a single line whenever a PR's CI / reviewer-bot / review-thread state
# changes *for real*. Built for the Monitor tool (run it with persistent: true):
# each printed line becomes one notification, so you are woken on the *transition*
# (CI finished, review posted, new comments, ready to merge) instead of on a timer.
#
# Debounced on purpose. GitHub recomputes mergeability after every push (flashing
# mergeStateStatus null/UNKNOWN) and CodeRabbit edits its comments mid-review
# (jittering the comment count and momentarily dropping its status check), and a
# transient read can briefly show unresolved=0/ready=true. Naively emitting on any
# string change wakes you on this churn — and a one-poll ready=true flash could
# even trip a premature merge. So this script:
#   - carries forward the last stable mergeStateStatus when it reads null/UNKNOWN
#   - treats an empty reviewer-bot check bucket as "pending" (bot vanishing
#     mid-review is not a change)
#   - keys the state on the PR head SHA, so a push is a real transition
#   - requires a candidate state to PERSIST across 2 consecutive polls before
#     emitting (so jitter and one-poll flashes never wake you), and never emits a
#     line at all while the snapshot is a degraded read (threads fetch failed)
#
# Usage (inside Monitor): pr-watch.sh [PR_NUMBER] [OWNER/REPO]
# Poll interval defaults to 10s; override with PR_WATCH_INTERVAL (seconds).
#
# Requires: gh, jq, and the sibling pr-state.sh.
set -uo pipefail

pr="${1:-}"
repo="${2:-${GH_REPO:-}}"
interval="${PR_WATCH_INTERVAL:-10}"
case "$interval" in ''|*[!0-9]*) interval=10 ;; esac   # integer seconds; bad value -> default (no busy-spin)
if [ -z "$pr" ]; then
  pr="$(gh pr view ${repo:+-R "$repo"} --json number -q .number 2>/dev/null)" || true
fi
dir="$(cd "$(dirname "$0")" && pwd)"

# One normalized state line. A transient degraded read (threads fetch failed)
# yields "" so we HOLD; a permanent error (no such PR, deleted/transferred) yields
# an "ERROR …" sentinel so the agent is woken instead of waiting forever.
# bot="" -> "pending".
snapshot() {
  "$dir/pr-state.sh" "$pr" "$repo" 2>/dev/null | jq -r '
    if (.error) then "ERROR \(.error)"
    elif (.threads_fetched == false) then ""
    else
      (.review_bot_checks | map(.bucket) | join("/")) as $bot
      | "fetched ci_pass=\(.ci_all_pass) ci_fail=\(.ci_failing|length) "
        + "bot=\(if ($bot=="") then "pending" else $bot end) "
        + "unresolved=\(.review_threads_unresolved) comments=\(.review_comment_count) "
        + "merge=\(.mergeStateStatus) head=\(.head) ready=\(.ready_to_merge)"
    end' 2>/dev/null
}

# The debounce/dedup KEY is the semantic state with the comment count stripped:
# `review_comment_count` jitters continuously while CodeRabbit edits its comments,
# so keying on it would starve a real transition (it could never hold for 2 polls).
# The comment count still rides along in the emitted line for context.
keyof() { printf '%s' "${1/comments=*merge=/merge=}"; }

emitted=""        # key of the last line we emitted
cand=""; cand_n=0 # current candidate key and how many consecutive polls it has held
last_merge="?"    # last non-null/UNKNOWN mergeStateStatus, for carry-forward

# Seed once so the first emission is the first real, debounced change.
seed="$(snapshot)"
if [ -n "$seed" ]; then
  emitted="$(keyof "$seed")"
  m="${seed#*merge=}"; m="${m%% *}"
  [ "$m" != "null" ] && [ "$m" != "UNKNOWN" ] && last_merge="$m"
fi

while true; do
  sleep "$interval"
  raw="$(snapshot)"
  [ -z "$raw" ] && continue                       # transient degraded read — hold

  case "$raw" in
    ERROR*) line="$raw" ;;                         # permanent error — wake on it
    *)
      merge="${raw#*merge=}"; merge="${merge%% *}"
      if [ "$merge" = "null" ] || [ "$merge" = "UNKNOWN" ]; then
        line="${raw/merge=$merge/merge=$last_merge}"   # carry forward last stable merge
      else
        line="$raw"; last_merge="$merge"
      fi ;;
  esac
  key="$(keyof "$line")"

  if [ "$key" = "$cand" ]; then
    cand_n=$((cand_n + 1))
  else
    cand="$key"; cand_n=1                          # new candidate — start its streak
  fi

  # Emit a state that has held for 2 polls and differs (semantically) from the last.
  if [ "$cand_n" -ge 2 ] && [ "$key" != "$emitted" ]; then
    echo "$line"
    emitted="$key"
  fi
done
