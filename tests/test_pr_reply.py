"""Tests for ship-pr/scripts/pr_reply.py.

The double-post protection is the point of this script, so the POST seam and
the retried-read seam are faked separately: tests assert not just exit codes
but HOW MANY times the non-idempotent POST ran, and that the sleep-then-verify
dance happens in the shell's exact order (verify before ever re-posting; never
retry blind when the author is unreadable). No network, no gh binary.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import pr_reply
from gh_retry import RunResult

SHIP_PR_SCRIPTS = Path(__file__).resolve().parent.parent / "ship-pr" / "scripts"


def reply(rid, login, url):
    return {"in_reply_to_id": rid, "user": {"login": login}, "html_url": url}


def page(*comments):
    return json.dumps(list(comments))


class FakeRetry:
    """Serves the two gh_retry-backed reads: `gh api user` (whoami) and the
    paginated reply listing. Listing responses are a queue so a post-failure
    re-check can observe a different world than the pre-check."""

    def __init__(self, me="alice", pages=()):
        self.me = me
        self.pages = list(pages)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if list(cmd[:3]) == ["gh", "api", "user"]:
            if self.me is None:
                return RunResult("", "gh: not logged in", 1)
            return RunResult(self.me + "\n", "", 0)
        assert "--paginate" in cmd, cmd
        out = self.pages.pop(0) if self.pages else "[]"
        return RunResult(out, "", 0)


class FakePost:
    """Queued results for the single-shot POST seam; records every call."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return self.results.pop(0)


