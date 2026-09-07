"""Tests for ship-pr/scripts/pr_local_cleanup.py.

The script's value is its GATES: every refusal must leave the checkout
untouched and say exactly why (exit 3 = deliberate skip, 0 = cleaned or
already clean, 2 = usage, 1 = unexpected error). Gate logic runs in-process
against real throwaway git repos (monkeypatch.chdir) with the gh seams
monkeypatched — no network, no gh auth. "Remote" operations (pull, the
pull/N/head containment fetch) run against a local bare repo over file paths;
refs/pull/N/head is planted there with `git update-ref` so the safe-delete
proof works offline. Argument/usage paths run as subprocesses; the only gh
invocation they can reach is one whose failure is the expected outcome.
"""

from __future__ import annotations

import json

import pytest

import pr_local_cleanup as plc


# --- helpers ---------------------------------------------------------------

def pr_json(state="MERGED", head="feature", base="main", oid="", cross=False):
    return json.dumps(
        {
            "state": state,
            "headRefName": head,
            "baseRefName": base,
            "headRefOid": oid,
            "isCrossRepository": cross,
        }
    )


def wire_gh(monkeypatch, *, cur="owner/repo", payload=""):
    """Point the gh seams at a scripted repo slug and PR payload."""
    monkeypatch.setattr(plc, "_gh_current_repo", lambda: cur)
    monkeypatch.setattr(plc, "_gh_pr_json", lambda pr, slug: payload)


def current_branch(sh, repo):
    return sh(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo).stdout.strip()


def branch_exists(sh, repo, name):
    r = sh(["git", "show-ref", "--verify", "--quiet", "refs/heads/" + name], cwd=repo)
    return r.returncode == 0


def rev(sh, repo, ref):
    return sh(["git", "rev-parse", ref], cwd=repo).stdout.strip()


