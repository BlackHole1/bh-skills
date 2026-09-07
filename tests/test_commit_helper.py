"""Integration tests for commit/scripts/commit_helper.py.

Every test runs the script as a subprocess against a real throwaway git repo
built by the conftest fixtures (isolated env: pinned identity 'Skills Test
<skills-test@example.com>', default branch main, no user git config, no gh).
This is the most integration-testable of the skill scripts: it shells out to
git only, never gh, so nothing here needs the network or auth.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

import commit_helper

SIGNOFF = "Signed-off-by: Skills Test <skills-test@example.com>"
HELPER = Path(__file__).resolve().parent.parent / "commit" / "scripts" / "commit_helper.py"


def prepare(run_script, repo, *args, env=None):
    return run_script("commit", "commit_helper.py", "prepare", *args, cwd=repo, env=env)


def commit(run_script, repo, *args, stdin="chore: test\n", env=None):
    return run_script(
        "commit", "commit_helper.py", "commit", *args, cwd=repo, stdin=stdin, env=env
    )


def state_fields(stdout):
    """Parse the first STATE line into a dict of its key=value fields."""
    first = stdout.splitlines()[0]
    assert first.startswith("STATE "), stdout
    return dict(kv.split("=", 1) for kv in first.split(" ")[1:])


def last_message(sh, repo):
    return sh(["git", "log", "-1", "--format=%B"], cwd=repo).stdout


# --- prepare ----------------------------------------------------------------


def test_prepare_outside_repo_reports_repo_no(run_script, tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    r = prepare(run_script, outside)
    assert r.returncode == 0
    assert r.stdout == "STATE repo=no\n"


def test_prepare_clean_repo_is_byte_exact_no_changes(run_script, git_repo, git_commit):
    repo = git_repo()
    git_commit(repo)
    r = prepare(run_script, repo)
    assert r.returncode == 0
    assert r.stdout == (
        "STATE repo=yes branch=main lang=en protected=yes unborn=no"
        " auto_staged=no staged_files=0\n"
        "\n"
        "NO_CHANGES (working tree is clean, nothing to commit)\n"
    )


def test_prepare_auto_stages_dirty_worktree(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "b.txt").write_text("new file\n", encoding="utf-8")

    r = prepare(run_script, repo)
    assert r.returncode == 0
    fields = state_fields(r.stdout)
    assert fields == {
        "repo": "yes",
        "branch": "main",
        "lang": "en",
        "protected": "yes",
        "unborn": "no",
        "auto_staged": "yes",
        "staged_files": "2",
    }
    staged = sh(["git", "diff", "--staged", "--name-only"], cwd=repo).stdout
    assert "a.txt" in staged and "b.txt" in staged

    # the snapshot sections follow, in order
    assert "\n## Recent commits (type and scope style to match)\n" in r.stdout
    assert "chore: test commit" in r.stdout  # git log --oneline entry
    assert "\n## Staged stat\n" in r.stdout
    assert "\n## Staged diff (first 500 of " in r.stdout


def test_prepare_leaves_manual_staging_alone(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    sh(["git", "add", "a.txt"], cwd=repo)
    (repo / "b.txt").write_text("untracked\n", encoding="utf-8")

    r = prepare(run_script, repo)
    assert r.returncode == 0
    fields = state_fields(r.stdout)
    assert fields["auto_staged"] == "no"
    assert fields["staged_files"] == "1"
    # the untracked file was NOT swept into the index
    porcelain = sh(["git", "status", "--porcelain"], cwd=repo).stdout
    assert "?? b.txt" in porcelain


def test_prepare_language_record_recall_and_override(
    run_script, git_repo, git_commit, sh
):
    repo = git_repo()
    git_commit(repo)

    r = prepare(run_script, repo, "zh")
    assert state_fields(r.stdout)["lang"] == "zh"
    rec = sh(["git", "config", "--local", "--get", "skills.lang"], cwd=repo)
    assert rec.stdout.strip() == "zh"

    # no argument: the recorded value wins
    r = prepare(run_script, repo)
    assert state_fields(r.stdout)["lang"] == "zh"

    # an explicit value overrides and re-records
    r = prepare(run_script, repo, "en")
    assert state_fields(r.stdout)["lang"] == "en"
    rec = sh(["git", "config", "--local", "--get", "skills.lang"], cwd=repo)
    assert rec.stdout.strip() == "en"


def test_prepare_unknown_language_dies(run_script, git_repo, git_commit):
    repo = git_repo()
    git_commit(repo)
    r = prepare(run_script, repo, "fr")
    assert r.returncode == 1
    assert r.stderr == "unknown language 'fr' (expected zh or en)\n"
    assert r.stdout == ""  # dies before the STATE line


def test_prepare_custom_lang_key(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    git_commit(repo)
    r = prepare(run_script, repo, "zh", env={"SKILLS_LANG_KEY": "my.bodylang"})
    assert state_fields(r.stdout)["lang"] == "zh"
    rec = sh(["git", "config", "--local", "--get", "my.bodylang"], cwd=repo)
    assert rec.stdout.strip() == "zh"
    # the default key was not touched
    assert sh(["git", "config", "--local", "--get", "skills.lang"], cwd=repo).returncode != 0


def test_prepare_diff_cap_honored(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    git_commit(repo)
    (repo / "big.txt").write_text(
        "".join("line {}\n".format(i) for i in range(100)), encoding="utf-8"
    )
    sh(["git", "add", "-A"], cwd=repo)
    full = sh(["git", "diff", "--staged"], cwd=repo).stdout
    total = full.count("\n")
    assert total > 9  # the cap must actually bite

    r = prepare(run_script, repo, env={"COMMIT_DIFF_MAX_LINES": "9"})
    assert r.returncode == 0
    header = (
        "## Staged diff (first 9 of {} lines; read more with:"
        " git diff --staged -- <path>)\n".format(total)
    )
    assert header in r.stdout
    tail = r.stdout.split(header, 1)[1]
    assert tail.count("\n") == 9
    first9 = "\n".join(full.split("\n")[:9]) + "\n"
    assert tail == first9


def test_prepare_diff_cap_bad_values_fall_back_to_500(
    run_script, git_repo, git_commit, sh
):
    repo = git_repo()
    git_commit(repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)
    for bad in ("lots", "", "12a"):
        r = prepare(run_script, repo, env={"COMMIT_DIFF_MAX_LINES": bad})
        assert r.returncode == 0, r.stderr
        assert "## Staged diff (first 500 of " in r.stdout, bad


# --- commit -----------------------------------------------------------------


def test_commit_on_protected_main_carves_branch(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    main_sha = git_commit(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)

    r = commit(
        run_script, repo, "feat-x", stdin="feat: change a\n\nBody line here.\n"
    )
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(
        r"COMMITTED branch=feat-x created_branch=feat-x sha=[0-9a-f]+\n", r.stdout
    )
    assert sh(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "feat-x"
    # main was never committed to
    assert (
        sh(["git", "rev-parse", "--short", "main"], cwd=repo).stdout.strip()
        == main_sha
    )
    body = last_message(sh, repo)
    assert body.startswith("feat: change a\n")
    assert "Body line here." in body
    assert SIGNOFF in body  # commit -s added the trailer


def test_commit_collision_picks_next_free_suffix(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    sh(["git", "branch", "feat-x"], cwd=repo)
    sh(["git", "branch", "feat-x-2"], cwd=repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)

    r = commit(run_script, repo, "feat-x")
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(
        r"COMMITTED branch=feat-x-3 created_branch=feat-x-3 sha=[0-9a-f]+\n", r.stdout
    )
    assert (
        sh(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "feat-x-3"
    )


def test_commit_empty_protected_list_allows_main(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)

    # set-but-empty means NO protected names: commit straight onto main,
    # no slug needed, no created_branch in the COMMITTED line
    r = commit(run_script, repo, env={"COMMIT_PROTECTED_BRANCHES": ""})
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"COMMITTED branch=main sha=[0-9a-f]+\n", r.stdout)
    assert sh(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "main"


def test_commit_custom_protected_list(run_script, git_repo, git_commit, sh):
    env = {"COMMIT_PROTECTED_BRANCHES": "trunk release"}
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)

    # main is not in the custom list, so it takes commits directly
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)
    r = commit(run_script, repo, env=env)
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"COMMITTED branch=main sha=[0-9a-f]+\n", r.stdout)

    # trunk IS protected: no slug dies, a slug carves off
    sh(["git", "switch", "-c", "trunk"], cwd=repo)
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)
    r = commit(run_script, repo, env=env)
    assert r.returncode == 1
    assert r.stderr == "On protected branch 'trunk' but no branch slug was given.\n"
    r = commit(run_script, repo, "fix-y", env=env)
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(
        r"COMMITTED branch=fix-y created_branch=fix-y sha=[0-9a-f]+\n", r.stdout
    )


def test_commit_on_feature_branch_needs_no_slug(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    sh(["git", "switch", "-c", "feature"], cwd=repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)
    r = commit(run_script, repo)
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"COMMITTED branch=feature sha=[0-9a-f]+\n", r.stdout)


def test_unborn_repo_first_commit_lands_on_carved_branch(
    run_script, git_repo, sh
):
    repo = git_repo()  # no commits: main is unborn
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")

    r = prepare(run_script, repo)
    assert r.returncode == 0
    fields = state_fields(r.stdout)
    assert fields["branch"] == "main"
    assert fields["protected"] == "yes"
    assert fields["unborn"] == "yes"
    assert fields["auto_staged"] == "yes"
    assert fields["staged_files"] == "1"

    r = commit(run_script, repo, "boot", stdin="feat: first commit\n")
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(
        r"COMMITTED branch=boot created_branch=boot sha=[0-9a-f]+\n", r.stdout
    )
    assert sh(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "boot"
    # switch -c renamed the unborn tip: main never came into existence
    verify = sh(["git", "show-ref", "--verify", "refs/heads/main"], cwd=repo)
    assert verify.returncode != 0
    assert SIGNOFF in last_message(sh, repo)


def test_detached_head_always_carves(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    sh(["git", "checkout", "--detach"], cwd=repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)

    r = prepare(run_script, repo)
    fields = state_fields(r.stdout)
    assert fields["branch"] == "<detached>"
    assert fields["protected"] == "yes"

    # detached + no slug is a die: the commit would be stranded otherwise
    r = commit(run_script, repo)
    assert r.returncode == 1
    assert (
        r.stderr == "On protected branch '<detached>' but no branch slug was given.\n"
    )

    r = commit(run_script, repo, "det-x")
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(
        r"COMMITTED branch=det-x created_branch=det-x sha=[0-9a-f]+\n", r.stdout
    )


def test_commit_message_matches_stdin_with_signoff(run_script, git_repo, git_commit, sh):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)

    # NB: the body must not look like a "Key: value" trailer, or git glues the
    # sign-off onto it without the separating blank line.
    message = "fix(core): stop the bleeding\n\nThe previous fix missed a case.\n"
    r = commit(run_script, repo, "fix-z", stdin=message)
    assert r.returncode == 0, r.stderr
    # `git log --format=%B` prints the body then its own terminating newline
    assert last_message(sh, repo) == message + "\n" + SIGNOFF + "\n\n"


# --- die paths --------------------------------------------------------------


def test_commit_outside_repo_dies(run_script, tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    r = commit(run_script, outside, "slug", stdin="")
    assert r.returncode == 1
    assert r.stderr == "Not a git repository.\n"


def test_commit_with_nothing_staged_dies(run_script, git_repo, git_commit):
    repo = git_repo()
    git_commit(repo)
    r = commit(run_script, repo, "slug", stdin="")
    assert r.returncode == 1
    assert r.stderr == "Nothing staged to commit (run 'prepare' first).\n"


def test_usage_die_on_missing_or_unknown_subcommand(run_script, tmp_path):
    usage = "usage: commit_helper.py {prepare [zh|en] | commit <branch-slug>}\n"
    r = run_script("commit", "commit_helper.py", cwd=tmp_path)
    assert r.returncode == 1
    assert r.stderr == usage
    r = run_script("commit", "commit_helper.py", "frobnicate", cwd=tmp_path)
    assert r.returncode == 1
    assert r.stderr == usage


# --- stdin byte transparency -------------------------------------------------
#
# force_utf8() cannot reconfigure sys.stdin, so a text read of the commit
# message uses the platform codepage with errors="strict". On Windows
# (cp936/cp1252/cp932) that mojibakes a Chinese body — the primary body
# language of this bilingual skill — or raises UnicodeDecodeError, and the
# traceback lands AFTER the feature branch was carved, leaving it checked out
# and empty. The shell piped stdin straight into `git commit -F -`; so must the
# port. PYTHONIOENCODING reproduces a Windows codepage stdin on any OS.

CJK_EMOJI_MESSAGE = (
    "feat(核心): 支持中文提交信息 🚀\n\n正文：修复了编码问题，emoji 也要活下来 ✅。\n"
)
LATIN1_MESSAGE = b"fix: latin1 caf\xe9 body\n"

# run_bytes() reads stdout raw, so no universal-newline translation: the helper
# print()s in text mode and Windows emits \r\n there. The text-mode fixtures
# elsewhere hide this; here it must be tolerated explicitly.
COMMITTED_LINE = r"COMMITTED branch=feature sha=[0-9a-f]+\r?\n"


def run_bytes(cmd, cwd, env, stdin=None):
    """subprocess.run in BINARY mode. The conftest `sh` fixture is text-mode
    UTF-8; these tests are about the raw bytes crossing the seam."""
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd),
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=60,
    )


def staged_feature_repo(git_repo, git_commit, sh):
    """A repo on a non-protected branch with exactly one staged change, so the
    commit path is reached without the branch-carving detour."""
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo)
    sh(["git", "switch", "-c", "feature"], cwd=repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo)
    return repo


@pytest.mark.parametrize("io_encoding", [None, "cp1252", "ascii", "cp932"])
def test_cjk_emoji_message_survives_any_stdin_encoding(
    git_repo, git_commit, sh, isolated_env, io_encoding
):
    repo = staged_feature_repo(git_repo, git_commit, sh)
    env = dict(isolated_env)
    if io_encoding:
        env["PYTHONIOENCODING"] = io_encoding
    raw = CJK_EMOJI_MESSAGE.encode("utf-8")

    r = run_bytes([sys.executable, HELPER, "commit", "slug"], repo, env, stdin=raw)
    assert r.returncode == 0, r.stderr
    assert b"Traceback" not in r.stderr
    assert re.fullmatch(COMMITTED_LINE, r.stdout.decode("utf-8"))

    stored = run_bytes(["git", "log", "-1", "--format=%B"], repo, isolated_env).stdout
    # byte-for-byte: git appends only its sign-off trailer after our bytes
    assert stored.startswith(raw)
    assert SIGNOFF.encode("utf-8") in stored


def test_invalid_utf8_message_commits_instead_of_crashing(
    git_repo, git_commit, sh, isolated_env
):
    # A latin-1 body cannot be decoded as UTF-8. The shell piped it to git and
    # git dealt with it (it warns, then re-encodes); the port must hand git the
    # same bytes rather than raising UnicodeDecodeError out of the stdin read.
    repo = staged_feature_repo(git_repo, git_commit, sh)
    r = run_bytes(
        [sys.executable, HELPER, "commit", "slug"], repo, isolated_env,
        stdin=LATIN1_MESSAGE,
    )
    assert r.returncode == 0, r.stderr
    assert b"Traceback" not in r.stderr
    assert b"UnicodeDecodeError" not in r.stderr
    assert re.fullmatch(COMMITTED_LINE, r.stdout.decode("utf-8"))
    # git owns whatever re-encoding happens next; we only promise it got there
    stored = run_bytes(["git", "log", "-1", "--format=%B"], repo, isolated_env).stdout
    assert b"fix: latin1 caf" in stored


@pytest.mark.parametrize("raw", [b"", b"   \n\n\t\n"])
def test_empty_or_whitespace_only_message_is_still_rejected(
    git_repo, git_commit, sh, isolated_env, raw
):
    # Unchanged from the shell: git is the one that refuses, and its exit code
    # is what the caller sees. Nothing must land.
    repo = staged_feature_repo(git_repo, git_commit, sh)
    r = run_bytes([sys.executable, HELPER, "commit", "slug"], repo, isolated_env, stdin=raw)
    assert r.returncode != 0
    # Deliberately NOT asserting git's English "Aborting commit due to empty
    # commit message." — isolated_env pins HOME and GIT_CONFIG_GLOBAL but not
    # LC_ALL/LANG, and Git for Windows ships translations, so that string is
    # whatever the runner's locale says. What must hold is locale-independent:
    # git spoke (its refusal was passed through), Python did not crash, and
    # nothing landed.
    assert r.stderr != b""
    assert b"Traceback" not in r.stderr
    assert r.stdout == b""  # no COMMITTED line
    log = run_bytes(["git", "log", "--format=%s"], repo, isolated_env).stdout
    assert log == b"chore: test commit\n"  # still just the fixture commit


class TextOnlyStdin:
    """A stdin whose text read is a landmine, so any return to decoding the
    message fails loudly instead of passing on a UTF-8 developer machine."""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def read(self):
        raise AssertionError("the commit message must be read as bytes, never decoded")


def test_read_stdin_bytes_prefers_the_binary_view(monkeypatch):
    raw = CJK_EMOJI_MESSAGE.encode("utf-8")
    monkeypatch.setattr(sys, "stdin", TextOnlyStdin(raw))
    assert commit_helper.read_stdin_bytes() == raw


def test_read_stdin_bytes_falls_back_without_a_binary_view(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("中文 🚀\n"))
    assert commit_helper.read_stdin_bytes() == "中文 🚀\n".encode("utf-8")
    monkeypatch.setattr(sys, "stdin", None)
    assert commit_helper.read_stdin_bytes() == b""


def test_run_command_binary_stdin_still_returns_decoded_str():
    # stdin_bytes flips the child into binary mode; stdout/stderr must still
    # come back as str (UTF-8, errors="replace") so callers are unaffected.
    res = commit_helper.run_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin_bytes="中文 🚀\n".encode("utf-8"),
    )
    assert res.rc == 0
    assert res.out == "中文 🚀\n"
    assert res.err == ""

    lossy = commit_helper.run_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9')"],
        stdin_bytes=b"",
    )
    assert lossy.rc == 0
    assert lossy.out == "caf�"  # errors="replace", not a crash


def test_run_command_binary_mode_missing_binary_is_still_rc_127():
    res = commit_helper.run_command(["definitely-not-a-real-binary-xyz"], stdin_bytes=b"x")
    assert res.rc == 127
    assert res.err == "definitely-not-a-real-binary-xyz: command not found"