@pytest.fixture
def body(tmp_path):
    path = tmp_path / "body.md"
    path.write_text("@coderabbitai intentional, see the guard two lines up\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def seams(monkeypatch):
    """Install fakes and collect sleeps; returns (retry, post, sleeps) setter."""

    def install(retry, post):
        sleeps = []
        monkeypatch.setattr(pr_reply, "_retry", retry)
        monkeypatch.setattr(pr_reply, "_run", post)
        monkeypatch.setattr(pr_reply, "_sleep", sleeps.append)
        return sleeps

    return install


# ---- the double-post-protection paths ----------------------------------------


def test_already_replied_short_circuits_without_posting(seams, body, capsys):
    retry = FakeRetry(pages=[page(reply(123, "alice", "https://x/pre"))])
    post = FakePost()
    seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 0
    assert capsys.readouterr().out == "already replied: https://x/pre\n"
    assert post.calls == []  # the POST never ran


def test_first_post_succeeds(seams, body, capsys):
    retry = FakeRetry(pages=["[]"])
    post = FakePost([RunResult(json.dumps({"id": 42, "html_url": "https://x/42"}), "", 0)])
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 0
    assert capsys.readouterr().out == "replied: https://x/42  (id 42)\n"
    assert post.calls == [
        ["gh", "api", "repos/o/r/pulls/7/comments/123/replies", "-F", "body=@" + body]
    ]
    assert sleeps == []


def test_bom_prefixed_ack_is_read_as_a_success(seams, body, capsys):
    # jq skipped a leading BOM; json.loads refuses one. An unread ACK sends
    # this script into the sleep-verify-repost loop, so a stray BOM must never
    # be the reason a landed reply looks unposted (see loads_json in pr_reply).
    retry = FakeRetry(pages=["[]"])
    post = FakePost(
        [RunResult("\ufeff" + json.dumps({"id": 42, "html_url": "https://x/42"}), "", 0)]
    )
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 0
    assert capsys.readouterr().out == "replied: https://x/42  (id 42)\n"
    assert len(post.calls) == 1  # no second POST
    assert sleeps == []


def test_transient_error_confirms_landed_reply_instead_of_reposting(seams, body, capsys):
    # The regression this design exists for: the POST's ACK is dropped ("EOF")
    # but the reply DID land. The re-check must find it and NOT post again.
    retry = FakeRetry(
        pages=["[]", page(reply(123, "alice", "https://x/landed"))]
    )
    post = FakePost([RunResult("", "Post https://api.github.com/...: EOF", 1)])
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 0
    assert capsys.readouterr().out == (
        "replied (confirmed after a transient error): https://x/landed\n"
    )
    assert len(post.calls) == 1  # exactly one POST — no double post
    assert sleeps == [2]  # waited out read-after-write lag before verifying


def test_transient_error_then_verified_retry_succeeds(seams, body, capsys):
    retry = FakeRetry(pages=["[]", "[]"])  # pre-check, then post-failure check
    post = FakePost(
        [
            RunResult("", "read: connection reset by peer", 1),
            RunResult(json.dumps({"id": 9, "html_url": "https://x/9"}), "", 0),
        ]
    )
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 0
    assert capsys.readouterr().out == "replied: https://x/9  (id 9)\n"
    assert len(post.calls) == 2
    assert sleeps == [2]


def test_non_transient_failure_never_retries(seams, body, capsys):
    retry = FakeRetry(pages=["[]", "[]"])
    post = FakePost([RunResult("", "HTTP 422 Validation Failed", 1)])
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 1
    err = capsys.readouterr().err
    # Message line, then the captured stderr verbatim (the shell's `cat "$err"`).
    assert err == (
        "pr-reply: failed to post reply to comment 123 in o/r #7:\n"
        "HTTP 422 Validation Failed"
    )
    assert len(post.calls) == 1
    # Even a non-transient failure gets ONE landed-check (sleep happens first).
    assert sleeps == [2]


def test_empty_me_never_retries_blind(seams, body, capsys):
    # Whoami failed: without a readable author we cannot verify whether a POST
    # landed, so even a transient-looking error must NOT be retried.
    retry = FakeRetry(me=None)
    post = FakePost([RunResult("", "unexpected EOF", 1)])
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 1
    assert len(post.calls) == 1
    assert sleeps == []  # broke out before the sleep-and-verify
    assert retry.calls == [["gh", "api", "user", "-q", ".login"]]  # listing never fetched


def test_transient_failure_exhausts_three_posts(seams, body):
    retry = FakeRetry()  # every listing read returns [] (nothing ever lands)
    post = FakePost([RunResult("", "HTTP 502 Bad Gateway", 1)] * 3)
    sleeps = seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r", "--body-file", body]) == 1
    assert len(post.calls) == 3
    assert sleeps == [2, 4, 6]  # 2 * attempt


def test_gh_repo_env_seam_supplies_the_slug(seams, body, monkeypatch, capsys):
    monkeypatch.setenv("GH_REPO", "env/repo")
    retry = FakeRetry(pages=["[]"])
    post = FakePost([RunResult(json.dumps({"id": 1, "html_url": "u"}), "", 0)])
    seams(retry, post)
    assert pr_reply.main(["7", "123", "--body-file", body]) == 0
    assert post.calls[0][2] == "repos/env/repo/pulls/7/comments/123/replies"
    capsys.readouterr()


# ---- argument / body validation ----------------------------------------------


def test_usage_requires_pr_and_reply_id(capsys):
    assert pr_reply.main([]) == 2
    assert pr_reply.main(["7"]) == 2
    assert "usage: pr_reply.py" in capsys.readouterr().err


def test_unknown_flag_exits_2(capsys):
    assert pr_reply.main(["-x", "7", "123"]) == 2
    assert capsys.readouterr().err == "unknown flag: -x\n"


def test_missing_body_file_value_exits_2(capsys):
    assert pr_reply.main(["7", "123", "--body-file"]) == 2
    assert capsys.readouterr().err == "missing value for --body-file\n"


@pytest.mark.parametrize("bad", ["12a", "1.5", "١٢٣"])  # ASCII digits only
def test_non_numeric_reply_id_exits_2(bad, capsys):
    assert pr_reply.main(["7", bad, "o/r"]) == 2
    assert capsys.readouterr().err == (
        "pr-reply: REPLY_TO_ID must be a numeric comment id"
        " (the 'reply-to id' from pr_comments.py)\n"
    )


def test_empty_body_file_exits_2(tmp_path, capsys):
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert pr_reply.main(["7", "123", "o/r", "--body-file", str(empty)]) == 2
    assert "pr-reply: empty body" in capsys.readouterr().err


def test_missing_body_file_counts_as_empty(tmp_path, capsys):
    assert pr_reply.main(["7", "123", "o/r", "--body-file", str(tmp_path / "no.md")]) == 2
    assert "pr-reply: empty body" in capsys.readouterr().err


# ---- stdin byte transparency --------------------------------------------------
#
# Taking the body on stdin is pointless if the read decodes it: force_utf8()
# cannot reconfigure sys.stdin, so on Windows it keeps the console/pipe codepage
# (cp1252 / cp936 / cp932) with errors="strict" — a Chinese or emoji reply would
# be POSTed as mojibake, or the script would die with UnicodeDecodeError. The
# body must travel stdin -> temp file -> POST as raw bytes, like `cat > file`.

CJK_EMOJI_BODY = (
    "@coderabbitai 这是误报：守卫就在上面两行 🚀\n\n见 `guard()` 的空值检查 ✅。\n"
).encode("utf-8")
INVALID_UTF8_BODY = b"@coderabbitai latin1 caf\xe9 plus \xff\xfe, not valid UTF-8\n"


class BytesStdin:
    """A stdin that only yields bytes. `.read()` is a landmine, so any return to
    decoding the body fails the test loudly instead of silently passing on a
    UTF-8 developer machine."""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def read(self):
        raise AssertionError("pr_reply must read the body as bytes, never decode it")


def post_from_stdin(monkeypatch, seams, raw):
    """Run main() with stdin = raw bytes; return (rc, {path, bytes}) captured at
    the POST seam — i.e. exactly what gh would have uploaded."""
    seen = {}
    retry = FakeRetry(pages=["[]"])

    def post(cmd, **kwargs):
        path = cmd[-1].split("body=@", 1)[1]
        seen["path"] = path
        with open(path, "rb") as handle:
            seen["bytes"] = handle.read()
        return RunResult(json.dumps({"id": 5, "html_url": "https://x/5"}), "", 0)

    monkeypatch.setattr(sys, "stdin", BytesStdin(raw))
    seams(retry, post)
    return pr_reply.main(["7", "123", "o/r"]), seen


def test_stdin_body_reaches_the_post_byte_for_byte(monkeypatch, seams, capsys):
    rc, seen = post_from_stdin(monkeypatch, seams, CJK_EMOJI_BODY)
    assert rc == 0
    assert seen["bytes"] == CJK_EMOJI_BODY
    assert capsys.readouterr().out == "replied: https://x/5  (id 5)\n"
    assert not os.path.exists(seen["path"])  # the trap still removes the temp body


def test_invalid_utf8_stdin_body_is_neither_rewritten_nor_fatal(
    monkeypatch, seams, capsys
):
    rc, seen = post_from_stdin(monkeypatch, seams, INVALID_UTF8_BODY)
    assert rc == 0
    assert seen["bytes"] == INVALID_UTF8_BODY
    capsys.readouterr()


def test_stdin_body_newlines_are_not_translated(monkeypatch, seams, capsys):
    # Binary mode keeps what the shell's `cat` kept: no \n -> \r\n on Windows,
    # and an author's CRLF survives unchanged.
    raw = b"@coderabbitai line one\r\nline two\nline three\r\n"
    rc, seen = post_from_stdin(monkeypatch, seams, raw)
    assert rc == 0
    assert seen["bytes"] == raw
    capsys.readouterr()


def test_empty_stdin_body_still_exits_2_on_the_byte_path(monkeypatch, seams, capsys):
    # `[ ! -s "$body_file" ]`: zero bytes is the rejection, unchanged.
    retry = FakeRetry(pages=["[]"])
    post = FakePost()
    monkeypatch.setattr(sys, "stdin", BytesStdin(b""))
    seams(retry, post)
    assert pr_reply.main(["7", "123", "o/r"]) == 2
    assert "pr-reply: empty body" in capsys.readouterr().err
    assert post.calls == []


def test_read_stdin_bytes_falls_back_without_a_binary_view(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("中文 🚀\n"))
    assert pr_reply.read_stdin_bytes() == "中文 🚀\n".encode("utf-8")
    monkeypatch.setattr(sys, "stdin", None)
    assert pr_reply.read_stdin_bytes() == b""


# A real interpreter driving main() with the gh seams faked in-process: no gh
# binary, no auth, no network. PYTHONIOENCODING reproduces a Windows codepage
# stdin on any OS — cp1252 used to mojibake this body, ascii and cp932 used to
# kill the script with UnicodeDecodeError.
_DRIVER = """\
import json, sys
import pr_reply
from gh_retry import RunResult

dest = sys.argv[1]


def fake_post(cmd, **kwargs):
    path = cmd[-1].split("body=@", 1)[1]
    with open(path, "rb") as src, open(dest, "wb") as out:
        out.write(src.read())
    return RunResult(json.dumps({"id": 1, "html_url": "https://x/1"}), "", 0)


def fake_retry(cmd, **kwargs):
    if list(cmd[:3]) == ["gh", "api", "user"]:
        return RunResult("alice\\n", "", 0)
    return RunResult("[]", "", 0)


pr_reply._run = fake_post
pr_reply._retry = fake_retry
sys.exit(pr_reply.main(sys.argv[2:]))
"""


@pytest.mark.parametrize("io_encoding", ["cp1252", "ascii", "cp932"])
@pytest.mark.parametrize("raw", [CJK_EMOJI_BODY, INVALID_UTF8_BODY])
def test_non_utf8_stdin_process_posts_the_exact_bytes(tmp_path, io_encoding, raw):
    dest = tmp_path / "posted.bin"
    env = dict(os.environ)
    env.pop("GH_REPO", None)
    env["PYTHONPATH"] = str(SHIP_PR_SCRIPTS)
    env["PYTHONIOENCODING"] = io_encoding
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(dest), "7", "123", "o/r"],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert dest.read_bytes() == raw


