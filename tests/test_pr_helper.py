"""Tests for create-pr/scripts/pr_helper.py.

Two layers, because the script has two boundaries:

- Subprocess integration tests against real throwaway git repos under the
  isolated env from conftest (no user git config, no gh auth). gh may be absent
  or unauthenticated there; both degrade deterministically to gh=no /
  repo_slug=unknown / default_base=main / Existing PR = NONE, which is exactly
  the fallback those tests pin down — nothing there asserts on live gh data.
- In-process tests that monkeypatch the module's `_gh` seam, so the whole
  gh-SUCCESS branch (repo_slug, default_base, the passed-through PR JSON) is
  covered without the gh binary, the network, or auth. git still runs for real
  in those, against the same throwaway repos; `enter_repo` applies conftest's
  isolation to this process so they read the pinned config too.

The port deviates from pr-helper.sh in five places, each argued in the module
under test; the deviations have tests of their own below.
"""

from __future__ import annotations

import errno
import io
import json
import os
import sys

import pytest

import pr_helper
from pr_helper import RunResult

USAGE = "usage: pr_helper.py prepare [zh|en] [base-branch]"


def prepare(run_script, repo, *args, env=None):
    return run_script("create-pr", "pr_helper.py", "prepare", *args, cwd=repo, env=env)


def state_of(stdout):
    lines = stdout.splitlines()
    assert lines, stdout
    line = lines[0]
    assert line.startswith("STATE "), line
    return dict(kv.split("=", 1) for kv in line[len("STATE "):].split(" "))


def rev(sh, repo, *args):
    r = sh(["git", *args], cwd=repo)
    assert r.returncode == 0, r.stderr
    return r.stdout.rstrip("\n")


