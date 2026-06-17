#!/usr/bin/env bash
# gh_retry — run a gh / gh api / git command, retrying ONLY transient failures
# (GitHub's intermittent "EOF", 5xx, timeouts, connection resets). The GitHub API
# drops connections often enough that every ship-pr session hit at least one
# "Post https://api.github.com/graphql: EOF"; without this the agent hand-rolls
# `until CMD; do sleep N; done` loops, and a transient read can silently degrade a
# snapshot. Centralizing the retry here means the bundled scripts are resilient
# and the agent never has to babysit a flaky read.
#
# Use two ways:
#   - source it, then call the function:   source gh-retry.sh; out=$(gh_retry gh api ...)
#   - run it as a command wrapper:         out=$(gh-retry.sh gh api ...)
#
# Tunables (env): GH_RETRY_TRIES (default 3), GH_RETRY_DELAY (base seconds, default 2;
# backoff is delay*attempt). A NON-transient failure returns immediately (no point
# retrying a 404 or a bad query). `gh pr checks` legitimately exits non-zero while
# printing valid JSON, so success is judged by "non-empty output AND no transient
# error on stderr", not by exit code alone.
#
# Requires: bash, the command being wrapped (gh/git).

gh_retry() {
  local tries="${GH_RETRY_TRIES:-3}" delay="${GH_RETRY_DELAY:-2}" i out rc err
  local transient='EOF|timed? ?out|50[234]|connection reset|connection refused|temporarily unavailable|i/o timeout|TLS handshake'
  err="$(mktemp 2>/dev/null || echo "/tmp/gh_retry.$$")"
  i=1
  while [ "$i" -le "$tries" ]; do
    out="$("$@" 2>"$err")"; rc=$?
    # Non-empty output IS success — even with a non-zero exit (gh pr checks exits
    # non-zero while printing valid JSON) and even if stderr carries benign noise
    # that happens to match a transient word. A dropped connection yields EMPTY
    # output, which is what we actually retry on.
    if [ -n "$out" ]; then
      rm -f "$err"; printf '%s' "$out"; return 0
    fi
    if grep -qiE "$transient" "$err" 2>/dev/null; then
      sleep "$((delay * i))"
      i="$((i + 1))"
      continue
    fi
    break   # empty output with a non-transient (or no) error — retrying won't help
  done
  rm -f "$err"
  printf '%s' "$out"
  return "${rc:-1}"
}

# When executed directly (not sourced), act as a command wrapper.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  gh_retry "$@"
fi
