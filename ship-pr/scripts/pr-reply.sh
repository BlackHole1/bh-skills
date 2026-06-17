#!/usr/bin/env bash
# Reply inside a review thread to dispute a false positive, robustly. The body is
# read from stdin (or a file), NEVER passed inline — that sidesteps the shell
# quoting hazards that broke real sessions ("(eval): unmatched '" on an apostrophe
# or backtick in the reasoning). Replying is a POST (not idempotent), so this does
# NOT blindly retry: it posts once, and only on a transient error re-checks whether
# the reply actually landed before trying again — so a flaky "EOF" never double-posts.
#
# Usage:
#   pr-comments.sh prints each finding's "reply-to id"; pass it here.
#   echo "..." | pr-reply.sh <PR> <REPLY_TO_ID> [OWNER/REPO]
#   pr-reply.sh <PR> <REPLY_TO_ID> [OWNER/REPO] --body-file note.md
#
# Convention: address the bot so it re-evaluates, e.g. start the body with
# "@coderabbitai " and give a concise, code-grounded reason. This script does not
# add the mention for you — write it into the body.
#
# Exit: 0 posted (or already replied); 1 error; 2 usage.
# Requires: gh (authenticated), jq, sibling gh-retry.sh.
set -uo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$dir/gh-retry.sh"

pr=""; reply_to=""; repo="${GH_REPO:-}"; body_file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --body-file) [ $# -ge 2 ] || { echo "missing value for --body-file" >&2; exit 2; }; body_file="$2"; shift 2 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    */*) repo="$1"; shift ;;
    *) if [ -z "$pr" ]; then pr="$1"; elif [ -z "$reply_to" ]; then reply_to="$1"; else repo="$1"; fi; shift ;;
  esac
done
if [ -z "$pr" ] || [ -z "$reply_to" ]; then
  echo "usage: pr-reply.sh <PR> <REPLY_TO_ID> [OWNER/REPO] [--body-file F]   (body on stdin otherwise)" >&2
  exit 2
fi
case "$reply_to" in ''|*[!0-9]*)
  echo "pr-reply: REPLY_TO_ID must be a numeric comment id (the 'reply-to id' from pr-comments.sh)" >&2
  exit 2 ;;
esac

if [ -n "$repo" ]; then
  owner="${repo%%/*}"; name="${repo##*/}"
else
  ownername="$(gh repo view --json owner,name -q '.owner.login + " " + .name' 2>/dev/null)" || true
  owner="${ownername%% *}"; name="${ownername##* }"
fi
slug="$owner/$name"

# Resolve the body into a file so a retry can re-post without re-reading stdin.
cleanup=""
if [ -z "$body_file" ]; then
  body_file="$(mktemp)"; cleanup="$body_file"; cat > "$body_file"
fi
trap '[ -n "$cleanup" ] && rm -f "$cleanup"' EXIT
if [ ! -s "$body_file" ]; then
  echo "pr-reply: empty body (write the reply to stdin or pass --body-file)" >&2
  exit 2
fi

me="$(gh_retry gh api user -q .login 2>/dev/null)" || me=""

# Existing reply by me to this comment? (idempotency / post-failure verification)
reply_exists() {
  [ -z "$me" ] && return 1
  gh_retry gh api "repos/$slug/pulls/$pr/comments" --paginate \
    | jq -s 'add // []' 2>/dev/null \
    | jq -r --arg me "$me" --argjson rid "$reply_to" \
        '[ .[] | select((.in_reply_to_id == $rid) and (.user.login == $me)) ] | (.[-1].html_url // empty)' 2>/dev/null
}

ex="$(reply_exists)" || true
if [ -n "$ex" ]; then
  echo "already replied: $ex"
  exit 0
fi

# Post once; retry ONLY a transient failure, and only after confirming the prior
# attempt did not actually land (so EOF-after-success never double-posts).
err="$(mktemp)"; trap '[ -n "$cleanup" ] && rm -f "$cleanup"; rm -f "$err"' EXIT
attempt=1
while [ "$attempt" -le 3 ]; do
  resp="$(gh api "repos/$slug/pulls/$pr/comments/$reply_to/replies" -F body=@"$body_file" 2>"$err")" || true
  if printf '%s' "$resp" | jq -e '.id' >/dev/null 2>&1; then
    printf '%s' "$resp" | jq -r '"replied: \(.html_url)  (id \(.id))"'
    exit 0
  fi
  # The POST failed to ACK. Before even considering a retry, let a committed-but-
  # unacked write become visible (GitHub read-after-write lag) and re-check — so a
  # dropped response on a reply that DID land never causes a double-post. Without a
  # readable author ($me empty) we cannot verify, so we never retry a POST blind.
  [ -z "$me" ] && break
  sleep "$((2 * attempt))"
  ex="$(reply_exists)" || true
  if [ -n "$ex" ]; then
    echo "replied (confirmed after a transient error): $ex"
    exit 0
  fi
  if grep -qiE 'EOF|timed? ?out|50[234]|connection reset|temporarily unavailable' "$err"; then
    attempt="$((attempt + 1))"; continue
  fi
  break
done
echo "pr-reply: failed to post reply to comment $reply_to in $slug #$pr:" >&2
cat "$err" >&2
exit 1
