"""Unit tests for ship-pr/scripts/gh_retry.py.

The retry loop is exercised through an injected runner/sleep, so no gh binary,
network, or real waiting is involved. One subprocess test covers the CLI
wrapper mode end to end with this Python interpreter as the wrapped command.

loads_json lives here too and is pinned directly: four modules parse their gh
output through it, and every one of those seams feeds a safety decision, so
"one BOM stripped, nothing else forgiven" is a contract, not an implementation
detail.
"""

from __future__ import annotations

import json
import sys

import pytest

import gh_retry
from gh_retry import RunResult, gh_retry as retry

BOM = "\ufeff"  # spelled as an escape so this source file stays BOM-free


class ScriptedRunner:
    """Returns queued RunResults; records how it was called."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, stdin=None):
        self.calls.append((list(cmd), stdin))
        return self.results.pop(0)


def test_nonempty_output_is_success_even_with_nonzero_exit():
    runner = ScriptedRunner([RunResult('[{"name":"ci"}]', "1 failing check", 8)])
    res = retry(["gh", "pr", "checks"], runner=runner, sleep=lambda s: None)
    assert res.out == '[{"name":"ci"}]'
    assert res.rc == 0
    assert len(runner.calls) == 1


def test_transient_error_retries_with_backoff_then_succeeds():
    runner = ScriptedRunner(
        [
            RunResult("", "Post https://api.github.com/graphql: EOF", 1),
            RunResult("", "HTTP 502 Bad Gateway", 1),
            RunResult("ok", "", 0),
        ]
    )
    sleeps = []
    res = retry(["gh", "api", "graphql"], runner=runner, sleep=sleeps.append)
    assert res.out == "ok"
    assert res.rc == 0
    assert len(runner.calls) == 3
    assert sleeps == [2, 4]  # delay * attempt


def test_non_transient_failure_returns_immediately():
    runner = ScriptedRunner([RunResult("", "HTTP 404: Not Found", 1)])
    sleeps = []
    res = retry(["gh", "api", "nope"], runner=runner, sleep=sleeps.append)
    assert res.out == ""
    assert res.rc == 1
    assert len(runner.calls) == 1
    assert sleeps == []


def test_exhausts_tries_and_returns_last_result():
    runner = ScriptedRunner([RunResult("", "connection reset by peer", 1)] * 3)
    sleeps = []
    res = retry(["gh", "api"], tries=3, runner=runner, sleep=sleeps.append)
    assert res.out == ""
    assert res.rc == 1
    assert len(runner.calls) == 3
    assert sleeps == [2, 4, 6]


def test_env_tunables_are_honored(monkeypatch):
    monkeypatch.setenv("GH_RETRY_TRIES", "2")
    monkeypatch.setenv("GH_RETRY_DELAY", "5")
    runner = ScriptedRunner([RunResult("", "i/o timeout", 1)] * 2)
    sleeps = []
    res = retry(["gh"], runner=runner, sleep=sleeps.append)
    assert res.rc == 1
    assert len(runner.calls) == 2
    assert sleeps == [5, 10]


def test_bad_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("GH_RETRY_TRIES", "lots")
    runner = ScriptedRunner([RunResult("x", "", 0)])
    assert retry(["gh"], runner=runner, sleep=lambda s: None).out == "x"


def test_missing_binary_maps_to_rc_127_without_retry():
    res = retry(["definitely-not-a-real-command-xyz"], sleep=lambda s: None)
    assert res.out == ""
    assert res.rc == 127


def test_transient_regex_covers_the_shell_patterns():
    for line in [
        "unexpected EOF",
        "request timed out",
        "timeout awaiting response",
        "HTTP 503 Service Unavailable",
        "read: connection reset",
        "connect: connection refused",
        "the service is temporarily unavailable",
        "dial tcp: i/o timeout",
        "TLS handshake failure",
    ]:
        assert gh_retry.TRANSIENT_RE.search(line), line
    for line in ["HTTP 404: Not Found", "Validation Failed", "HTTP 501"]:
        assert not gh_retry.TRANSIENT_RE.search(line), line


# --- loads_json (BOM-tolerant parse, shared by four modules) -----------------


def test_loads_json_parses_a_plain_document():
    # No BOM: plain json.loads behavior, all types intact.
    assert gh_retry.loads_json('{"state":"MERGED","ready":true}') == {
        "state": "MERGED",
        "ready": True,
    }
    assert gh_retry.loads_json("[1, 2]") == [1, 2]
    assert gh_retry.loads_json("null") is None
    assert gh_retry.loads_json('"plain"') == "plain"


def test_loads_json_strips_a_leading_bom_like_jq():
    # The whole point: gh output that arrives BOM-prefixed still parses, so a
    # safety decision is never downgraded by a stray byte-order mark.
    assert gh_retry.loads_json(BOM + '{"state":"MERGED"}') == {"state": "MERGED"}
    assert gh_retry.loads_json(BOM + "[]") == []
    # json.loads itself would have refused it — this is a real difference.
    with pytest.raises(ValueError):
        json.loads(BOM + '{"state":"MERGED"}')


def test_loads_json_strips_exactly_one_bom_and_no_more():
    # jq skips ONE leading BOM; a doubled BOM is still malformed input and must
    # stay an error rather than being scrubbed until something parses.
    with pytest.raises(ValueError):
        gh_retry.loads_json(BOM + BOM + '{"a":1}')
    # A BOM anywhere but the very front is untouched, too.
    with pytest.raises(ValueError):
        gh_retry.loads_json('{"a":1}' + BOM)
    with pytest.raises(ValueError):
        gh_retry.loads_json("[" + BOM + "1]")


def test_loads_json_bom_only_raises_like_empty_input():
    # Nothing but a BOM is an empty document: same ValueError, same message as
    # json.loads(""), so callers' `except ValueError` arms behave identically.
    with pytest.raises(ValueError) as stripped:
        gh_retry.loads_json(BOM)
    with pytest.raises(ValueError) as empty:
        json.loads("")
    assert type(stripped.value) is type(empty.value)
    assert str(stripped.value) == str(empty.value)


def test_loads_json_invalid_input_raises_unchanged():
    for text in ["", "not json", "{", '{"a":1} trailing']:
        with pytest.raises(json.JSONDecodeError):
            gh_retry.loads_json(text)
        with pytest.raises(json.JSONDecodeError):
            gh_retry.loads_json(BOM + text)  # BOM stripped, garbage still raises


def test_cli_wrapper_passes_stdout_through(run_script):
    code = "import sys; sys.stdout.write('hello'); sys.exit(3)"
    r = run_script("ship-pr", "gh_retry.py", sys.executable, "-c", code)
    assert r.stdout == "hello"
    assert r.returncode == 0  # non-empty output is success


def test_cli_wrapper_usage_error():
    # No args: exit 2 (checked via direct main call to stay fast).
    assert gh_retry.main([]) == 2
