"""Tests for ship-pr/scripts/pr_merge.py.

Merging is the one irreversible thing these scripts do, so both gh seams and
the pr_state snapshot are faked and the tests assert WHETHER the merge call was
issued at all, not just the exit code: the already-merged short circuit must
never re-merge, a refused or degraded snapshot must never green-light one, and
a merge that fatals locally but landed remotely must read as success.

The refusal report is pinned byte-for-byte as this port's OWN contract, not as
ported behavior: pr-merge.sh discarded those lines through a redirect-ordering
bug (see refusal_report's docstring) and only ever printed the REFUSING line
and the closing hint. The line formatting still follows the shell's jq, whose
array interpolation is compact JSON. No network, no gh binary.
"""

from __future__ import annotations

import json

import pytest

import pr_merge
from gh_retry import RunResult

PR = "7"
SLUG = "octo/widget"
BOM = "\ufeff"  # gh output that arrived through a re-encoding proxy

NOT_READY = {
    "pr": 7,
    "state": "OPEN",
    "title": "feat: add the widget",
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "BLOCKED",
    "threads_fetched": True,
    "ci_all_pass": False,
    "ci_failing": ["build (ubuntu)", "test"],
    "review_bot_checks": [
        {"name": "CodeRabbit", "bucket": "pass"},
        {"name": "Sourcery", "bucket": "fail"},
    ],
    "review_threads_unresolved": 2,
    "ready_to_merge": False,
}

READY = dict(
    NOT_READY,
    mergeStateStatus="CLEAN",
    ci_all_pass=True,
    ci_failing=[],
    review_bot_checks=[{"name": "CodeRabbit", "bucket": "pass"}],
    review_threads_unresolved=0,
    ready_to_merge=True,
)

# What a transient GraphQL EOF leaves behind: threads unknown, so every
# thread-derived field is null and ready_to_merge is false.
DEGRADED = dict(
    NOT_READY,
    threads_fetched=False,
    ci_all_pass=True,
    ci_failing=[],
    review_bot_checks=[],
    review_threads_unresolved=None,
    mergeable=None,
    mergeStateStatus=None,
)


def state_json(state=None, oid=None):
    """A `gh pr view --json state,mergeCommit` payload."""
    doc = {"state": state or "OPEN"}
    doc["mergeCommit"] = {"oid": oid} if oid else None
    return json.dumps(doc)


OPEN = state_json()
MERGED = state_json("MERGED", "abc1234")


def commit(message):
    return {"commit": {"message": message}}


class FakeGh:
    """The retried gh boundary. `views` is a queue of `gh pr view
    --json state,mergeCommit` payloads (the before/after idempotency probes);
    everything else answers from fixed values and is recorded."""

    def __init__(self, views=(OPEN, MERGED), title="feat: add the widget", commits="[]"):
        self.views = list(views)
        self.title = title
        self.commits = commits
        self.calls = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"] and "state,mergeCommit" in cmd:
            return RunResult(self.views.pop(0) + "\n", "", 0)
        if cmd[:3] == ["gh", "pr", "view"] and "title" in cmd:
            return RunResult(self.title + "\n", "", 0)
        if cmd[:2] == ["gh", "api"]:
            return RunResult(self.commits, "", 0)
        if cmd[:3] == ["gh", "pr", "merge"]:
            # gh can fatal on the local fast-forward while the remote merge
            # lands; the caller must ignore this result entirely.
            return RunResult("", "fatal: Not possible to fast-forward, aborting.", 1)
        raise AssertionError("unexpected gh call: {}".format(cmd))

    @property
    def merges(self):
        return [c for c in self.calls if c[:3] == ["gh", "pr", "merge"]]

    @property
    def api_calls(self):
        return [c for c in self.calls if c[:2] == ["gh", "api"]]


class FakePlain:
    """The non-retried resolution boundary (`gh pr view --json number`,
    `gh repo view`). Unset answers are empty, like a gh that failed."""

    def __init__(self, number="", ownername=""):
        self.number = number
        self.ownername = ownername
        self.calls = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "repo", "view"]:
            return RunResult(self.ownername + "\n" if self.ownername else "", "", 0)
        return RunResult(self.number + "\n" if self.number else "", "", 0)