@pytest.fixture
def clean_env(isolated_env, monkeypatch):
    """In-process main() shells out to git; give those subprocesses the same
    isolated environment conftest gives script subprocesses, and pin git
    messages to English so the 'Already up to date' detection is testable."""
    for key in (
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GH_CONFIG_DIR",
    ):
        monkeypatch.setenv(key, isolated_env[key])
    for key in ("GH_REPO", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")


@pytest.fixture
def merged_pair(git_repo, git_commit, sh, tmp_path):
    """A work checkout plus a bare remote where PR feature->main has merged:
    remote main == the feature tip, local main is stale, HEAD is on feature
    (the state ship-pr's remote-only merge leaves the user in)."""

    def make(remote_name="origin"):
        work = git_repo("work")
        (work / "a.txt").write_text("one\n", encoding="utf-8")
        git_commit(work, "c1")
        bare = tmp_path / "remote.git"
        r = sh(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
        assert r.returncode == 0, r.stderr
        sh(["git", "remote", "add", remote_name, str(bare)], cwd=work)
        assert sh(["git", "push", "-q", remote_name, "main"], cwd=work).returncode == 0
        sh(["git", "switch", "-q", "-c", "feature"], cwd=work)
        (work / "b.txt").write_text("two\n", encoding="utf-8")
        git_commit(work, "c2")
        assert sh(["git", "push", "-q", remote_name, "feature"], cwd=work).returncode == 0
        # "Merge" the PR remotely: remote main fast-forwards to the feature tip.
        assert (
            sh(["git", "push", "-q", remote_name, "feature:main"], cwd=work).returncode
            == 0
        )
        fsha = rev(sh, work, "refs/heads/feature")
        return work, bare, fsha

    return make


# --- argument parsing / usage (subprocess: no gh needed, or gh-fails path) --

def test_help_prints_usage_and_exits_zero(run_script):
    r = run_script("ship-pr", "pr_local_cleanup.py", "-h")
    assert r.returncode == 0
    assert r.stdout == plc.USAGE + "\n"


def test_missing_base_value_is_usage_error(run_script):
    r = run_script("ship-pr", "pr_local_cleanup.py", "7", "--base")
    assert r.returncode == 2
    assert r.stderr == "missing value for --base\n"


def test_unknown_flag_is_usage_error(run_script):
    r = run_script("ship-pr", "pr_local_cleanup.py", "--wat")
    assert r.returncode == 2
    assert r.stderr == "unknown flag: --wat\n"


def test_no_pr_and_undetectable_pr_is_usage_error(run_script, tmp_path):
    # Outside any repo and without gh auth the PR probe fails (or gh is
    # absent, rc 127) — both are the expected-failure path here.
    r = run_script("ship-pr", "pr_local_cleanup.py", cwd=tmp_path)
    assert r.returncode == 2
    assert r.stderr == plc.USAGE + "\n"


def test_in_process_usage_when_pr_undetectable(clean_env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(plc, "_gh_detect_pr", lambda repo: "")
    monkeypatch.chdir(tmp_path)
    assert plc.main([]) == 2
    assert capsys.readouterr().err == plc.USAGE + "\n"


def test_pr_autodetect_forwards_repo_and_is_used(
    clean_env, monkeypatch, capsys, git_repo
):
    seen = []

    def fake_detect(repo):
        seen.append(repo)
        return "42"

    monkeypatch.setattr(plc, "_gh_detect_pr", fake_detect)
    wire_gh(monkeypatch, cur="own/rep", payload=pr_json(state="OPEN"))
    monkeypatch.chdir(git_repo("detect"))
    assert plc.main(["own/rep"]) == 3
    assert seen == ["own/rep"]
    out = capsys.readouterr().out
    assert out == "SKIPPED: PR #42 is OPEN, not MERGED — local branch left in place.\n"


# --- the gates, in order ----------------------------------------------------

def test_skip_outside_work_tree(clean_env, monkeypatch, capsys, tmp_path):
    # The gh seams must not even be consulted before the work-tree gate.
    def boom(*a, **k):
        raise AssertionError("gh consulted before the work-tree gate")

    monkeypatch.setattr(plc, "_gh_current_repo", boom)
    monkeypatch.chdir(tmp_path)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: not inside a git working tree — nothing local to tidy up.\n"
    )


def test_skip_when_repo_unresolvable(clean_env, monkeypatch, capsys, git_repo):
    wire_gh(monkeypatch, cur="")
    monkeypatch.chdir(git_repo())
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: this directory has no resolvable GitHub repo — leaving the local side alone.\n"
    )


def test_skip_on_repo_mismatch(clean_env, monkeypatch, capsys, git_repo):
    wire_gh(monkeypatch, cur="owner/repo")
    monkeypatch.chdir(git_repo())
    assert plc.main(["7", "other/repo"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: current repo (owner/repo) isn't the PR's repo (other/repo) — leaving your checkout untouched.\n"
    )


def test_bare_second_word_is_repo(clean_env, monkeypatch, capsys, git_repo):
    # `*)` in the shell case: first bare word is the PR, the next one the repo.
    wire_gh(monkeypatch, cur="owner/repo")
    monkeypatch.chdir(git_repo())
    assert plc.main(["7", "elsewhere"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: current repo (owner/repo) isn't the PR's repo (elsewhere) — leaving your checkout untouched.\n"
    )


def test_gh_repo_env_seam_sets_repo(clean_env, monkeypatch, capsys, git_repo):
    wire_gh(monkeypatch, cur="owner/repo")
    monkeypatch.setenv("GH_REPO", "other/repo")
    monkeypatch.chdir(git_repo())
    assert plc.main(["7"]) == 3
    assert "isn't the PR's repo (other/repo)" in capsys.readouterr().out


def test_repo_match_is_case_insensitive_and_unreadable_pr_is_error(
    clean_env, monkeypatch, capsys, git_repo
):
    # GitHub slugs are case-insensitive: OWNER/Repo must pass the mismatch
    # gate; the empty PR read then exits 1 (an error, never a silent skip).
    wire_gh(monkeypatch, cur="owner/repo", payload="")
    monkeypatch.chdir(git_repo())
    assert plc.main(["7", "OWNER/Repo"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "pr-local-cleanup: could not read PR #7 (transient?) — not touching local branches.\n"
    )


@pytest.mark.parametrize("payload", ["\n", "   ", " \t\r\n "])
def test_whitespace_only_pr_read_is_a_failed_read(
    clean_env, monkeypatch, capsys, git_repo, payload
):
    # The shell captured this with `$( )`, which strips trailing newlines, so a
    # reply of just a newline read as EMPTY and exited 1. Any all-whitespace
    # body must stay a failed read: it can never parse, and letting it through
    # degrades every field to "" and reports a skip ("no base branch") for what
    # is really a transient read failure.
    wire_gh(monkeypatch, payload=payload)
    monkeypatch.chdir(git_repo())
    assert plc.main(["7"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "pr-local-cleanup: could not read PR #7 (transient?) — not touching local branches.\n"
    )


def test_skip_when_base_unresolvable(clean_env, monkeypatch, capsys, git_repo):
    wire_gh(monkeypatch, payload=pr_json(base=""))
    monkeypatch.chdir(git_repo())
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: couldn't resolve the base branch for #7.\n"
    )


def test_base_override_fills_missing_base(clean_env, monkeypatch, capsys, git_repo):
    # With --base the missing-base gate passes and the next gate (MERGED)
    # produces the skip — proof the override was applied.
    wire_gh(monkeypatch, payload=pr_json(state="OPEN", base=""))
    monkeypatch.chdir(git_repo())
    assert plc.main(["7", "--base", "main"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: PR #7 is OPEN, not MERGED — local branch left in place.\n"
    )


def test_skip_when_not_merged(clean_env, monkeypatch, capsys, git_repo):
    wire_gh(monkeypatch, payload=pr_json(state="CLOSED"))
    monkeypatch.chdir(git_repo())
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: PR #7 is CLOSED, not MERGED — local branch left in place.\n"
    )


def test_skip_on_uncommitted_changes(clean_env, monkeypatch, capsys, sh, merged_pair):
    work, _, fsha = merged_pair()
    (work / "a.txt").write_text("changed\n", encoding="utf-8")
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: working tree has uncommitted changes — commit or stash them first, then I can tidy up.\n"
    )
    assert current_branch(sh, work) == "feature"  # untouched


def test_skip_on_untracked_only_names_stash_u(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    # The remedy split is the point: plain `git stash` does NOT clear
    # untracked files, so the untracked-only message must differ.
    work, _, fsha = merged_pair()
    (work / "new.txt").write_text("hi\n", encoding="utf-8")
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: working tree has untracked files — commit them, 'git stash -u', or remove them first, then I can tidy up.\n"
    )


def test_mixed_dirty_and_untracked_reports_uncommitted(
    clean_env, monkeypatch, capsys, merged_pair
):
    work, _, fsha = merged_pair()
    (work / "a.txt").write_text("changed\n", encoding="utf-8")
    (work / "new.txt").write_text("hi\n", encoding="utf-8")
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert "uncommitted changes" in capsys.readouterr().out


@pytest.mark.parametrize(
    "marker",
    ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply"],
)
def test_skip_on_in_progress_marker(
    clean_env, monkeypatch, capsys, sh, merged_pair, marker
):
    work, _, fsha = merged_pair()
    content = rev(sh, work, "HEAD") + "\n" if marker.endswith("_HEAD") else ""
    (work / ".git" / marker).write_text(content, encoding="utf-8")
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: a git operation ({}) is in progress — leaving your checkout alone.\n".format(marker)
    )


def test_skip_on_detached_head(clean_env, monkeypatch, capsys, sh, merged_pair):
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "--detach"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: HEAD is detached — not auto-switching. (#7 is merged.)\n"
    )


def test_skip_on_unrelated_branch(clean_env, monkeypatch, capsys, sh, merged_pair):
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "-c", "other"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: you're on 'other', neither the merged branch ('feature') nor the base ('main') — not moving you. (#7 is merged.)\n"
    )


def test_fork_pr_on_head_named_branch_skips(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    # Cross-repo PR: a local branch named like the fork's head is NOT provably
    # the merged branch, so being "on it" counts as being on neither.
    work, _, fsha = merged_pair()
    wire_gh(monkeypatch, payload=pr_json(oid=fsha, cross=True))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: you're on 'feature'; #7 merged from a fork and can't be matched to a local branch. Switch to 'main' yourself if you want a pull. (#7 is merged.)\n"
    )
    assert branch_exists(sh, work, "feature")


# --- dry run ---------------------------------------------------------------

def test_dry_run_reports_full_plan_and_changes_nothing(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    work, _, fsha = merged_pair()
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "DRY-RUN — owner/repo #7 is merged; would:\n"
        "  - switch 'feature' -> 'main'\n"
        "  - fast-forward 'main'\n"
        "  - delete local branch 'feature' (only if proven fully merged; otherwise keep it)\n"
    )
    assert current_branch(sh, work) == "feature"
    assert branch_exists(sh, work, "feature")
    assert rev(sh, work, "refs/heads/main") != fsha  # base not pulled


def test_dry_run_on_base_without_local_head(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "main"], cwd=work)
    sh(["git", "branch", "-q", "-D", "feature"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "DRY-RUN — owner/repo #7 is merged; would:\n"
        "  - fast-forward 'main'\n"
    )


# --- the acting paths ------------------------------------------------------

def test_happy_path_switch_pull_delete_on_exact_sha(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    # Local feature tip == headRefOid: containment is proven without any
    # fetch, so this works even without a pull/N/head ref on the remote.
    work, _, fsha = merged_pair()
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - switched to 'main'\n"
        "  - fast-forwarded 'main'\n"
        "  - deleted merged local branch 'feature'\n"
    )
    assert current_branch(sh, work) == "main"
    assert not branch_exists(sh, work, "feature")
    assert rev(sh, work, "refs/heads/main") == fsha


def test_bom_prefixed_gh_json_still_acts(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    # jq skipped a leading BOM. If the port choked on one, every field would
    # degrade to "" and the base gate would turn this ACT into a silent SKIP —
    # the checkout left on a merged branch with a stale base.
    work, _, fsha = merged_pair()
    wire_gh(monkeypatch, payload="\ufeff" + pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - switched to 'main'\n"
        "  - fast-forwarded 'main'\n"
        "  - deleted merged local branch 'feature'\n"
    )
    assert current_branch(sh, work) == "main"
    assert not branch_exists(sh, work, "feature")


def test_rerun_when_already_clean_is_a_noop(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "main"], cwd=work)
    sh(["git", "merge", "-q", "--ff-only", "feature"], cwd=work)
    sh(["git", "branch", "-q", "-D", "feature"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - nothing to do (already on 'main', merged branch gone)\n"
    )


def test_ahead_local_commits_keep_the_branch(
    clean_env, monkeypatch, capsys, sh, merged_pair, git_commit
):
    # The merged ref is fetchable (pull/7/head planted in the bare remote) but
    # the local tip has a commit the merge never saw — never delete that.
    work, bare, fsha = merged_pair()
    r = sh(["git", "update-ref", "refs/pull/7/head", fsha], cwd=bare)
    assert r.returncode == 0, r.stderr
    (work / "extra.txt").write_text("wip\n", encoding="utf-8")
    ahead = git_commit(work, "c3: unpushed work")
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - switched to 'main'\n"
        "  - fast-forwarded 'main'\n"
        "  - kept 'feature' — it has commits the merge never saw (nothing lost; delete it yourself once you're sure)\n"
    )
    assert branch_exists(sh, work, "feature")
    assert rev(sh, work, "refs/heads/feature").startswith(ahead)


def test_unverifiable_containment_keeps_the_branch(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    # headRefOid doesn't match the local tip and the remote has no
    # refs/pull/7/head (all 3 fetch attempts fail): "couldn't check" must be
    # reported honestly, distinct from "genuinely ahead".
    work, _, fsha = merged_pair()
    wire_gh(monkeypatch, payload=pr_json(oid="0" * 40))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - switched to 'main'\n"
        "  - fast-forwarded 'main'\n"
        "  - kept 'feature' — couldn't verify it's fully merged (left as-is; delete it yourself once you're sure)\n"
    )
    assert branch_exists(sh, work, "feature")


def test_stale_local_tip_contained_in_merged_ref_is_deleted(
    clean_env, monkeypatch, capsys, sh, merged_pair, git_commit
):
    # ship-pr pushed fixes past the user's stale local copy: local tip is an
    # ANCESTOR of the fetched pull/7/head, so force-delete is proven safe.
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "-c", "tmp"], cwd=work)
    (work / "fix.txt").write_text("fix\n", encoding="utf-8")
    git_commit(work, "c3: bot fix")
    merged_tip = rev(sh, work, "HEAD")
    assert (
        sh(["git", "push", "-q", "origin", "tmp:refs/pull/7/head"], cwd=work).returncode
        == 0
    )
    sh(["git", "switch", "-q", "feature"], cwd=work)
    sh(["git", "branch", "-q", "-D", "tmp"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=merged_tip))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - switched to 'main'\n"
        "  - fast-forwarded 'main'\n"
        "  - deleted merged local branch 'feature'\n"
    )
    assert not branch_exists(sh, work, "feature")


