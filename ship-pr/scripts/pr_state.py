#!/usr/bin/env python3
"""One-shot health snapshot of a GitHub PR as compact JSON: CI checks,
reviewer-bot status checks, review-thread counts (incl. unresolved),
mergeability, and a single ready_to_merge boolean.

Usage: pr_state.py [PR_NUMBER] [OWNER/REPO]
  PR_NUMBER  defaults to the current branch's PR (requires running in the repo)
  OWNER/REPO defaults to $GH_REPO, else the repo of the current directory

Transient GitHub API failures (EOF/5xx) are retried through the sibling
gh_retry wrapper so a flaky read never degrades the snapshot. gh_retry judges
success by non-empty stdout and inspects stderr itself to detect transients,
so the wrapped calls do not silence their own stderr.

Requires: Python 3.9+, gh (authenticated) on PATH.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from gh_retry import RunResult, force_utf8, gh_retry, loads_json, run_command

Runner = Callable[[Sequence[str]], RunResult]

NO_PR_MESSAGE = "no PR (pass a PR number, and OWNER/REPO unless run inside the repo)"
# Printed verbatim as one compact line so consumers can match it byte-for-byte.
NO_PR_LINE = (
    '{"error":"no PR (pass a PR number, and OWNER/REPO unless run inside the repo)"}'
)

# Status-check names treated as "review bot" rather than CI. Extend as needed.
BOT_RE = re.compile(r"coderabbit|sourcery|codium|qodo|greptile|ellipsis")

# Exact GraphQL document the shell script sent (including the leading newline),
# passed as the value of `-f query=...`.
THREADS_QUERY = (
    "\n"
    "query($owner:String!,$repo:String!,$pr:Int!){\n"
    "  repository(owner:$owner,name:$repo){\n"
    "    pullRequest(number:$pr){\n"
    "      reviewThreads(first:100){nodes{isResolved isOutdated comments(first:1){totalCount}}}\n"
    "    }\n"
    "  }\n"
    "}"
)

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _gh(cmd: Sequence[str]) -> RunResult:
    """Retried gh boundary (module-level so tests can monkeypatch it)."""
    return gh_retry(cmd)


def _gh_plain(cmd: Sequence[str]) -> RunResult:
    """Plain gh boundary for the cheap resolution lookups the shell ran
    without the retry wrapper."""
    return run_command(cmd)


def _ascii_downcase(s: str) -> str:
    """jq's ascii_downcase: fold A-Z only (no Unicode case folding)."""
    return s.translate(_ASCII_LOWER)


def _capture(res: RunResult) -> str:
    """Shell command-substitution capture: stdout minus trailing newlines,
    kept regardless of the exit code (the shell's `x="$(cmd)" || true`)."""
    return res.out.rstrip("\n")


def resolve_pr(repo: str, run: Optional[Runner] = None) -> str:
    """Number of the current branch's PR, or "" when there is none."""
    run = run or _gh_plain
    cmd = ["gh", "pr", "view"]
    if repo:
        cmd += ["-R", repo]
    cmd += ["--json", "number", "-q", ".number"]
    return _capture(run(cmd))


def resolve_owner_name(repo: str, run: Optional[Runner] = None) -> Tuple[str, str]:
    """Owner/name for the GraphQL query: derived textually from OWNER/REPO when
    given (first segment / last path segment), else one `gh repo view` call
    (no retry, matching the shell), split on the first/last space."""
    if repo:
        return repo.split("/", 1)[0], repo.rsplit("/", 1)[-1]
    run = run or _gh_plain
    ownername = _capture(
        run(
            [
                "gh",
                "repo",
                "view",
                "--json",
                "owner,name",
                "-q",
                '.owner.login + " " + .name',
            ]
        )
    )
    return ownername.split(" ", 1)[0], ownername.rsplit(" ", 1)[-1]


def _threads_nodes(doc: Any) -> Any:
    """`.data.repository.pullRequest.reviewThreads.nodes` with jq indexing
    semantics: indexing null or a missing key yields null (None); indexing a
    non-object raises, like the jq program would."""
    node = doc
    for key in ("data", "repository", "pullRequest", "reviewThreads", "nodes"):
        if node is None:
            return None
        node = node.get(key)
    return node