@pytest.fixture
def seams(monkeypatch):
    """Install the gh seams plus a snapshot stub; returns a setup callable."""
    monkeypatch.delenv("GH_REPO", raising=False)  # a real one must never leak in

    def install(gh=None, plain=None, state=READY):
        gh = gh if gh is not None else FakeGh()
        plain = plain if plain is not None else FakePlain()
        collected = []

        def fake_collect(pr, repo="", **kwargs):
            collected.append((pr, repo))
            if isinstance(state, Exception):
                raise state
            return state

        monkeypatch.setattr(pr_merge, "_gh", gh)
        monkeypatch.setattr(pr_merge, "_gh_plain", plain)
        monkeypatch.setattr(pr_merge, "collect_state", fake_collect)
        return gh, plain, collected

    return install


# --- already merged --------------------------------------------------------


def test_already_merged_short_circuits_without_merging(seams, capsys):
    gh, _, collected = seams(FakeGh(views=[MERGED]))
    assert pr_merge.main([PR, SLUG]) == 0
    assert capsys.readouterr().out == "already merged: octo/widget #7 (abc1234)\n"
    assert gh.merges == []  # never re-merge
    assert collected == []  # and never even ask for a snapshot


def test_already_merged_without_a_commit_oid_prints_the_literal(seams, capsys):
    # `.mergeCommit.oid // "merged"` — a merged PR whose commit gh withheld.
    gh, _, _ = seams(FakeGh(views=[state_json("MERGED")]))
    assert pr_merge.main([PR, SLUG]) == 0
    assert capsys.readouterr().out == "already merged: octo/widget #7 (merged)\n"
    assert gh.merges == []


def test_a_bom_prefixed_state_probe_still_reads_as_merged(seams, capsys):
    # jq skipped a leading BOM; json.loads refuses one. This probe is what
    # stops a second merge, so a BOM must not make an already-MERGED PR look
    # open — hence gh_retry.loads_json here (see merged_oid).
    gh, _, collected = seams(FakeGh(views=[BOM + MERGED]))
    assert pr_merge.main([PR, SLUG]) == 0
    assert capsys.readouterr().out == "already merged: octo/widget #7 (abc1234)\n"
    assert gh.merges == []
    assert collected == []


def test_merged_oid_strips_only_one_bom():
    # Anything beyond the single jq-style BOM is still an unreadable payload,
    # and unreadable can only ever mean "not merged".
    def gh(cmd):
        return RunResult(BOM + BOM + MERGED + "\n", "", 0)

    assert pr_merge.merged_oid(PR, SLUG, gh=gh) == ""


def test_unreadable_state_probe_is_not_treated_as_merged(seams):
    gh, _, _ = seams(FakeGh(views=["", MERGED]))
    assert pr_merge.main([PR, SLUG]) == 0
    assert len(gh.merges) == 1  # the empty probe fell through to the merge


# --- safety gate -----------------------------------------------------------


REFUSAL = (
    "pr-merge: REFUSING — PR #7 is not ready to merge.\n"
    "  threads_fetched: true\n"
    '  ci_all_pass:     false   failing: ["build (ubuntu)","test"]\n'
    "  review_bot:      pass/fail\n"
    "  unresolved:      2\n"
    "  mergeable:       MERGEABLE / BLOCKED\n"
    "  (if threads_fetched is false this is a transient read — retry;"
    " otherwise resolve the blocker)\n"
)


def test_refuses_when_not_ready_with_the_exact_report(seams, capsys):
    gh, _, collected = seams(state=NOT_READY)
    assert pr_merge.main([PR, SLUG]) == 3
    captured = capsys.readouterr()
    assert captured.err == REFUSAL
    assert captured.out == ""
    assert gh.merges == []
    assert collected == [(PR, SLUG)]  # snapshot asked for THIS pr in THIS repo