def test_fork_pr_on_base_pulls_but_never_deletes(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "main"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=fsha, cross=True))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - fast-forwarded 'main'\n"
    )
    assert branch_exists(sh, work, "feature")


def test_switch_failure_skips_after_no_mutation(
    clean_env, monkeypatch, capsys, sh, merged_pair
):
    # Base branch that exists neither locally nor on the remote: the switch
    # fails, and even though the delete was already proven safe (exact SHA),
    # NOTHING happens.
    work, _, fsha = merged_pair()
    wire_gh(monkeypatch, payload=pr_json(oid=fsha, base="release"))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 3
    assert capsys.readouterr().out == (
        "SKIPPED: couldn't switch to base 'release' (no local branch and no origin/release) — left on 'feature'. (#7 is merged.)\n"
    )
    assert current_branch(sh, work) == "feature"
    assert branch_exists(sh, work, "feature")


def test_no_remote_reports_pull_left_as_is(
    clean_env, monkeypatch, capsys, sh, git_repo, git_commit
):
    # No remotes at all: the pull can't run ("no remote resolved") but an
    # exact-SHA delete still can — no network was needed to prove it.
    work = git_repo("solo")
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    git_commit(work, "c1")
    sh(["git", "switch", "-q", "-c", "feature"], cwd=work)
    (work / "b.txt").write_text("two\n", encoding="utf-8")
    git_commit(work, "c2")
    fsha = rev(sh, work, "refs/heads/feature")
    sh(["git", "switch", "-q", "main"], cwd=work)
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    assert capsys.readouterr().out == (
        "Post-merge local cleanup for owner/repo #7:\n"
        "  - left 'main' as-is (couldn't fast-forward: no remote resolved)\n"
        "  - deleted merged local branch 'feature'\n"
    )
    assert not branch_exists(sh, work, "feature")