def make_branch_repo(git_repo, git_commit, sh):
    """main with one commit, feature branch with one more commit."""
    repo = git_repo()
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    git_commit(repo, "feat: base commit")
    r = sh(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    assert r.returncode == 0, r.stderr
    (repo / "b.txt").write_text("branch work\n", encoding="utf-8")
    branch_sha = git_commit(repo, "feat: branch commit")
    return repo, branch_sha


def test_state_on_feature_branch(git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    st = state_of(r.stdout)
    assert st["repo"] == "yes"
    assert st["branch"] == "feature"
    assert st["detached"] == "no"
    assert st["lang"] == "en"
    # gh is absent or unauthenticated in the isolated env: fallback values.
    assert st["gh"] == "no"
    assert st["repo_slug"] == "unknown"
    assert st["default_base"] == "main"
    assert st["base"] == "main"
    assert st["on_base"] == "no"
    assert st["head_sha"] == rev(sh, repo, "rev-parse", "HEAD")
    assert st["merge_base"] == rev(sh, repo, "rev-parse", "main")
    assert st["upstream"] == "none"
    assert st["ahead"] == "n/a"
    assert st["dirty"] == "no"
    assert st["commits"] == "1"

    # Blank line after STATE, then the sections in their fixed order.
    assert r.stdout.splitlines()[1] == ""
    headers = [
        "## Existing PR (NONE means create a new one)",
        "## Commits on this branch (oldest first, subjects and bodies)",
        "## Files changed",
        "## Branch diff (first ",
        "## PR template",
    ]
    pos = -1
    for h in headers:
        idx = r.stdout.index(h)
        assert idx > pos, h
        pos = idx
    # No PR (gh fails in the isolated env) -> the NONE marker.
    assert "## Existing PR (NONE means create a new one)\nNONE\n" in r.stdout
    assert "b.txt" in r.stdout  # diffstat / diff cover the branch's work


def test_state_on_base_branch(git_repo, git_commit, sh, run_script):
    repo = git_repo()
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    git_commit(repo, "feat: base commit")
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    st = state_of(r.stdout)
    assert st["branch"] == "main"
    assert st["on_base"] == "yes"
    # On the base itself the merge base is HEAD, so the range is empty.
    assert st["merge_base"] == st["head_sha"]
    assert st["commits"] == "0"


def test_merge_base_falls_back_to_root_commit(git_repo, git_commit, sh, run_script):
    repo = git_repo()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(repo, "feat: one")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    git_commit(repo, "feat: two")
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    git_commit(repo, "feat: three")
    r = prepare(run_script, repo, "en", "no-such-branch")
    assert r.returncode == 0, r.stderr
    st = state_of(r.stdout)
    assert st["base"] == "no-such-branch"
    assert st["on_base"] == "no"
    root = rev(sh, repo, "rev-list", "--max-parents=0", "HEAD")
    assert st["merge_base"] == root
    # root..HEAD excludes the root commit itself.
    assert st["commits"] == "2"
    assert "git diff {}..HEAD -- <path>".format(root) in r.stdout


def test_detached_head(git_repo, git_commit, sh, run_script):
    repo = git_repo()
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    git_commit(repo, "feat: base commit")
    r = sh(["git", "checkout", "-q", "--detach"], cwd=repo)
    assert r.returncode == 0, r.stderr
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    st = state_of(r.stdout)
    assert st["branch"] == "<detached>"
    assert st["detached"] == "yes"
    assert st["on_base"] == "no"


def test_dirty_flag(git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    (repo / "b.txt").write_text("uncommitted edit\n", encoding="utf-8")
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    assert state_of(r.stdout)["dirty"] == "yes"


def test_commits_section_oldest_first_with_bodies(git_repo, git_commit, sh, run_script):
    repo = git_repo()
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    git_commit(repo, "feat: base commit")
    r = sh(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    assert r.returncode == 0, r.stderr
    (repo / "b.txt").write_text("1\n", encoding="utf-8")
    sha_second = git_commit(repo, "feat: second\n\nsecond body line")
    (repo / "b.txt").write_text("2\n", encoding="utf-8")
    sha_third = git_commit(repo, "fix: third")
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    assert state_of(r.stdout)["commits"] == "2"
    second = ">>> {} feat: second".format(sha_second)
    third = ">>> {} fix: third".format(sha_third)
    assert second in r.stdout
    assert third in r.stdout
    assert r.stdout.index(second) < r.stdout.index(third)  # oldest first
    assert "second body line" in r.stdout


def diff_section(stdout):
    """The lines between the Branch diff header and the next blank line."""
    lines = stdout.splitlines()
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith("## Branch diff (")
    )
    body = []
    for ln in lines[start + 1:]:
        if ln == "":
            break
        body.append(ln)
    return lines[start], body


def test_diff_cap_env_knob(git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    (repo / "big.txt").write_text(
        "".join("line {}\n".format(i) for i in range(50)), encoding="utf-8"
    )
    git_commit(repo, "feat: big file")
    mb = rev(sh, repo, "merge-base", "HEAD", "main")
    full = sh(["git", "diff", "{}..HEAD".format(mb)], cwd=repo).stdout
    total = full.count("\n")
    assert total > 5

    r = prepare(run_script, repo, env={"PR_DIFF_MAX_LINES": "5"})
    assert r.returncode == 0, r.stderr
    header, body = diff_section(r.stdout)
    assert header == (
        "## Branch diff (first 5 of {} lines; read more with: "
        "git diff {}..HEAD -- <path>)".format(total, mb)
    )
    assert body == full.splitlines()[:5]


@pytest.mark.parametrize("bad", ["abc", "", "12x", "-5"])
def test_diff_cap_falls_back_to_600(bad, git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    r = prepare(run_script, repo, env={"PR_DIFF_MAX_LINES": bad})
    assert r.returncode == 0, r.stderr
    header, _ = diff_section(r.stdout)
    assert header.startswith("## Branch diff (first 600 of ")


def test_language_recorded_under_shared_key(git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    r = prepare(run_script, repo, "zh")
    assert r.returncode == 0, r.stderr
    assert state_of(r.stdout)["lang"] == "zh"
    # The record lands under the commit skill's key, so one setting covers both.
    assert rev(sh, repo, "config", "--local", "--get", "skills.lang") == "zh"
    # A later run with no argument picks the record up.
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    assert state_of(r.stdout)["lang"] == "zh"


def test_unknown_language_dies(git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    r = prepare(run_script, repo, "fr")
    assert r.returncode == 1
    assert "unknown language 'fr' (expected zh or en)" in r.stderr


@pytest.mark.parametrize(
    "candidate",
    [
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
        "pull_request_template.md",
        "docs/PULL_REQUEST_TEMPLATE.md",
        "docs/pull_request_template.md",
    ],
)
def test_template_candidate_locations(candidate, git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    path = repo / candidate
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "## Summary\n\n- template body\n"
    path.write_text(content, encoding="utf-8")
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    line = next(
        ln for ln in r.stdout.splitlines() if ln.startswith("## PR template (")
    )
    # Case-insensitive filesystems (macOS) satisfy an earlier candidate whose
    # name differs only by case, exactly as the shell's [ -f ] did.
    assert candidate.lower() in line.lower()
    assert line.endswith("), its sections are required")
    # The template is the last section: content verbatim, no trailing blank.
    assert r.stdout.endswith("its sections are required\n" + content)


def test_template_directory_pick_is_the_ports_own_determinism_guarantee(
    git_repo, git_commit, sh, run_script
):
    """Which of several .github/PULL_REQUEST_TEMPLATE/*.md files wins.

    The shell ran `find ... | head -1`, whose order the filesystem decides — it
    never promised a particular file, so this is NOT ported behavior. The port
    sorts the glob and therefore always picks the lexicographically first name;
    the assertion below pins the PORT's own guarantee, so a future refactor
    cannot quietly make the pick depend on directory order again.
    """
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    tdir = repo / ".github" / "PULL_REQUEST_TEMPLATE"
    tdir.mkdir(parents=True)
    (tdir / "zz.md").write_text("zz template\n", encoding="utf-8")
    (tdir / "aa.md").write_text("aa template\n", encoding="utf-8")
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    assert (
        "## PR template (.github/PULL_REQUEST_TEMPLATE/aa.md), "
        "its sections are required" in r.stdout
    )
    assert r.stdout.endswith("aa template\n")


def test_template_none(git_repo, git_commit, sh, run_script):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    assert r.stdout.endswith("## PR template: NONE\n")


def test_not_a_repo(tmp_path, run_script):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = run_script("create-pr", "pr_helper.py", "prepare", cwd=plain)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "STATE repo=no\n"


def test_usage_failure_exit_1(run_script, tmp_path):
    for args in ([], ["frobnicate"]):
        r = run_script("create-pr", "pr_helper.py", *args, cwd=tmp_path)
        assert r.returncode == 1
        assert r.stderr.rstrip("\n") == USAGE


# ---- the gh-success branch (in-process, gh faked at the _gh seam) ------------

REPO_VIEW = ["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"]
PR_VIEW = [
    "gh", "pr", "view",
    "--json", "number,state,isDraft,title,url,baseRefName,body",
]

BOM = "\ufeff"  # spelled as an escape so this file stays BOM-free
GH_ABSENT = RunResult("", "gh: command not found", 127)
GH_UNAUTHENTICATED = RunResult("", "gh auth login required", 4)
NO_PR = RunResult("", "no pull requests found for branch", 1)


def repo_meta(payload, rc=0, err=""):
    """One `gh repo view --json nameWithOwner,defaultBranchRef` answer."""
    return RunResult(json.dumps(payload) + "\n", err, rc)


class FakeGh:
    """Serves pr_helper's two gh calls and records the argv. gh is the only
    boundary faked: git still runs for real in the throwaway repo."""

    def __init__(self, repo=GH_ABSENT, pr=NO_PR):
        self.repo = repo
        self.pr = pr
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if list(cmd) == REPO_VIEW:
            return self.repo
        if list(cmd) == PR_VIEW:
            return self.pr
        raise AssertionError("unexpected gh call: {}".format(list(cmd)))


# The env vars conftest pins for isolation, and the seams/knobs it clears; the
# in-process tests need both applied to THIS process, not to a child's env.
_ISOLATION_VARS = (
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GH_CONFIG_DIR",
    "GH_PROMPT_DISABLED",
    "GH_NO_UPDATE_NOTIFIER",
)
_CLEARED_VARS = (
    "PR_DIFF_MAX_LINES",
    "SKILLS_LANG_KEY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GH_REPO",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


@pytest.fixture
def enter_repo(monkeypatch, isolated_env):
    """chdir into a throwaway repo with conftest's isolation applied in-process,
    so the git calls below read the same pinned config the subprocess tests get
    and no developer env var leaks into a seam."""

    def enter(repo):
        for var in _ISOLATION_VARS:
            monkeypatch.setenv(var, isolated_env[var])
        for var in _CLEARED_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.chdir(repo)

    return enter


@pytest.fixture
def prepare_inproc(monkeypatch, enter_repo, capsys):
    """Run cmd_prepare inside `repo` with `gh` serving every gh call."""

    def run(repo, gh, *args):
        enter_repo(repo)
        monkeypatch.setattr(pr_helper, "_gh", gh)
        assert pr_helper.cmd_prepare(list(args)) == 0
        return capsys.readouterr().out

    return run


def test_gh_success_supplies_slug_and_default_base(
    git_repo, git_commit, sh, prepare_inproc
):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    gh = FakeGh(
        repo=repo_meta(
            {"nameWithOwner": "octo/hello", "defaultBranchRef": {"name": "trunk"}}
        )
    )
    st = state_of(prepare_inproc(repo, gh))
    assert st["gh"] == "yes"
    assert st["repo_slug"] == "octo/hello"  # what permalink citations are built from
    assert st["default_base"] == "trunk"
    assert st["base"] == "trunk"  # no base argument: the repo's default wins
    # Exactly the two calls the shell made, with the shell's --json field lists.
    assert gh.calls == [REPO_VIEW, PR_VIEW]


def test_a_bom_prefixed_repo_view_is_still_parsed(
    git_repo, git_commit, sh, prepare_inproc
):
    """jq skipped a leading BOM and this parse is the only source of repo_slug.

    Without the skip the whole answer is discarded, so a stray byte-order mark
    (a proxy, a re-encoded response) silently costs gh=yes and the slug that
    permalink citations are built from. Anything past one BOM stays malformed.
    """
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    payload = {"nameWithOwner": "octo/hello", "defaultBranchRef": {"name": "trunk"}}
    good = repo_meta(payload)
    st = state_of(
        prepare_inproc(repo, FakeGh(repo=RunResult(BOM + good.out, "", 0)))
    )
    assert st["gh"] == "yes"
    assert st["repo_slug"] == "octo/hello"
    assert st["default_base"] == "trunk"

    doubled = state_of(
        prepare_inproc(repo, FakeGh(repo=RunResult(BOM + BOM + good.out, "", 0)))
    )
    assert doubled["gh"] == "no"
    assert doubled["repo_slug"] == "unknown"


def test_explicit_base_argument_beats_the_default_branch(
    git_repo, git_commit, sh, prepare_inproc
):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    gh = FakeGh(
        repo=repo_meta(
            {"nameWithOwner": "octo/hello", "defaultBranchRef": {"name": "trunk"}}
        )
    )
    st = state_of(prepare_inproc(repo, gh, "en", "main"))
    assert st["base"] == "main"
    assert st["default_base"] == "trunk"  # still reported, for the model's benefit


def test_null_default_branch_ref_keeps_gh_yes_and_the_slug(
    git_repo, git_commit, sh, prepare_inproc
):
    """A GitHub repo with nothing pushed yet answers defaultBranchRef: null.

    jq's `.nameWithOwner + " " + .defaultBranchRef.name` concatenated the slug
    with null, so the shell saw a non-empty meta, kept gh=yes with the right
    slug, and ended up with an EMPTY default_base. The port keeps the two
    usable facts (gh answered, and here is the slug) and substitutes the `main`
    default only for the field gh could not supply.
    """
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    gh = FakeGh(
        repo=repo_meta({"nameWithOwner": "octo/fresh", "defaultBranchRef": None})
    )
    st = state_of(prepare_inproc(repo, gh))
    assert st["gh"] == "yes"
    assert st["repo_slug"] == "octo/fresh"
    assert st["default_base"] == "main"
    assert st["base"] == "main"


def test_missing_name_with_owner_reports_gh_no_but_keeps_the_branch(
    git_repo, git_commit, sh, prepare_inproc
):
    """The mirror image of the null-defaultBranchRef case, and a deviation.

    jq's `.nameWithOwner + " " + .defaultBranchRef.name` turned a null slug into
    a non-empty " trunk", so the shell reported gh=yes with an EMPTY repo_slug —
    the field it would have pasted into permalink citations. The port reads the
    two fields independently: no usable nameWithOwner means no permalink base,
    so gh=no with repo_slug=unknown, while the default branch that DID come back
    still beats guessing "main".
    """
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    gh = FakeGh(repo=repo_meta({"defaultBranchRef": {"name": "trunk"}}))
    st = state_of(prepare_inproc(repo, gh))
    assert st["gh"] == "no"
    assert st["repo_slug"] == "unknown"
    assert st["default_base"] == "trunk"


@pytest.mark.parametrize(
    "answer",
    [
        GH_ABSENT,
        GH_UNAUTHENTICATED,
        RunResult("", "", 0),
        RunResult("not json\n", "", 0),
    ],
    ids=["absent", "unauthenticated", "empty", "malformed"],
)
def test_unusable_gh_answers_degrade_to_gh_no(
    answer, git_repo, git_commit, sh, prepare_inproc
):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    st = state_of(prepare_inproc(repo, FakeGh(repo=answer)))
    assert st["gh"] == "no"
    assert st["repo_slug"] == "unknown"
    assert st["default_base"] == "main"


def test_non_zero_gh_that_still_printed_usable_json_is_believed(
    git_repo, git_commit, sh, prepare_inproc
):
    # gh can print the answer and then exit non-zero (a partial GraphQL error,
    # a failed update check). The shell captured stdout with `|| true` and used
    # it; so does the port — an answer that parses is an answer.
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    gh = FakeGh(
        repo=repo_meta(
            {"nameWithOwner": "octo/hello", "defaultBranchRef": {"name": "main"}},
            rc=1,
            err="warning: could not read the update check",
        )
    )
    st = state_of(prepare_inproc(repo, gh))
    assert st["gh"] == "yes"
    assert st["repo_slug"] == "octo/hello"
    assert st["default_base"] == "main"


def test_existing_pr_json_is_passed_through_verbatim(
    git_repo, git_commit, sh, prepare_inproc
):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    body = json.dumps(
        {
            "number": 7,
            "state": "OPEN",
            "isDraft": False,
            "title": "feat: ship it 🚀",
            "url": "https://github.com/octo/hello/pull/7",
            "baseRefName": "main",
            "body": "## Summary\n\n- did the thing\n",
        }
    ) + "\n"
    gh = FakeGh(
        repo=repo_meta(
            {"nameWithOwner": "octo/hello", "defaultBranchRef": {"name": "main"}}
        ),
        pr=RunResult(body, "", 0),
    )
    out = prepare_inproc(repo, gh)
    assert "## Existing PR (NONE means create a new one)\n" + body + "\n" in out
    assert "\nNONE\n" not in out  # a PR exists: no NONE marker anywhere


# ---- documented deviations from pr-helper.sh --------------------------------


def test_unborn_head_prints_every_section_and_exits_zero(git_repo, run_script):
    """DEVIATION (deliberate): a repo with no commits at all.

    `git diff HEAD` fails there, and in the shell that call sat in the one
    unguarded pipeline (`git diff | wc -l`) under `set -euo pipefail`, so the
    script aborted with exit 128 right after the Files changed section. A fresh
    `git init` is exactly a state this skill exists to describe, so the port
    ignores the rc, prints the remaining sections and exits 0. The STATE line
    the caller routes on came out before that point either way.
    """
    repo = git_repo()
    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    st = state_of(r.stdout)
    assert st["repo"] == "yes"
    assert st["branch"] == "main"  # the unborn branch still has a name
    assert st["commits"] == "0"
    assert st["merge_base"] == "none"
    assert st["head_sha"] == "HEAD"  # rev-parse echoes an unresolvable arg
    assert st["upstream"] == "none"
    assert st["ahead"] == "n/a"
    # The sections after the abort point are all present, and nothing was said
    # on stderr about the diffs that could not be computed.
    assert "## Files changed\n" in r.stdout
    assert (
        "## Branch diff (first 600 of 0 lines; read more with: "
        "git diff HEAD..HEAD -- <path>)"
    ) in r.stdout
    assert r.stdout.endswith("## PR template: NONE\n")
    assert r.stderr == ""


def test_unresolvable_push_target_reports_none_not_the_literal(
    git_repo, git_commit, sh, run_script
):
    """DEVIATION (deliberate, and a fix): @{push} configured but unresolvable.

    With branch.<name>.merge pointing at a ref that no longer exists,
    `git rev-parse --abbrev-ref --symbolic-full-name @{push}` ECHOES the literal
    string "@{push}" on stdout while exiting 128. The shell captured stdout
    regardless of the rc and reported upstream=@{push} with ahead=0, i.e.
    "already pushed, nothing new" — the one conclusion that makes a caller skip
    the push. The port gates on the rc and tells the truth.
    """
    repo = git_repo()
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    git_commit(repo, "feat: base commit")
    for key, value in (
        ("remote.origin.url", "https://example.invalid/octo/hello.git"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ("branch.main.remote", "origin"),
        ("branch.main.merge", "refs/heads/pruned"),
        # push.default=simple refuses differently; upstream reproduces the echo.
        ("push.default", "upstream"),
    ):
        assert sh(["git", "config", key, value], cwd=repo).returncode == 0

    r = prepare(run_script, repo)
    assert r.returncode == 0, r.stderr
    st = state_of(r.stdout)
    assert st["upstream"] == "none"
    assert st["ahead"] == "n/a"
    assert "@{push}" not in r.stdout


def test_crlf_template_is_copied_through_byte_for_byte(
    git_repo, git_commit, sh, enter_repo, monkeypatch, capsysbinary
):
    """DEVIATION (deliberate): the template is `cat`-ed, not read as text.

    A CRLF template is normal on Windows; a text-mode read would rewrite every
    CRLF to a bare LF and re-encode the content, handing the model something
    that is not what the repo asks contributors to fill in.
    """
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    raw = b"## Summary\r\n\r\n- keep my CRLFs\r\n"
    (repo / "PULL_REQUEST_TEMPLATE.md").write_bytes(raw)
    enter_repo(repo)
    monkeypatch.setattr(pr_helper, "_gh", FakeGh())
    assert pr_helper.cmd_prepare([]) == 0
    out = capsysbinary.readouterr().out
    assert out.endswith(b"its sections are required\n" + raw)
    assert out.count(b"\r\n") == 3  # every CRLF survived


def test_non_utf8_template_is_neither_a_crash_nor_mangled(
    git_repo, git_commit, sh, enter_repo, monkeypatch, capsysbinary
):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    raw = "## Résumé\n".encode("latin-1") + b"- \xff\xfe not utf-8\n"
    (repo / "PULL_REQUEST_TEMPLATE.md").write_bytes(raw)
    enter_repo(repo)
    monkeypatch.setattr(pr_helper, "_gh", FakeGh())
    assert pr_helper.cmd_prepare([]) == 0
    assert capsysbinary.readouterr().out.endswith(
        b"its sections are required\n" + raw
    )


# ---- main()'s error handling -------------------------------------------------


class ClosedPipe(io.StringIO):
    """A stdout whose consumer has gone away: every write raises."""

    def write(self, s):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


class PipeDiesOnFlush(io.StringIO):
    """A stdout that takes the writes and only discovers the dead pipe when the
    buffered output is flushed."""

    def flush(self):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


@pytest.mark.parametrize(
    "stdout", [ClosedPipe, PipeDiesOnFlush], ids=["write", "flush"]
)
def test_broken_pipe_exits_quietly(stdout, monkeypatch, tmp_path, capsys):
    # The skill documents piping this output, so a consumer that stops reading
    # (`| head`, a model that got its STATE line) must get silence and a plain
    # exit status, never a traceback plus the interpreter's shutdown noise.
    # 141 is 128+SIGPIPE, the status the shell script died with. The flush case
    # is the one that has to be caught inside main: output still sitting in the
    # buffer would otherwise blow up in the interpreter's exit-time flush.
    monkeypatch.chdir(tmp_path)  # not a repo: STATE repo=no is the only output
    monkeypatch.setattr(sys, "stdout", stdout())
    assert pr_helper.main(["prepare"]) == 141
    assert capsys.readouterr().err == ""


def _lowest_free_fd():
    """The lowest unused descriptor number — it goes UP when a call leaks one."""
    fd = os.open(os.devnull, os.O_WRONLY)
    os.close(fd)
    return fd


def test_mute_stdout_leaks_no_descriptor_without_a_real_stdout(monkeypatch):
    # The in-process path: sys.stdout.fileno() raises io.UnsupportedOperation,
    # so dup2 never runs and the devnull fd used to leak outright — one per
    # call, with nothing ever installed to justify it.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    before = _lowest_free_fd()
    for _ in range(5):
        pr_helper._mute_stdout()
    assert _lowest_free_fd() == before


def test_mute_stdout_closes_the_null_fd_after_dup2(tmp_path, monkeypatch):
    # The success path: dup2 DUPLICATES the descriptor onto stdout's, so the
    # original is redundant and must be closed. A real file stands in for
    # stdout, so the redirection lands on it and not on the test runner's.
    target = open(str(tmp_path / "out.txt"), "w", encoding="utf-8")
    try:
        monkeypatch.setattr(sys, "stdout", target)
        before = _lowest_free_fd()
        for _ in range(5):
            pr_helper._mute_stdout()
        assert _lowest_free_fd() == before
    finally:
        target.close()


def test_other_oserrors_become_one_tidy_stderr_line(
    git_repo, git_commit, sh, enter_repo, monkeypatch, capsys
):
    repo, _ = make_branch_repo(git_repo, git_commit, sh)
    enter_repo(repo)
    monkeypatch.setattr(pr_helper, "_gh", FakeGh())
    # The template vanished between find_pr_template's isfile check and the
    # read (or is unreadable): one line, not a traceback.
    monkeypatch.setattr(
        pr_helper, "find_pr_template", lambda: "PULL_REQUEST_TEMPLATE.md"
    )
    assert pr_helper.main(["prepare"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("pr_helper.py: ")
    assert err.count("\n") == 1
    assert "Traceback" not in err