def collect_state(
    pr: Any,
    repo: str = "",
    gh: Optional[Runner] = None,
    plain: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Build the snapshot dict exactly as the shell's jq program did (same
    field order, same values)."""
    gh = gh or _gh
    plain = plain or _gh_plain
    r_flags = ["-R", repo] if repo else []

    # Owner/name for the GraphQL query.
    owner, name = resolve_owner_name(repo, run=plain)

    # Status checks. bucket: pass|fail|pending|skipping|cancel. `gh pr checks`
    # exits non-zero when checks fail/pend but still prints JSON, so the output
    # is taken regardless of the exit code.
    checks_text = _capture(
        gh(["gh", "pr", "checks", str(pr), *r_flags, "--json", "name,bucket,state"])
    )
    # loads_json, not json.loads: gh output carrying a leading BOM parsed fine
    # under jq, and rejecting it here would raise out of collect_state and turn
    # a readable snapshot into an error (or, for pr_watch, a held read).
    checks = loads_json(checks_text) if checks_text else []
    if checks is None:  # jq's ($checks // [])
        checks = []

    prview_text = _capture(
        gh(
            [
                "gh",
                "pr",
                "view",
                str(pr),
                *r_flags,
                "--json",
                "mergeable,mergeStateStatus,reviewDecision,state,title,headRefName,headRefOid",
            ]
        )
    )
    prview = loads_json(prview_text) if prview_text else {}
    if prview is None:
        prview = {}

    threads_text = _capture(
        gh(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                "owner=" + owner,
                "-F",
                "repo=" + name,
                "-F",
                "pr=" + str(pr),
                "-f",
                "query=" + THREADS_QUERY,
            ]
        )
    )

    # A failed/partial threads fetch (e.g. a transient GraphQL EOF) must NOT be
    # read as "0 unresolved" — that would fabricate ready_to_merge=true and
    # green-light a merge while real unresolved review threads are hidden. A
    # genuinely empty PR still returns a non-null (possibly empty) nodes array,
    # so this only trips on an actual fetch failure, never on a real
    # zero-thread PR.
    nodes = _threads_nodes(loads_json(threads_text)) if threads_text else None
    threads_ok = nodes is not None
    t = nodes if threads_ok else []

    bot = [c for c in checks if BOT_RE.search(_ascii_downcase(c["name"]))]
    ci = [c for c in checks if not BOT_RE.search(_ascii_downcase(c["name"]))]

    # Vacuously true when the repo has no non-bot CI checks (all() over an
    # empty list is True, like jq's `all` over an empty array). No length>0
    # requirement: a repo can legitimately have zero CI, and ready_to_merge
    # still gates on mergeStateStatus==CLEAN, which GitHub reports as
    # UNSTABLE/BLOCKED while checks are pending or failing.
    cipass = all(c.get("bucket") in ("pass", "skipping") for c in ci)
    botpass = all(c.get("bucket") in ("pass", "skipping") for c in bot)
    unresolved = sum(1 for n in t if n.get("isResolved") is False)

    return {
        # Deliberate deviation: jq's `tonumber` accepted "42.5" and emitted a
        # snapshot with a float PR; int() rejects it and main turns that into a
        # non-zero exit with nothing on stdout. A fractional PR number is not a
        # thing, and no caller can act on `"pr": 42.5` — failing loudly beats
        # publishing a nonsense snapshot. Unreachable in practice (the number is
        # either gh-resolved or typed by a human).
        "pr": int(pr),
        "state": prview.get("state"),
        "title": prview.get("title"),
        "mergeable": prview.get("mergeable"),
        "mergeStateStatus": prview.get("mergeStateStatus"),
        "head": prview.get("headRefOid"),
        "reviewDecision": prview.get("reviewDecision"),
        "checks": [{"name": c.get("name"), "bucket": c.get("bucket")} for c in checks],
        "ci_check_count": len(ci),
        "ci_all_pass": cipass,
        "ci_failing": [c.get("name") for c in ci if c.get("bucket") == "fail"],
        "ci_pending": [c.get("name") for c in ci if c.get("bucket") == "pending"],
        "review_bot_checks": [
            {"name": c.get("name"), "bucket": c.get("bucket")} for c in bot
        ],
        "threads_fetched": threads_ok,
        "review_threads_total": len(t) if threads_ok else None,
        "review_threads_unresolved": unresolved if threads_ok else None,
        "review_comment_count": (
            sum((n.get("comments") or {}).get("totalCount") or 0 for n in t)
            if threads_ok
            else None
        ),
        "ready_to_merge": (
            threads_ok
            and cipass
            and botpass
            and unresolved == 0
            and prview.get("mergeable") == "MERGEABLE"
            and prview.get("mergeStateStatus") == "CLEAN"
        ),
    }


def main(argv: Sequence[str]) -> int:
    force_utf8()
    pr = argv[0] if len(argv) > 0 else ""
    repo = (argv[1] if len(argv) > 1 else "") or os.environ.get("GH_REPO", "")
    if not pr:
        pr = resolve_pr(repo)
    if not pr:
        print(NO_PR_LINE)
        return 0
    try:
        state = collect_state(pr, repo)
    except Exception as exc:  # malformed gh output — the shell's jq died here too
        # Deliberate deviation: jq exited 5 on this, we exit 2. The port keeps
        # one non-zero code for "couldn't build a snapshot", and no consumer
        # branches on 5 — they branch on whether stdout held a snapshot, which
        # in both versions it does not. Unreachable outside malformed gh output.
        sys.stderr.write("pr-state: {}\n".format(exc))
        return 2
    print(json.dumps(state, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
