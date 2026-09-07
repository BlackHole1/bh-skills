#!/usr/bin/env python3
"""pr_reply — reply inside a review thread to dispute a false positive, robustly.

The body is read from stdin (or a file), NEVER passed inline — that sidesteps
the shell quoting hazards that broke real sessions ("(eval): unmatched '" on an
apostrophe or backtick in the reasoning). It is read as RAW BYTES and written
through untouched, so a Chinese or emoji body survives a non-UTF-8 stdin (see
read_stdin_bytes). Replying is a POST (not idempotent),
so this does NOT blindly retry: it posts once, and only on a transient error
re-checks whether the reply actually landed before trying again — so a flaky
"EOF" never double-posts.

Usage:
  pr_comments prints each finding's "reply-to id"; pass it here.
  echo "..." | pr_reply.py <PR> <REPLY_TO_ID> [OWNER/REPO]
  pr_reply.py <PR> <REPLY_TO_ID> [OWNER/REPO] --body-file note.md

Convention: address the bot so it re-evaluates, e.g. start the body with
"@coderabbitai " and give a concise, code-grounded reason. This script does not
add the mention for you — write it into the body.

Exit: 0 posted (or already replied); 1 error; 2 usage.
Requires: gh (authenticated), sibling gh_retry.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from typing import List, Sequence

from gh_retry import force_utf8, gh_retry, loads_json, run_command

# Transient errors worth a verified retry. This per-script list is deliberately
# SHORTER than gh_retry's (no "connection refused", "i/o timeout", "TLS
# handshake"): a POST is not idempotent, so only the failure modes seen to
# swallow an ACK after a landed write qualify — keep it verbatim.
TRANSIENT_RE = re.compile(
    r"EOF|timed? ?out|50[234]|connection reset|temporarily unavailable",
    re.IGNORECASE,
)

# Injectable seams (tests monkeypatch these):
#   _run   — a single, NEVER-retried invocation (the POST; plain `gh repo view`)
#   _retry — retried reads (whoami, the reply listing) via gh_retry
_run = run_command
_retry = gh_retry
_sleep = time.sleep


def read_stdin_bytes() -> bytes:
    """Read stdin as RAW BYTES, exactly like the shell's `cat > "$body_file"`.

    Deliberate deviation from the "read sys.stdin.read()" porting rule, and the
    reason is the same one that put the body on stdin in the first place:
    force_utf8() can only reconfigure stdout/stderr, so sys.stdin keeps
    locale.getpreferredencoding() with errors="strict". On a Windows console or
    pipe (cp1252 / cp936 / cp932) a text read either mangles a Chinese or emoji
    reply into mojibake that then gets POSTed, or kills the script outright with
    an unhandled UnicodeDecodeError. Byte transparency is the whole point here,
    so the body is never decoded — it goes stdin -> temp file -> POST untouched.
    """
    stream = sys.stdin
    if stream is None:  # no stdin at all (pythonw and friends)
        return b""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        return buffer.read()
    # No binary view (a text stream someone swapped in): take the text and
    # encode it as UTF-8, which is what a byte-transparent read would have got.
    data = stream.read()
    return data if isinstance(data, bytes) else data.encode("utf-8", "replace")


def _jq_interp(value: object) -> str:
    """Render a JSON value the way jq string interpolation does: strings raw,
    everything else as its JSON text (null -> "null", 123 -> "123")."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def parse_concatenated_arrays(text: str) -> List[object]:
    """Flatten `gh api --paginate` output: one JSON array per page, concatenated
    back to back. The shell did `jq -s 'add // []'`; here a raw_decode loop
    walks the top-level values. Like jq erroring out (its stderr was silenced),
    any malformed value — or a top-level non-array — yields []."""
    decoder = json.JSONDecoder()
    items: List[object] = []
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        try:
            value, idx = decoder.raw_decode(text, idx)
        except ValueError:
            return []
        if not isinstance(value, list):
            return []
        items.extend(value)
    return items


def find_reply_url(comments: Sequence[object], reply_to: int, me: str) -> str:
    """The LAST matching reply's html_url where in_reply_to_id == reply_to and
    user.login == me ('' when none) — jq's `[...] | (.[-1].html_url // empty)`."""
    url = ""
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if comment.get("in_reply_to_id") == reply_to and login == me:
            url = comment.get("html_url") or ""
    return url


def reply_exists(slug: str, pr: str, reply_to: int, me: str) -> str:
    """Existing reply by me to this comment? (idempotency / post-failure
    verification). Without a readable author we cannot verify — return ''."""
    if not me:
        return ""
    res = _retry(["gh", "api", "repos/{}/pulls/{}/comments".format(slug, pr), "--paginate"])
    return find_reply_url(parse_concatenated_arrays(res.out), reply_to, me)