def test_diverged_base_reports_first_line_of_pull_output(
    clean_env, monkeypatch, capsys, sh, merged_pair, git_commit
):
    work, _, fsha = merged_pair()
    sh(["git", "switch", "-q", "main"], cwd=work)
    (work / "local.txt").write_text("mine\n", encoding="utf-8")
    git_commit(work, "d1: local divergence")
    wire_gh(monkeypatch, payload=pr_json(oid=fsha))
    monkeypatch.chdir(work)
    assert plc.main(["7"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Post-merge local cleanup for owner/repo #7:"
    assert lines[1].startswith("  - left 'main' as-is (couldn't fast-forward: ")
    assert lines[2] == "  - deleted merged local branch 'feature'"
    assert len(lines) == 3


# --- resolve_remote --------------------------------------------------------

def test_resolve_remote_prefers_configured_branch_remote(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("rr1")
    git_commit(repo)
    sh(["git", "remote", "add", "alpha", "https://github.com/Owner/Repo.git"], cwd=repo)
    sh(["git", "remote", "add", "beta", "https://example.com/elsewhere.git"], cwd=repo)
    sh(["git", "config", "branch.main.remote", "beta"], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.resolve_remote("main", "Owner/Repo") == "beta"


def test_resolve_remote_ignores_config_naming_missing_remote(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("rr2")
    git_commit(repo)
    sh(["git", "remote", "add", "alpha", "https://github.com/Owner/Repo.git"], cwd=repo)
    sh(["git", "config", "branch.main.remote", "ghost"], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.resolve_remote("main", "Owner/Repo") == "alpha"


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Owner/Repo.git",
        "https://github.com/Owner/Repo",
        "ssh://git@github.com/Owner/Repo/",
    ],
)
def test_resolve_remote_matches_slug_url_forms(
    clean_env, monkeypatch, sh, git_repo, git_commit, url
):
    # Two remotes and no origin, so only the URL match can pick one.
    repo = git_repo("rr3")
    git_commit(repo)
    sh(["git", "remote", "add", "aaa", "https://example.com/x.git"], cwd=repo)
    sh(["git", "remote", "add", "zzz", url], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.resolve_remote("main", "Owner/Repo") == "zzz"


def test_resolve_remote_falls_back_to_origin(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("rr4")
    git_commit(repo)
    sh(["git", "remote", "add", "origin", "https://example.com/a.git"], cwd=repo)
    sh(["git", "remote", "add", "up", "https://example.com/b.git"], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.resolve_remote("main", "Owner/Repo") == "origin"


def test_resolve_remote_uses_sole_remote(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("rr5")
    git_commit(repo)
    sh(["git", "remote", "add", "up", "https://example.com/b.git"], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.resolve_remote("main", "Owner/Repo") == "up"


def test_resolve_remote_gives_up_without_candidates(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("rr6")
    git_commit(repo)
    sh(["git", "remote", "add", "aaa", "https://example.com/a.git"], cwd=repo)
    sh(["git", "remote", "add", "bbb", "https://example.com/b.git"], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.resolve_remote("main", "Owner/Repo") == ""


# --- switch_to -------------------------------------------------------------

def test_switch_to_existing_local_branch(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("sw1")
    git_commit(repo)
    sh(["git", "branch", "-q", "dev"], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.switch_to("dev", "") is True
    assert current_branch(sh, repo) == "dev"


def test_switch_to_creates_tracking_branch_from_remote(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("sw2")
    git_commit(repo)
    sha = rev(sh, repo, "HEAD")
    sh(["git", "remote", "add", "origin", "https://example.com/r.git"], cwd=repo)
    sh(["git", "update-ref", "refs/remotes/origin/dev", sha], cwd=repo)
    monkeypatch.chdir(repo)
    assert plc.switch_to("dev", "origin") is True
    assert current_branch(sh, repo) == "dev"


def test_switch_to_missing_branch_fails(
    clean_env, monkeypatch, sh, git_repo, git_commit
):
    repo = git_repo("sw3")
    git_commit(repo)
    monkeypatch.chdir(repo)
    assert plc.switch_to("nope", "") is False
    assert plc.switch_to("nope", "origin") is False
    assert current_branch(sh, repo) == "main"


# --- pure helpers ----------------------------------------------------------

def test_classify_porcelain():
    assert plc.classify_porcelain("") is None
    assert plc.classify_porcelain("?? a.txt") == "untracked"
    assert plc.classify_porcelain("?? a.txt\n?? b.txt") == "untracked"
    assert plc.classify_porcelain(" M a.txt") == "dirty"
    assert plc.classify_porcelain("?? a.txt\n M b.txt") == "dirty"
    assert plc.classify_porcelain("A  staged.txt") == "dirty"


def test_url_matches_slug():
    slug = "Owner/Repo"
    assert plc.url_matches_slug("git@github.com:Owner/Repo", slug)
    assert plc.url_matches_slug("git@github.com:Owner/Repo.git", slug)
    assert plc.url_matches_slug("https://github.com/Owner/Repo/", slug)
    # The separator before the slug is required (shell class [:/]) …
    assert not plc.url_matches_slug("https://github.com/prefixOwner/Repo", slug)
    # … and the shell case match is case-sensitive.
    assert not plc.url_matches_slug("https://github.com/owner/repo.git", slug)
    assert not plc.url_matches_slug("https://example.com/Other/Thing.git", slug)


def test_parse_pr_fields():
    payload = pr_json(state="MERGED", head="f", base="m", oid="abc", cross=True)
    assert plc.parse_pr_fields(payload) == ("MERGED", "f", "m", "abc", True)
    # Unreadable JSON degrades to empty fields like the shell's failed jq.
    assert plc.parse_pr_fields("not json") == ("", "", "", "", False)
    assert plc.parse_pr_fields("[1,2]") == ("", "", "", "", False)
    # jq's `// false` text comparison: only true / "true" pass.
    assert plc.parse_pr_fields('{"isCrossRepository":"true"}')[4] is True
    assert plc.parse_pr_fields('{"isCrossRepository":false}')[4] is False
    # A leading BOM is skipped, not "unreadable" — jq read straight through it.
    assert plc.parse_pr_fields("\ufeff" + payload) == ("MERGED", "f", "m", "abc", True)


def test_pull_did_entry():
    # The up-to-date spellings win even over a non-zero rc (case before rc).
    assert plc.pull_did_entry("Already up to date.", 0, "main") is None
    assert plc.pull_did_entry("From x\nAlready up-to-date.", 1, "main") is None
    assert plc.pull_did_entry("Updating a..b\nFast-forward", 0, "main") == (
        "fast-forwarded 'main'"
    )
    assert plc.pull_did_entry("fatal: refusing\nmore detail", 1, "main") == (
        "left 'main' as-is (couldn't fast-forward: fatal: refusing)"
    )