def test_degraded_snapshot_refuses_and_reports_nulls(seams, capsys):
    gh, _, _ = seams(state=DEGRADED)
    assert pr_merge.main([PR, SLUG]) == 3
    assert capsys.readouterr().err == (
        "pr-merge: REFUSING — PR #7 is not ready to merge.\n"
        "  threads_fetched: false\n"
        "  ci_all_pass:     true   failing: []\n"
        "  review_bot:      \n"
        "  unresolved:      null\n"
        "  mergeable:       null / null\n"
        "  (if threads_fetched is false this is a transient read — retry;"
        " otherwise resolve the blocker)\n"
    )
    assert gh.merges == []


def test_snapshot_failure_refuses_with_no_report_lines(seams, capsys):
    # The snapshot died, so there are no fields to report: only the header and
    # the hint (the shell reached the same shape by capturing an empty string).
    gh, _, _ = seams(state=RuntimeError("boom"))
    assert pr_merge.main([PR, SLUG]) == 3
    assert capsys.readouterr().err == (
        "pr-merge: REFUSING — PR #7 is not ready to merge.\n"
        "  (if threads_fetched is false this is a transient read — retry;"
        " otherwise resolve the blocker)\n"
    )
    assert gh.merges == []


def test_ready_to_merge_must_be_boolean_true(seams):
    gh, _, _ = seams(state=dict(READY, ready_to_merge=None))
    assert pr_merge.main([PR, SLUG]) == 3
    assert gh.merges == []


def test_force_bypasses_the_gate(seams, capsys):
    gh, _, collected = seams(state=NOT_READY)
    assert pr_merge.main([PR, SLUG, "--force"]) == 0
    assert capsys.readouterr().out == "merged: octo/widget #7 (abc1234)\n"
    assert collected == []  # gate skipped entirely
    assert len(gh.merges) == 1


def test_refusal_report_truncates_where_jq_would_error():
    # jq streams its outputs, so an error document prints the lines it could
    # render and then dies on `review_bot_checks|map(...)` over null.
    assert pr_merge.refusal_report({"error": "no PR"}) == [
        "  threads_fetched: null",
        "  ci_all_pass:     null   failing: null",
    ]
    assert pr_merge.refusal_report(None) == []


# --- subject ---------------------------------------------------------------


def merge_call(gh):
    assert len(gh.merges) == 1, gh.merges
    return gh.merges[0]


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_default_subject_comes_from_the_gated_snapshot(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG]) == 0
    assert flag_value(merge_call(gh), "--subject") == "feat: add the widget (#7)"
    # The title came from the snapshot, not from an extra gh read.
    assert not [c for c in gh.calls if "title" in c]


def test_default_subject_falls_back_to_gh_under_force(seams):
    gh, _, _ = seams(FakeGh(title="fix: the other thing"))
    assert pr_merge.main([PR, SLUG, "--force"]) == 0
    assert flag_value(merge_call(gh), "--subject") == "fix: the other thing (#7)"


def test_explicit_subject_and_body_win(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG, "--subject", "chore: land it", "--body", "why"]) == 0
    argv = merge_call(gh)
    assert flag_value(argv, "--subject") == "chore: land it"
    assert flag_value(argv, "--body") == "why"


# --- DCO sign-off ----------------------------------------------------------


def test_signoff_trailers_sorted_and_deduped():
    payload = json.dumps(
        [
            commit("feat: x\n\nSigned-off-by: Zoe <z@e>\nSigned-off-by: Ann <a@e>"),
            commit("fix: y\n\nSigned-off-by: Ann <a@e>"),
            commit("docs: z (no trailer)"),
        ]
    )
    assert pr_merge.signoff_trailers(payload) == (
        "Signed-off-by: Ann <a@e>\nSigned-off-by: Zoe <z@e>"
    )


