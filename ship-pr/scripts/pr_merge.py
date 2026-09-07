#!/usr/bin/env python3
"""pr_merge — land a PR safely and idempotently.

This bundles the merge dance every ship-pr session hand-rolled: re-confirm the
terminal gate, preserve the DCO sign-off, merge through the transient-retry
wrapper, and verify the result so a flaky "EOF" or a local-checkout "Not
possible to fast-forward" never looks like a failure (or causes a double-merge).

Usage: pr_merge.py <PR> [OWNER/REPO] [flags]
  --subject S        squash commit subject (default: "<PR title> (#<PR>)")
  --body B           squash commit body (a DCO Signed-off-by from the PR's
                     commits is appended automatically if missing)
  --strategy S       squash (default) | merge | rebase
  --no-delete-branch keep the head branch
  --force            skip the readiness gate (use ONLY when you have already
                     confirmed state out-of-band; normally never pass this)

Exit codes: 0 merged (or already merged); 3 refused (not ready); 1 error;
2 usage.
Safety: REFUSES unless the pr_state snapshot reports ready_to_merge AND
threads_fetched (a degraded snapshot can never green-light a merge). It runs
against the remote (-R) so gh never tries to fast-forward your local checkout.

Requires: Python 3.9+, gh (authenticated), sibling gh_retry.py + pr_state.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from gh_retry import RunResult, force_utf8, gh_retry, loads_json, run_command
from pr_state import collect_state

Runner = Callable[[Sequence[str]], RunResult]

# The shell's `grep -qi '^Signed-off-by:'`: case-insensitive, anchored at the
# start of any line of the body.
SIGNOFF_RE = re.compile(r"^Signed-off-by:", re.IGNORECASE | re.MULTILINE)
# jq's `scan("(?m)^Signed-off-by:.*$")`. jq drives Oniguruma with Perl syntax,
# where (?m) is Perl's multiline (^/$ match at line boundaries) and `.` still
# stops at a newline — exactly Python's re.MULTILINE. Case-SENSITIVE here, like
# the jq program (only the body pre-check was case-insensitive).
SIGNOFF_SCAN_RE = re.compile(r"^Signed-off-by:.*$", re.MULTILINE)

NO_PR_MESSAGE = "pr-merge: no PR (pass a number, and OWNER/REPO unless run inside the repo)"
REFUSAL_HINT = (
    "  (if threads_fetched is false this is a transient read — retry;"
    " otherwise resolve the blocker)"
)


def _gh(cmd: Sequence[str]) -> RunResult:
    """Retried gh boundary (module-level so tests can monkeypatch it)."""
    return gh_retry(cmd)


def _gh_plain(cmd: Sequence[str]) -> RunResult:
    """Plain gh boundary for the cheap PR/repo resolution lookups the shell ran
    without the retry wrapper."""
    return run_command(cmd)


def _capture(res: RunResult) -> str:
    """Shell command-substitution capture: stdout minus trailing newlines, kept
    regardless of the exit code (the shell's `x="$(cmd)" || true`)."""
    return res.out.rstrip("\n")


def _jq_interp(value: Any) -> str:
    """Render a JSON value the way jq string interpolation does: strings raw,
    everything else as its COMPACT JSON text (null -> "null", ["a","b"] ->
    '["a","b"]' with no spaces). Also matches `jq -r` for the scalars this
    script reads out (.title, the merged-commit oid)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _jq_join(values: Any, sep: str) -> str:
    """jq's `join`: null elements become "", numbers/booleans stringify, arrays
    and objects raise (jq errors out, aborting the whole program)."""
    if not isinstance(values, list):
        raise TypeError("Cannot iterate over {}".format(type(values).__name__))
    parts: List[str] = []
    for value in values:
        if value is None:
            parts.append("")
        elif isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (bool, int, float)):
            parts.append(_jq_interp(value))
        else:
            raise TypeError("Cannot join with {}".format(type(value).__name__))
    return sep.join(parts)


def _buckets(checks: Any) -> List[Any]:
    """jq's `map(.bucket)`: a non-array raises ("Cannot iterate over null"),
    null elements index to null, non-object elements raise."""
    if not isinstance(checks, list):
        raise TypeError("Cannot iterate over {}".format(type(checks).__name__))
    out: List[Any] = []
    for check in checks:
        if check is None:
            out.append(None)
        elif isinstance(check, dict):
            out.append(check.get("bucket"))
        else:
            raise TypeError("Cannot index {}".format(type(check).__name__))
    return out


def merged_oid(pr: Any, slug: str, gh: Optional[Runner] = None) -> str:
    """Idempotent verify helper: the MERGED commit oid (or "") without changing
    anything. Mirrors the shell's
    `if .state == "MERGED" then (.mergeCommit.oid // "merged") else "" end`,
    where the `//` only substitutes for null/false, and any unreadable or
    malformed payload (jq's stderr was silenced) reads as "not merged".

    Parsed with gh_retry.loads_json, not json.loads: jq tolerated a leading BOM
    and this read decides whether we merge at all, so a stray BOM must not turn
    an already-MERGED PR into "not merged" and trigger a second merge."""
    gh = gh or _gh
    text = _capture(
        gh(["gh", "pr", "view", str(pr), "-R", slug, "--json", "state,mergeCommit"])
    )
    if not text:
        return ""
    try:
        doc = loads_json(text)
        if not isinstance(doc, dict):  # jq: `.state` on a non-object errors
            return ""
        if doc.get("state") != "MERGED":
            return ""
        commit = doc.get("mergeCommit")
        if commit is not None and not isinstance(commit, dict):
            return ""  # jq: `.oid` on a non-object errors
        oid = commit.get("oid") if isinstance(commit, dict) else None
        if oid is None or oid is False:
            return "merged"
        return _jq_interp(oid)
    except ValueError:
        return ""


def refusal_report(state: Any) -> List[str]:
    """The indented report explaining a refused snapshot.

    Deliberate deviation from pr-merge.sh, which never showed these lines: its
    jq pipeline ended in `2>/dev/null >&2`, and bash applies redirections left
    to right, so fd2 went to /dev/null first and `>&2` then pointed fd1 at the
    same place — jq's output was discarded, leaving only the REFUSING line and
    the closing hint. The report the shell author clearly meant to print is the
    useful thing (it names which gate blocked the merge), so the port prints it
    to stderr instead of reproducing an accidental redirect bug.

    The rendering itself still mirrors jq: it STREAMS the five interpolated
    strings, so a document it cannot render partway through (e.g.
    `review_bot_checks` null in an error document) truncates the report at that
    line rather than suppressing all of it, and an empty input (pr_state
    produced nothing at all) prints no lines. Reproduced by building the lines
    lazily and stopping at the first failure.
    """
    lines: List[str] = []
    if not isinstance(state, dict):
        return lines
    builders = [
        lambda s: "  threads_fetched: {}".format(_jq_interp(s.get("threads_fetched"))),
        lambda s: "  ci_all_pass:     {}   failing: {}".format(
            _jq_interp(s.get("ci_all_pass")), _jq_interp(s.get("ci_failing"))
        ),
        lambda s: "  review_bot:      {}".format(
            _jq_join(_buckets(s.get("review_bot_checks")), "/")
        ),
        lambda s: "  unresolved:      {}".format(
            _jq_interp(s.get("review_threads_unresolved"))
        ),
        lambda s: "  mergeable:       {} / {}".format(
            _jq_interp(s.get("mergeable")), _jq_interp(s.get("mergeStateStatus"))
        ),
    ]
    for build in builders:
        try:
            lines.append(build(state))
        except Exception:
            break
    return lines


def snapshot(pr: Any, slug: str) -> Optional[Dict[str, Any]]:
    """The pr_state snapshot, in-process (the shell shelled out to pr-state.sh
    and kept going on failure). A dead snapshot yields None — the shell then
    captured an empty string, whose jq reads produced no `ready` value at all —
    which can only ever REFUSE, never green-light a merge."""
    try:
        return collect_state(pr, slug)
    except Exception:
        return None


def is_ready(state: Any) -> bool:
    """The shell's `[ "$(jq -r '.ready_to_merge // false')" = "true" ]`."""
    value = state.get("ready_to_merge") if isinstance(state, dict) else None
    if value is None or value is False:
        value = False
    return _jq_interp(value) == "true"


def signoff_trailers(commits_json: str) -> str:
    """Every Signed-off-by line in the PR's commit messages, deduped, SORTED
    (jq's `unique` sorts) and joined with newlines. A missing or malformed
    payload yields "" — jq's stderr was silenced and the shell kept going.
    BOM-tolerant like jq (loads_json): a BOM here would silently drop the DCO
    trailers from the squash commit."""
    if not commits_json:
        return ""
    found: List[str] = []
    try:
        commits = loads_json(commits_json)
        for entry in commits:  # jq's `.[]` — a non-array errors out
            message = entry["commit"]["message"]
            found.extend(SIGNOFF_SCAN_RE.findall(message))
    except Exception:
        return ""
    return "\n".join(sorted(set(found)))


def merge_argv(
    pr: Any, slug: str, strategy: str, delete_branch: bool, subject: str, body: str
) -> List[str]:
    """The gh merge invocation. `-R` keeps it a remote-only operation, and the
    commit subject/body only exist for the strategies that write a commit."""
    argv = ["gh", "pr", "merge", str(pr), "-R", slug, "--" + strategy]
    if delete_branch:
        argv.append("--delete-branch")
    if strategy in ("squash", "merge"):
        argv += ["--subject", subject, "--body", body]
    return argv


def main(argv: Sequence[str]) -> int:
    force_utf8()

    pr = ""
    repo = os.environ.get("GH_REPO", "")
    subject = ""
    body = ""
    strategy = "squash"
    delete_branch = True
    force = False

    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--subject", "--body", "--strategy"):
            if i + 1 >= len(args):
                sys.stderr.write("missing value for {}\n".format(arg))
                return 2
            value = args[i + 1]
            if arg == "--subject":
                subject = value
            elif arg == "--body":
                body = value
            else:
                strategy = value
            i += 2
            continue
        if arg == "--no-delete-branch":
            delete_branch = False
            i += 1
            continue
        if arg == "--force":
            force = True
            i += 1
            continue
        # The shell's `-*)` arm sits BEFORE `*/*)`, so a dashed word is an
        # unknown flag even when it contains a slash.
        if arg.startswith("-"):
            sys.stderr.write("unknown flag: {}\n".format(arg))
            return 2
        if "/" in arg:  # the `*/*)` glob: any word with a slash is the repo
            repo = arg
        elif not pr:
            pr = arg
        else:
            repo = arg
        i += 1

    r_flags = ["-R", repo] if repo else []
    if not pr:
        pr = _capture(
            _gh_plain(["gh", "pr", "view", *r_flags, "--json", "number", "-q", ".number"])
        )
    if not pr:
        sys.stderr.write(NO_PR_MESSAGE + "\n")
        return 1
    if repo:
        owner = repo.split("/", 1)[0]
        name = repo.rsplit("/", 1)[-1]
    else:
        ownername = _capture(
            _gh_plain(
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
        owner = ownername.split(" ", 1)[0]
        name = ownername.rsplit(" ", 1)[-1]
    slug = "{}/{}".format(owner, name)

    # Already merged? Report success and stop (never re-merge).
    oid = merged_oid(pr, slug)
    if oid:
        print("already merged: {} #{} ({})".format(slug, pr, oid))
        return 0

    # --- Safety gate: re-confirm the terminal condition from a NON-degraded
    #     snapshot ---
    if not force:
        state = snapshot(pr, slug)
        if not is_ready(state):
            sys.stderr.write(
                "pr-merge: REFUSING — PR #{} is not ready to merge.\n".format(pr)
            )
            for line in refusal_report(state):
                sys.stderr.write(line + "\n")
            sys.stderr.write(REFUSAL_HINT + "\n")
            return 3
        if not subject:
            subject = "{} (#{})".format(
                _jq_interp(state.get("title") if isinstance(state, dict) else None), pr
            )
    if not subject:
        # Only reachable under --force (the gated branch always fills subject).
        subject = "{} (#{})".format(
            _capture(
                _gh(["gh", "pr", "view", str(pr), "-R", slug, "--json", "title", "-q", ".title"])
            ),
            pr,
        )

    # --- Preserve DCO: append a Signed-off-by trailer from the PR's commits if
    #     absent ---
    if not SIGNOFF_RE.search(body):
        commits = _capture(_gh(["gh", "api", "repos/{}/pulls/{}/commits".format(slug, pr)]))
        signoff = signoff_trailers(commits)
        if signoff:
            body = "{}\n\n{}".format(body, signoff) if body else signoff

    # --- Merge (remote-only via -R, through the transient-retry wrapper) ---
    # Output AND exit code are discarded on purpose (the shell's
    # `>/dev/null 2>&1 || true`); the verify-after below is the verdict.
    _gh(merge_argv(pr, slug, strategy, delete_branch, subject, body))

    # --- Idempotent verify-after: the merge is a server op; trust the resulting
    #     state, not the merge call's exit code (it can fatal on local
    #     fast-forward yet land). ---
    oid = merged_oid(pr, slug)
    if oid:
        print("merged: {} #{} ({})".format(slug, pr, oid))
        return 0
    sys.stderr.write(
        "pr-merge: merge did not land for {} #{} — re-run pr_state.py and inspect.\n".format(
            slug, pr
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