def main(argv: Sequence[str]) -> int:
    force_utf8()

    pr = ""
    reply_to = ""
    repo = os.environ.get("GH_REPO", "")
    body_file = ""
    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--body-file":
            if i + 1 >= len(args):
                sys.stderr.write("missing value for --body-file\n")
                return 2
            body_file = args[i + 1]
            i += 2
            continue
        if arg.startswith("-"):
            sys.stderr.write("unknown flag: {}\n".format(arg))
            return 2
        if "/" in arg:
            repo = arg
        elif not pr:
            pr = arg
        elif not reply_to:
            reply_to = arg
        else:
            repo = arg
        i += 1

    if not pr or not reply_to:
        sys.stderr.write(
            "usage: pr_reply.py <PR> <REPLY_TO_ID> [OWNER/REPO] [--body-file F]"
            "   (body on stdin otherwise)\n"
        )
        return 2
    if not re.fullmatch(r"[0-9]+", reply_to):
        sys.stderr.write(
            "pr-reply: REPLY_TO_ID must be a numeric comment id"
            " (the 'reply-to id' from pr_comments.py)\n"
        )
        return 2
    reply_to_id = int(reply_to)

    if repo:
        owner = repo.split("/")[0]
        name = repo.split("/")[-1]
    else:
        res = _run(
            ["gh", "repo", "view", "--json", "owner,name", "-q", '.owner.login + " " + .name']
        )
        ownername = res.out.rstrip("\n") if res.rc == 0 else ""
        owner = ownername.split(" ")[0]
        name = ownername.split(" ")[-1]
    slug = "{}/{}".format(owner, name)

    # Resolve the body into a file so a retry can re-post without re-reading
    # stdin. The temp file is removed on every exit path (the shell's trap).
    cleanup = ""
    try:
        if not body_file:
            fd, body_file = tempfile.mkstemp(prefix="pr-reply-")
            cleanup = body_file
            # Binary mode: the stdin bytes land in the file untouched — no
            # decode/encode round trip, and no Windows \n -> \r\n rewriting.
            with os.fdopen(fd, "wb") as handle:
                handle.write(read_stdin_bytes())
        try:
            empty = os.path.getsize(body_file) == 0
        except OSError:
            empty = True
        if empty:
            sys.stderr.write(
                "pr-reply: empty body (write the reply to stdin or pass --body-file)\n"
            )
            return 2

        res = _retry(["gh", "api", "user", "-q", ".login"])
        me = res.out.rstrip("\n") if res.rc == 0 else ""

        ex = reply_exists(slug, pr, reply_to_id, me)
        if ex:
            print("already replied: {}".format(ex))
            return 0

        # Post once; retry ONLY a transient failure, and only after confirming
        # the prior attempt did not actually land (so EOF-after-success never
        # double-posts).
        err = ""
        attempt = 1
        while attempt <= 3:
            res = _run(
                [
                    "gh",
                    "api",
                    "repos/{}/pulls/{}/comments/{}/replies".format(slug, pr, reply_to),
                    "-F",
                    "body=@{}".format(body_file),
                ]
            )
            err = res.err
            # BOM-tolerant like jq (loads_json): failing to read the ACK of a
            # POST that DID land is what sends us round the retry loop, so a
            # stray BOM must never be the reason we consider re-posting.
            try:
                resp = loads_json(res.out)
            except ValueError:
                resp = None
            # jq -e '.id' succeeds unless .id is null or false (0 is truthy).
            resp_id = resp.get("id") if isinstance(resp, dict) else None
            if resp_id is not None and resp_id is not False:
                print(
                    "replied: {}  (id {})".format(
                        _jq_interp(resp.get("html_url")), _jq_interp(resp_id)
                    )
                )
                return 0
            # The POST failed to ACK. Before even considering a retry, let a
            # committed-but-unacked write become visible (GitHub read-after-write
            # lag) and re-check — so a dropped response on a reply that DID land
            # never causes a double-post. Without a readable author (me empty)
            # we cannot verify, so we never retry a POST blind.
            if not me:
                break
            _sleep(2 * attempt)
            ex = reply_exists(slug, pr, reply_to_id, me)
            if ex:
                print("replied (confirmed after a transient error): {}".format(ex))
                return 0
            if TRANSIENT_RE.search(err):
                attempt += 1
                continue
            break
        sys.stderr.write(
            "pr-reply: failed to post reply to comment {} in {} #{}:\n".format(
                reply_to, slug, pr
            )
        )
        sys.stderr.write(err)
        return 1
    finally:
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
