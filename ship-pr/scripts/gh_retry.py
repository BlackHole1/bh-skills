#!/usr/bin/env python3
"""gh_retry — run a gh / gh api / git command, retrying ONLY transient failures
(GitHub's intermittent "EOF", 5xx, timeouts, connection resets). The GitHub API
drops connections often enough that every ship-pr session hit at least one
"Post https://api.github.com/graphql: EOF"; without this the agent hand-rolls
`until CMD; do sleep N; done` loops, and a transient read can silently degrade a
snapshot. Centralizing the retry here means the bundled scripts are resilient
and the agent never has to babysit a flaky read.

Use two ways:
  - import it:              from gh_retry import gh_retry
                            res = gh_retry(["gh", "api", ...]); res.out
  - run it as a wrapper:    python3 gh_retry.py gh api ...

Tunables (env): GH_RETRY_TRIES (default 3), GH_RETRY_DELAY (base seconds,
default 2; backoff is delay*attempt). A NON-transient failure returns
immediately (no point retrying a 404 or a bad query). `gh pr checks`
legitimately exits non-zero while printing valid JSON, so success is judged by
"non-empty output", not by exit code alone.

It is also where the ship-pr scripts get the two tiny helpers they all need
alongside the retry itself: force_utf8() and loads_json() (BOM-tolerant JSON).

Requires: Python 3.9+, the command being wrapped (gh/git) on PATH.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, NamedTuple, Optional, Sequence

TRANSIENT_RE = re.compile(
    r"EOF|timed? ?out|50[234]|connection reset|connection refused"
    r"|temporarily unavailable|i/o timeout|TLS handshake",
    re.IGNORECASE,
)


def force_utf8() -> None:
    """Make stdout/stderr UTF-8 so emoji-bearing PR content survives Windows
    consoles (whose default encoding is often cp1252/GBK, not UTF-8)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def loads_json(text: str) -> Any:
    """json.loads, but tolerant of a leading UTF-8 BOM the way jq was.

    jq skips a BOM at the start of its input; json.loads raises on it. Every
    JSON seam in ship-pr feeds a safety decision (is this PR merged? is this
    seed baseline usable? was the threads read honest?), and a parse failure
    silently DOWNGRADES that decision — a cleanup run skips, a watcher throws
    away its TOCTOU seed, a snapshot degrades. A stray BOM (a proxy, a
    re-encoded response, a fixture written by a Windows editor) must never be
    what makes that difference. Exactly one BOM is stripped, like jq; anything
    else raises the same ValueError json.loads would."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text)


class RunResult(NamedTuple):
    out: str
    err: str
    rc: int


def run_command(
    cmd: Sequence[str],
    *,
    stdin: Optional[str] = None,
    cwd: Optional[str] = None,
) -> RunResult:
    """Run cmd without a shell, decoding output as UTF-8. A missing binary maps
    to rc 127 (like a shell), so "gh not installed" reads as an ordinary
    non-transient failure instead of a Python traceback."""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            input=stdin,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return RunResult("", "{}: command not found".format(cmd[0]), 127)
    except OSError as exc:
        return RunResult("", "{}: {}".format(cmd[0], exc), 126)
    return RunResult(proc.stdout or "", proc.stderr or "", proc.returncode)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def gh_retry(
    cmd: Sequence[str],
    *,
    tries: Optional[int] = None,
    delay: Optional[int] = None,
    runner: Callable[..., RunResult] = run_command,
    sleep: Callable[[float], None] = time.sleep,
    stdin: Optional[str] = None,
) -> RunResult:
    """Run cmd, retrying transient failures. Semantics:

    - Non-empty stdout IS success — even with a non-zero exit (gh pr checks
      exits non-zero while printing valid JSON) and even if stderr carries
      benign noise that happens to match a transient word. A dropped connection
      yields EMPTY output, which is what we actually retry on. rc is 0 then.
    - Empty output + transient-looking stderr: sleep delay*attempt and retry.
    - Empty output + non-transient (or no) error: return immediately —
      retrying a 404 or a bad query won't help.
    """
    if tries is None:
        tries = _env_int("GH_RETRY_TRIES", 3)
    if delay is None:
        delay = _env_int("GH_RETRY_DELAY", 2)

    out, err, rc = "", "", 1
    attempt = 1
    while attempt <= tries:
        out, err, rc = runner(cmd, stdin=stdin)
        if out:
            return RunResult(out, err, 0)
        if TRANSIENT_RE.search(err or ""):
            sleep(delay * attempt)
            attempt += 1
            continue
        break  # empty output with a non-transient (or no) error — give up
    return RunResult(out, err, rc)


def main(argv: Sequence[str]) -> int:
    force_utf8()
    if not argv:
        sys.stderr.write("usage: gh_retry.py CMD [ARGS...]\n")
        return 2
    res = gh_retry(list(argv))
    sys.stdout.write(res.out)
    if not res.out and res.err:
        # The shell version swallowed the final stderr; surfacing it costs
        # nothing (consumers parse stdout) and makes failures debuggable.
        sys.stderr.write(res.err)
    return res.rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