# ---- the --paginate concatenated-arrays parser -------------------------------


def test_parse_concatenated_arrays_flattens_pages():
    assert pr_reply.parse_concatenated_arrays("[1,2][3]") == [1, 2, 3]
    assert pr_reply.parse_concatenated_arrays('[{"a":1}]\n[{"b":2}]\n') == [
        {"a": 1},
        {"b": 2},
    ]
    assert pr_reply.parse_concatenated_arrays("[]") == []
    assert pr_reply.parse_concatenated_arrays("") == []
    assert pr_reply.parse_concatenated_arrays("  \n ") == []


def test_parse_concatenated_arrays_bails_like_jq_on_garbage():
    assert pr_reply.parse_concatenated_arrays("[1] not json") == []
    assert pr_reply.parse_concatenated_arrays('{"a":1}') == []  # non-array page


def test_find_reply_url_last_match_wins():
    comments = [
        reply(1, "alice", "https://x/first"),
        {"in_reply_to_id": 1, "user": None, "html_url": "https://x/nouser"},
        reply(1, "bob", "https://x/notme"),
        reply(2, "alice", "https://x/otherthread"),
        reply(1, "alice", "https://x/last"),
    ]
    assert pr_reply.find_reply_url(comments, 1, "alice") == "https://x/last"
    assert pr_reply.find_reply_url(comments, 3, "alice") == ""
    # Null html_url on the last match is jq's `// empty`, not a fallback.
    nulled = [reply(1, "alice", "https://x/u"), reply(1, "alice", None)]
    assert pr_reply.find_reply_url(nulled, 1, "alice") == ""


# ---- subprocess integration (no gh needed on these paths) --------------------


def test_cli_usage_exit(run_script):
    r = run_script("ship-pr", "pr_reply.py", stdin="")
    assert r.returncode == 2
    assert "usage: pr_reply.py" in r.stderr


def test_cli_bad_reply_id_exits_before_any_gh_call(run_script):
    r = run_script("ship-pr", "pr_reply.py", "7", "12a", stdin="ignored")
    assert r.returncode == 2
    assert "must be a numeric comment id" in r.stderr


def test_cli_empty_stdin_body_exits_2(run_script):
    # Explicit OWNER/REPO, so no gh call happens before the body check.
    r = run_script("ship-pr", "pr_reply.py", "7", "123", "o/r", stdin="")
    assert r.returncode == 2
    assert "pr-reply: empty body" in r.stderr