def test_signoff_scan_is_line_anchored_and_stops_at_the_line_end():
    payload = json.dumps(
        [commit("Signed-off-by: Ann <a@e>\nCo-authored-by: Bo <b@e>\nnot: Signed-off-by: x")]
    )
    assert pr_merge.signoff_trailers(payload) == "Signed-off-by: Ann <a@e>"


def test_signoff_trailers_tolerates_a_leading_bom():
    # A BOM here used to silently drop every DCO trailer from the squash
    # commit (a swallowed parse error reads exactly like "no trailers").
    payload = BOM + json.dumps([commit("feat: x\n\nSigned-off-by: Ann <a@e>")])
    assert pr_merge.signoff_trailers(payload) == "Signed-off-by: Ann <a@e>"
    # Still exactly one BOM: a doubled one is malformed and yields "".
    assert pr_merge.signoff_trailers(BOM + payload) == ""


def test_signoff_trailers_tolerates_garbage():
    assert pr_merge.signoff_trailers("") == ""
    assert pr_merge.signoff_trailers("not json") == ""
    assert pr_merge.signoff_trailers('{"message":"nope"}') == ""
    assert pr_merge.signoff_trailers("[{}]") == ""


def test_signoff_is_appended_to_a_non_empty_body(seams):
    payload = json.dumps([commit("feat: x\n\nSigned-off-by: Ann <a@e>")])
    gh, _, _ = seams(FakeGh(commits=payload))
    assert pr_merge.main([PR, SLUG, "--body", "closes #3"]) == 0
    assert flag_value(merge_call(gh), "--body") == "closes #3\n\nSigned-off-by: Ann <a@e>"


def test_signoff_becomes_the_body_when_empty(seams):
    payload = json.dumps([commit("feat: x\n\nSigned-off-by: Ann <a@e>")])
    gh, _, _ = seams(FakeGh(commits=payload))
    assert pr_merge.main([PR, SLUG]) == 0
    assert flag_value(merge_call(gh), "--body") == "Signed-off-by: Ann <a@e>"


def test_existing_signoff_is_left_untouched_and_costs_no_api_call(seams):
    gh, _, _ = seams(FakeGh(commits=json.dumps([commit("x\n\nSigned-off-by: Bo <b@e>")])))
    body = "closes #3\nsigned-off-by: Ann <a@e>"  # case-insensitive, line-anchored
    assert pr_merge.main([PR, SLUG, "--body", body]) == 0
    assert flag_value(merge_call(gh), "--body") == body
    assert gh.api_calls == []


def test_no_signoff_anywhere_leaves_the_body_empty(seams):
    gh, _, _ = seams(FakeGh(commits=json.dumps([commit("feat: x")])))
    assert pr_merge.main([PR, SLUG]) == 0
    assert flag_value(merge_call(gh), "--body") == ""
    assert gh.api_calls == [["gh", "api", "repos/octo/widget/pulls/7/commits"]]


# --- merge argv ------------------------------------------------------------


def test_squash_argv_is_the_default(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG]) == 0
    assert merge_call(gh) == [
        "gh",
        "pr",
        "merge",
        "7",
        "-R",
        "octo/widget",
        "--squash",
        "--delete-branch",
        "--subject",
        "feat: add the widget (#7)",
        "--body",
        "",
    ]


def test_merge_strategy_still_carries_subject_and_body(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG, "--strategy", "merge"]) == 0
    argv = merge_call(gh)
    assert argv[6] == "--merge"
    assert "--subject" in argv and "--body" in argv


def test_rebase_strategy_drops_subject_and_body(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG, "--strategy", "rebase"]) == 0
    argv = merge_call(gh)
    assert argv[6] == "--rebase"
    assert "--subject" not in argv and "--body" not in argv


def test_no_delete_branch_drops_the_flag(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG, "--no-delete-branch"]) == 0
    assert "--delete-branch" not in merge_call(gh)


def test_merge_argv_helper_matches_the_shell_ordering():
    assert pr_merge.merge_argv(7, SLUG, "squash", False, "s", "b") == [
        "gh", "pr", "merge", "7", "-R", SLUG, "--squash", "--subject", "s", "--body", "b",
    ]


# --- verify-after ----------------------------------------------------------


def test_merge_that_fatals_locally_but_landed_is_success(seams, capsys):
    # FakeGh's merge always returns a non-zero fast-forward fatal; the state
    # probe afterwards is what decides.
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG]) == 0
    assert capsys.readouterr().out == "merged: octo/widget #7 (abc1234)\n"


def test_merge_that_did_not_land_exits_1(seams, capsys):
    gh, _, _ = seams(FakeGh(views=[OPEN, OPEN]))
    assert pr_merge.main([PR, SLUG]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "pr-merge: merge did not land for octo/widget #7"
        " — re-run pr_state.py and inspect.\n"
    )
    assert len(gh.merges) == 1  # exactly one attempt, never a retry loop


# --- flag parsing ----------------------------------------------------------


@pytest.mark.parametrize("flag", ["--subject", "--body", "--strategy"])
def test_missing_flag_value_is_a_usage_error(seams, capsys, flag):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG, flag]) == 2
    assert capsys.readouterr().err == "missing value for {}\n".format(flag)
    assert gh.calls == []


@pytest.mark.parametrize("flag", ["--nope", "-x", "--delete-branch", "-", "--force=1"])
def test_unknown_flag_is_a_usage_error(seams, capsys, flag):
    gh, _, _ = seams()
    assert pr_merge.main([PR, flag]) == 2
    assert capsys.readouterr().err == "unknown flag: {}\n".format(flag)
    assert gh.calls == []


def test_a_dashed_word_with_a_slash_is_still_an_unknown_flag(seams, capsys):
    # The shell's `-*)` arm precedes `*/*)`.
    gh, _, _ = seams()
    assert pr_merge.main(["-R", "octo/widget"]) == 2
    assert capsys.readouterr().err == "unknown flag: -R\n"


def test_a_flag_value_that_looks_like_a_flag_is_consumed_as_a_value(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, SLUG, "--subject", "--force"]) == 0
    assert flag_value(merge_call(gh), "--subject") == "--force"


# --- positional resolution -------------------------------------------------


def test_repo_before_pr_resolves_the_same(seams):
    gh, _, collected = seams()
    assert pr_merge.main([SLUG, PR]) == 0
    assert collected == [(PR, SLUG)]
    assert merge_call(gh)[3:6] == ["7", "-R", "octo/widget"]


def test_first_bare_word_is_the_pr_and_a_later_one_is_the_repo(seams):
    gh, _, _ = seams()
    assert pr_merge.main([PR, "scratch", SLUG]) == 0
    assert merge_call(gh)[3:6] == ["7", "-R", "octo/widget"]


def test_gh_repo_env_supplies_the_repo(seams, monkeypatch):
    monkeypatch.setenv("GH_REPO", SLUG)
    gh, plain, collected = seams()
    assert pr_merge.main([PR]) == 0
    assert collected == [(PR, SLUG)]
    assert plain.calls == []  # nothing left to resolve


def test_pr_is_resolved_from_the_current_branch(seams):
    gh, plain, collected = seams(plain=FakePlain(number="42"))
    assert pr_merge.main([SLUG]) == 0
    assert plain.calls == [
        ["gh", "pr", "view", "-R", SLUG, "--json", "number", "-q", ".number"]
    ]
    assert collected == [("42", SLUG)]


def test_owner_and_name_are_resolved_when_no_repo_is_given(seams):
    gh, plain, collected = seams(plain=FakePlain(ownername="octo Hello-World"))
    assert pr_merge.main([PR]) == 0
    assert collected == [(PR, "octo/Hello-World")]
    assert merge_call(gh)[4:6] == ["-R", "octo/Hello-World"]


def test_no_pr_anywhere_exits_1(seams, capsys):
    gh, _, _ = seams(plain=FakePlain())
    assert pr_merge.main([]) == 1
    assert capsys.readouterr().err == (
        "pr-merge: no PR (pass a number, and OWNER/REPO unless run inside the repo)\n"
    )
    assert gh.calls == []
