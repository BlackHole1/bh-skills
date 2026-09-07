"""Tests for ship-pr/scripts/pr_state.py.

The snapshot this script prints is a machine-read contract: ship-pr's loop keys
off `ready_to_merge`, and humans read the rest, so the tests pin the exact key
ORDER, the exact gh argv (including the GraphQL document), and every derived
field. The honesty gate gets the most attention: a threads read that failed must
surface as `threads_fetched: false` with null counts, NEVER as a fabricated
"0 unresolved" that would green-light a merge over hidden review threads.

Both gh boundaries are injected (`collect_state(gh=, plain=)`) or monkeypatched
on the module for the CLI paths, so nothing here needs the gh binary, the
network, or auth. The two subprocess tests deliberately run with a PATH that has
no gh on it — "gh is absent" is one of the behaviors under test.
"""

from __future__ import annotations

import json

import pytest

import pr_state
from gh_retry import RunResult

# The consumer-visible key order of the snapshot, in the order the shell's jq
# object literal built it. Changing this breaks anything parsing the JSON.
SNAPSHOT_KEYS = [
    "pr",
    "state",
    "title",
    "mergeable",
    "mergeStateStatus",
    "head",
    "reviewDecision",
    "checks",
    "ci_check_count",
    "ci_all_pass",
    "ci_failing",
    "ci_pending",
    "review_bot_checks",
    "threads_fetched",
    "review_threads_total",
    "review_threads_unresolved",
    "review_comment_count",
    "ready_to_merge",
]

HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f0deadbeef"

HEALTHY_PRVIEW = {
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
    "reviewDecision": "APPROVED",
    "state": "OPEN",
    "title": "feat: ship it 🚀",
    "headRefName": "feat/ship",
    "headRefOid": HEAD_SHA,
}

UNSET = object()  # "use the healthy default"
EMPTY = object()  # "the read came back empty (gh_retry gave up)"


def check(name, bucket, state="COMPLETED"):
    """One `gh pr checks --json name,bucket,state` row."""
    return {"name": name, "bucket": bucket, "state": state}


def thread(resolved=True, outdated=False, comments=1):
    """One reviewThreads node; comments=None omits the comments object."""
    node = {"isResolved": resolved, "isOutdated": outdated}
    if comments is not None:
        node["comments"] = {"totalCount": comments}
    return node


def threads_payload(nodes):
    return {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}
    }


def _result(value):
    """Canned gh output: EMPTY for a give-up, a RunResult verbatim, a raw string
    for malformed/partial output, else the object as JSON (gh emits a trailing
    newline, which `_capture` must strip)."""
    if value is EMPTY:
        return RunResult("", "Post https://api.github.com/graphql: EOF", 1)
    if isinstance(value, RunResult):
        return value
    if isinstance(value, str):
        return RunResult(value, "", 0)
    return RunResult(json.dumps(value) + "\n", "", 0)


class FakeGh:
    """Serves the three retried reads collect_state makes, recording the argv."""

    def __init__(self, checks=UNSET, prview=UNSET, threads=UNSET):
        self.checks = [] if checks is UNSET else checks
        self.prview = HEALTHY_PRVIEW if prview is UNSET else prview
        self.threads = threads_payload([]) if threads is UNSET else threads
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        sub = list(cmd[1:3])
        if sub == ["pr", "checks"]:
            return _result(self.checks)
        if sub == ["pr", "view"]:
            return _result(self.prview)
        if sub == ["api", "graphql"]:
            return _result(self.threads)
        raise AssertionError("unexpected gh call: {}".format(list(cmd)))


class FakePlain:
    """The un-retried seam: `gh pr view --json number` (PR resolution) and
    `gh repo view` (owner/name resolution)."""

    def __init__(self, number="", ownername=""):
        self.number = number
        self.ownername = ownername
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        out = self.number if list(cmd[1:3]) == ["pr", "view"] else self.ownername
        return RunResult(out, "", 0 if out else 1)


def snapshot(pr="7", repo="o/r", **kw):
    """collect_state over a FakeGh; an explicit repo means no resolution call."""
    return pr_state.collect_state(pr, repo, gh=FakeGh(**kw), plain=FakePlain())


@pytest.fixture(autouse=True)
def _no_inherited_gh_repo(monkeypatch):
    """The in-process tests must not inherit a developer's GH_REPO (the
    subprocess ones already run under conftest's cleaned environment)."""
    monkeypatch.delenv("GH_REPO", raising=False)


# ---- the snapshot contract ---------------------------------------------------


def test_full_snapshot_key_order_and_values():
    gh = FakeGh(
        checks=[
            check("build (ubuntu-latest)", "pass"),
            check("CodeRabbit", "pass"),
            check("lint", "skipping"),
        ],
        prview=HEALTHY_PRVIEW,
        threads=threads_payload([thread(comments=3), thread(comments=1)]),
    )
    state = pr_state.collect_state("7", "o/r", gh=gh, plain=FakePlain())

    # Key order is part of the contract (consumers parse this JSON).
    assert list(state) == SNAPSHOT_KEYS
    assert state == {
        "pr": 7,  # jq's `$num | tonumber`: a number, not the "7" string
        "state": "OPEN",
        "title": "feat: ship it 🚀",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "head": HEAD_SHA,
        "reviewDecision": "APPROVED",
        # map({name, bucket}) — the per-check `state` field is dropped.
        "checks": [
            {"name": "build (ubuntu-latest)", "bucket": "pass"},
            {"name": "CodeRabbit", "bucket": "pass"},
            {"name": "lint", "bucket": "skipping"},
        ],
        "ci_check_count": 2,
        "ci_all_pass": True,
        "ci_failing": [],
        "ci_pending": [],
        "review_bot_checks": [{"name": "CodeRabbit", "bucket": "pass"}],
        "threads_fetched": True,
        "review_threads_total": 2,
        "review_threads_unresolved": 0,
        "review_comment_count": 4,
        "ready_to_merge": True,
    }
    # headRefName is fetched (the shell asked for it) but deliberately unexposed.
    assert "headRefName" not in state


def test_gh_calls_match_the_shell_invocations():
    gh = FakeGh()
    plain = FakePlain(ownername="never/called")
    pr_state.collect_state("7", "acme/widgets", gh=gh, plain=plain)

    assert plain.calls == []  # explicit OWNER/REPO: no repo-resolution call
    assert gh.calls == [
        ["gh", "pr", "checks", "7", "-R", "acme/widgets", "--json", "name,bucket,state"],
        [
            "gh",
            "pr",
            "view",
            "7",
            "-R",
            "acme/widgets",
            "--json",
            "mergeable,mergeStateStatus,reviewDecision,state,title,headRefName,headRefOid",
        ],
        [
            "gh",
            "api",
            "graphql",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
            "-f",
            "query=" + pr_state.THREADS_QUERY,
        ],
    ]


def test_threads_query_is_the_document_the_shell_sent():
    assert pr_state.THREADS_QUERY == (
        "\n"
        "query($owner:String!,$repo:String!,$pr:Int!){\n"
        "  repository(owner:$owner,name:$repo){\n"
        "    pullRequest(number:$pr){\n"
        "      reviewThreads(first:100){nodes{isResolved isOutdated comments(first:1){totalCount}}}\n"
        "    }\n"
        "  }\n"
        "}"
    )


def test_owner_and_name_come_from_gh_when_no_repo_is_given():
    gh = FakeGh()
    plain = FakePlain(ownername="octocat Hello-World\n")
    pr_state.collect_state("7", "", gh=gh, plain=plain)

    assert len(plain.calls) == 1  # exactly one resolution call, not one per read
    assert gh.calls[0] == ["gh", "pr", "checks", "7", "--json", "name,bucket,state"]
    assert gh.calls[2][:9] == [
        "gh",
        "api",
        "graphql",
        "-F",
        "owner=octocat",
        "-F",
        "repo=Hello-World",
        "-F",
        "pr=7",
    ]


def test_capture_strips_trailing_newlines_and_keeps_output_on_failure():
    # The shell's `x="$(cmd)" || true`: output survives a non-zero exit.
    assert pr_state._capture(RunResult("42\n\n", "", 0)) == "42"
    assert pr_state._capture(RunResult("[]\n", "1 failing check", 8)) == "[]"
    assert pr_state._capture(RunResult("", "", 1)) == ""


def test_failing_checks_json_is_used_despite_the_nonzero_exit():
    # `gh pr checks` exits non-zero while printing valid JSON.
    payload = json.dumps([check("build", "fail")]) + "\n"
    state = snapshot(checks=RunResult(payload, "1 failing check", 8))
    assert state["ci_failing"] == ["build"]


# ---- reviewer bot versus CI classification -----------------------------------


BOT_NAMES = [
    "coderabbit",
    "CodeRabbit",
    "CODERABBIT",
    "coderabbitai / review",
    "Sourcery review",
    "sourcery-ai/pr-review",
    "PR-Agent by CodiumAI",
    "codium",
    "Qodo Merge",
    "qodo-merge-pro",
    "greptile",
    "Greptile Summary",
    "ellipsis",
    "Ellipsis / review (pull_request)",
]

CI_NAMES = [
    "build",
    "test (ubuntu-latest, 3.12)",
    "lint",
    "CodeQL",
    "codecov/patch",
    "Cirrus CI",
    "docs / deploy",
    "quality-gate",
]


@pytest.mark.parametrize("name", BOT_NAMES)
def test_reviewer_bot_names_match_case_insensitively_as_substrings(name):
    state = snapshot(checks=[check(name, "pending")])
    assert state["review_bot_checks"] == [{"name": name, "bucket": "pending"}]
    assert state["checks"] == [{"name": name, "bucket": "pending"}]
    assert state["ci_check_count"] == 0
    assert state["ci_pending"] == []  # a pending BOT is not pending CI
    assert state["ready_to_merge"] is False


@pytest.mark.parametrize("name", CI_NAMES)
def test_plain_ci_checks_are_not_misclassified_as_bots(name):
    state = snapshot(checks=[check(name, "fail")])
    assert state["review_bot_checks"] == []
    assert state["ci_check_count"] == 1
    assert state["ci_failing"] == [name]


def test_ascii_downcase_folds_only_a_to_z():
    assert pr_state._ascii_downcase("CodeRabbit / Review") == "coderabbit / review"
    assert pr_state._ascii_downcase("QODO-Merge_PRO") == "qodo-merge_pro"
    # jq's ascii_downcase leaves non-ASCII alone; a str.lower() port would
    # over-fold (KELVIN SIGN -> "k", "İ" -> "i" + combining dot).
    for ch in ("K", "İ", "É", "Ä", "Σ"):
        assert pr_state._ascii_downcase(ch) == ch
        assert ch.lower() != ch  # ...exactly what str.lower() would have changed


def test_bot_regex_is_case_sensitive_so_matching_relies_on_the_downcase():
    assert pr_state.BOT_RE.search("CodeRabbit") is None
    assert pr_state.BOT_RE.search(pr_state._ascii_downcase("CodeRabbit"))


def test_mixed_case_bot_and_ci_checks_split_cleanly():
    state = snapshot(
        checks=[
            check("Build", "pass"),
            check("CodeRabbit", "fail"),
            check("Test", "pass"),
            check("QODO Merge", "pass"),
        ]
    )
    assert state["ci_check_count"] == 2
    assert state["ci_all_pass"] is True
    assert state["ci_failing"] == []  # the failure is the bot's, not CI's
    assert [c["name"] for c in state["review_bot_checks"]] == ["CodeRabbit", "QODO Merge"]
    assert state["ready_to_merge"] is False  # ...but the bot failure still blocks


# ---- bucket handling ---------------------------------------------------------


def test_pass_and_skipping_both_count_as_passing():
    state = snapshot(
        checks=[
            check("build", "pass"),
            check("optional-e2e", "skipping"),
            check("coderabbit", "skipping"),
        ]
    )
    assert state["ci_check_count"] == 2
    assert state["ci_all_pass"] is True
    assert state["ready_to_merge"] is True


def test_fail_and_pending_populate_their_lists():
    state = snapshot(
        checks=[
            check("build", "fail"),
            check("test", "pending"),
            check("lint", "pass"),
            check("coderabbit", "fail"),
        ]
    )
    assert state["ci_failing"] == ["build"]
    assert state["ci_pending"] == ["test"]
    assert state["ci_all_pass"] is False
    assert state["review_bot_checks"] == [{"name": "coderabbit", "bucket": "fail"}]
    assert state["ready_to_merge"] is False


def test_cancelled_check_blocks_pass_without_landing_in_either_list():
    state = snapshot(checks=[check("build", "cancel")])
    assert state["ci_all_pass"] is False
    assert state["ci_failing"] == []
    assert state["ci_pending"] == []
    assert state["ready_to_merge"] is False


# ---- vacuous truth -----------------------------------------------------------


def test_zero_ci_checks_is_vacuously_passing_but_still_gated_on_merge_state():
    # A pending PR reports UNSTABLE/BLOCKED; the vacuous all() must not green-light it.
    state = snapshot(
        checks=[], prview={**HEALTHY_PRVIEW, "mergeStateStatus": "UNSTABLE"}
    )
    assert state["ci_check_count"] == 0
    assert state["ci_all_pass"] is True
    assert state["review_bot_checks"] == []
    assert state["ready_to_merge"] is False


def test_zero_checks_on_a_clean_pr_is_legitimately_ready():
    # A repo can genuinely have no CI at all.
    state = snapshot(checks=[])
    assert state["ci_all_pass"] is True
    assert state["ready_to_merge"] is True


def test_zero_bot_checks_is_vacuously_passing():
    state = snapshot(checks=[check("build", "pass")])
    assert state["review_bot_checks"] == []
    assert state["ready_to_merge"] is True  # only possible if botpass is vacuous


# ---- ready_to_merge is the AND of six conditions -----------------------------


READY = dict(
    checks=[check("build", "pass"), check("coderabbit", "pass")],
    prview=HEALTHY_PRVIEW,
    threads=threads_payload([thread(resolved=True, comments=1)]),
)


def test_the_base_scenario_is_ready_to_merge():
    assert snapshot(**READY)["ready_to_merge"] is True


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"threads": EMPTY}, id="threads-not-fetched"),
        pytest.param(
            {"checks": [check("build", "fail"), check("coderabbit", "pass")]},
            id="ci-not-passing",
        ),
        pytest.param(
            {"checks": [check("build", "pass"), check("coderabbit", "fail")]},
            id="bot-not-passing",
        ),
        pytest.param(
            {"threads": threads_payload([thread(resolved=False)])},
            id="unresolved-thread",
        ),
        pytest.param(
            {"prview": {**HEALTHY_PRVIEW, "mergeable": "CONFLICTING"}},
            id="not-mergeable",
        ),
        pytest.param(
            {"prview": {**HEALTHY_PRVIEW, "mergeStateStatus": "BLOCKED"}},
            id="merge-state-not-clean",
        ),
    ],
)
def test_each_condition_alone_blocks_ready_to_merge(override):
    kw = dict(READY)
    kw.update(override)
    assert snapshot(**kw)["ready_to_merge"] is False


# ---- the honesty gate: a failed threads read is never "0 unresolved" ---------


DEGRADED_THREADS = [
    pytest.param(EMPTY, id="fetch-gave-up-empty"),
    pytest.param("", id="empty-string"),
    pytest.param({}, id="no-data-key"),
    pytest.param({"data": None}, id="data-null"),
    pytest.param(
        {"data": None, "errors": [{"message": "Something went wrong"}]},
        id="graphql-errors",
    ),
    pytest.param({"data": {"repository": None}}, id="repository-null"),
    pytest.param({"data": {"repository": {"pullRequest": {}}}}, id="no-reviewThreads"),
    pytest.param(threads_payload(None), id="nodes-null"),
]


@pytest.mark.parametrize("payload", DEGRADED_THREADS)
def test_degraded_threads_read_never_fabricates_zero_unresolved(payload):
    # Everything else about this PR is green, so only the honesty gate can stop
    # ready_to_merge — the exact situation that would hide real review threads.
    state = snapshot(threads=payload, checks=[check("build", "pass")])
    assert state["ci_all_pass"] is True
    assert state["mergeStateStatus"] == "CLEAN"
    assert state["threads_fetched"] is False
    assert state["review_threads_total"] is None
    assert state["review_threads_unresolved"] is None
    assert state["review_comment_count"] is None
    assert state["ready_to_merge"] is False


def test_genuinely_empty_pr_reports_real_zeros_and_can_be_ready():
    # The contrast case: a non-null (empty) nodes array is a real answer.
    state = snapshot(threads=threads_payload([]), checks=[check("build", "pass")])
    assert state["threads_fetched"] is True
    assert state["review_threads_total"] == 0
    assert state["review_threads_unresolved"] == 0
    assert state["review_comment_count"] == 0
    assert state["ready_to_merge"] is True


def test_malformed_threads_payload_aborts_instead_of_guessing(monkeypatch, capsys):
    # Non-JSON output killed jq (--argjson) in the shell; the port raises, and
    # main turns that into exit 2 with NOTHING on stdout — never a snapshot.
    gh = FakeGh(threads="<html>502 Bad Gateway</html>")
    with pytest.raises(ValueError):
        pr_state.collect_state("7", "o/r", gh=gh, plain=FakePlain())

    monkeypatch.setattr(pr_state, "_gh", FakeGh(threads="<html>502</html>"))
    monkeypatch.setattr(pr_state, "_gh_plain", FakePlain())
    assert pr_state.main(["7", "o/r"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("pr-state: ")


def test_every_read_failing_degrades_to_empty_containers_not_errors():
    state = snapshot(checks=EMPTY, prview=EMPTY, threads=EMPTY)
    assert state["checks"] == []
    assert state["ci_check_count"] == 0
    assert state["review_bot_checks"] == []
    assert [state["state"], state["title"], state["head"]] == [None, None, None]
    assert state["mergeable"] is None
    assert state["mergeStateStatus"] is None
    assert state["reviewDecision"] is None
    assert state["threads_fetched"] is False
    assert state["ready_to_merge"] is False


def test_json_null_reads_are_treated_as_empty_like_jqs_alternative_operator():
    state = snapshot(checks="null", prview="null")
    assert state["checks"] == []
    assert state["ci_all_pass"] is True
    assert state["state"] is None


# ---- input the parser must tolerate (jq did) ---------------------------------


def test_a_leading_bom_on_every_gh_read_parses_like_jq():
    # jq skips a leading BOM; json.loads raises on it. A BOM anywhere in these
    # three reads used to blow collect_state up (exit 2 from the CLI, a held
    # read from pr_watch) — a re-encoded response must not cost a snapshot.
    bom = "\ufeff"
    state = snapshot(
        checks=bom + json.dumps([check("build", "pass")]),
        prview=bom + json.dumps(HEALTHY_PRVIEW),
        threads=bom + json.dumps(threads_payload([thread(resolved=False)])),
    )
    assert state["checks"] == [{"name": "build", "bucket": "pass"}]
    assert state["ci_all_pass"] is True
    assert state["mergeStateStatus"] == "CLEAN"
    assert state["head"] == HEAD_SHA
    assert state["threads_fetched"] is True
    assert state["review_threads_unresolved"] == 1
    assert state["ready_to_merge"] is False  # the unresolved thread, not a parse


# ---- thread counting ---------------------------------------------------------


def test_unresolved_counts_only_an_explicit_false():
    nodes = [thread(resolved=False), thread(resolved=True), {"isOutdated": True}]
    state = snapshot(threads=threads_payload(nodes))
    assert state["review_threads_total"] == 3
    assert state["review_threads_unresolved"] == 1  # jq's `.isResolved == false`
    assert state["ready_to_merge"] is False


def test_outdated_unresolved_threads_still_block():
    # isOutdated is fetched but never filtered on: an outdated thread still counts.
    state = snapshot(threads=threads_payload([thread(resolved=False, outdated=True)]))
    assert state["review_threads_unresolved"] == 1
    assert state["ready_to_merge"] is False


def test_review_comment_count_sums_and_tolerates_missing_comment_objects():
    nodes = [
        thread(comments=3),
        thread(comments=1),
        thread(comments=None),  # no comments key at all
        {"isResolved": True, "comments": None},  # explicit null
    ]
    state = snapshot(threads=threads_payload(nodes))
    assert state["review_threads_total"] == 4
    assert state["review_comment_count"] == 4  # jq's `add` skips the nulls


def test_review_comment_count_is_zero_not_null_when_nothing_has_comments():
    state = snapshot(threads=threads_payload([{"isResolved": True}]))
    assert state["review_comment_count"] == 0  # jq's `add // 0`


# ---- repo / PR resolution ----------------------------------------------------


def test_resolve_owner_name_splits_an_explicit_slug_without_calling_gh():
    plain = FakePlain(ownername="should/not-be-used")
    assert pr_state.resolve_owner_name("acme/widgets", run=plain) == ("acme", "widgets")
    # ${repo%%/*} / ${repo##*/}: first segment, last path segment.
    assert pr_state.resolve_owner_name("acme/team/widgets", run=plain) == (
        "acme",
        "widgets",
    )
    assert pr_state.resolve_owner_name("solo", run=plain) == ("solo", "solo")
    assert plain.calls == []


def test_resolve_owner_name_falls_back_to_one_gh_repo_view_call():
    plain = FakePlain(ownername="octocat Hello-World\n")
    assert pr_state.resolve_owner_name("", run=plain) == ("octocat", "Hello-World")
    assert plain.calls == [
        [
            "gh",
            "repo",
            "view",
            "--json",
            "owner,name",
            "-q",
            '.owner.login + " " + .name',
        ]
    ]


def test_resolve_owner_name_splits_on_the_first_and_last_space():
    # ${ownername%% *} / ${ownername##* } — a spaced repo name keeps only the tail.
    plain = FakePlain(ownername="octo cat My Repo")
    assert pr_state.resolve_owner_name("", run=plain) == ("octo", "Repo")


def test_resolve_owner_name_degrades_to_empty_strings_when_gh_fails():
    assert pr_state.resolve_owner_name("", run=FakePlain(ownername="")) == ("", "")


def test_resolve_pr_uses_the_repo_flag_when_given():
    plain = FakePlain(number="42\n")
    assert pr_state.resolve_pr("acme/widgets", run=plain) == "42"
    assert plain.calls == [
        ["gh", "pr", "view", "-R", "acme/widgets", "--json", "number", "-q", ".number"]
    ]


def test_resolve_pr_without_a_repo_and_with_no_pr_returns_empty():
    plain = FakePlain(number="")
    assert pr_state.resolve_pr("", run=plain) == ""
    assert plain.calls == [
        ["gh", "pr", "view", "--json", "number", "-q", ".number"]
    ]


# ---- the CLI -----------------------------------------------------------------


NO_PR_STDOUT = (
    '{"error":"no PR (pass a PR number, and OWNER/REPO unless run inside the repo)"}\n'
)


@pytest.fixture
def seams(monkeypatch):
    """Install both module-level gh boundaries; returns (gh, plain)."""

    def install(gh=None, plain=None):
        gh = gh if gh is not None else FakeGh()
        plain = plain if plain is not None else FakePlain()
        monkeypatch.setattr(pr_state, "_gh", gh)
        monkeypatch.setattr(pr_state, "_gh_plain", plain)
        return gh, plain

    return install


def test_main_prints_the_no_pr_line_verbatim_and_exits_zero(seams, capsys):
    gh, plain = seams(plain=FakePlain(number=""))
    assert pr_state.main([]) == 0
    assert capsys.readouterr().out == NO_PR_STDOUT
    assert gh.calls == []  # bailed before any retried read


def test_main_resolves_the_current_branch_pr_when_no_number_is_given(seams, capsys):
    gh, plain = seams(plain=FakePlain(number="42\n", ownername="octocat Hello-World"))
    assert pr_state.main([]) == 0
    assert json.loads(capsys.readouterr().out)["pr"] == 42
    assert gh.calls[0][3] == "42"


def test_main_renders_indent_two_json_with_literal_emoji(seams, capsys):
    seams(gh=FakeGh(checks=[check("build", "pass")]))
    assert pr_state.main(["7", "o/r"]) == 0
    out = capsys.readouterr().out

    assert out.endswith("}\n")
    assert '\n  "pr": 7,' in out  # indent=2
    assert '"title": "feat: ship it 🚀"' in out  # ensure_ascii=False
    assert "\\u" not in out
    rendered = json.loads(out)
    assert list(rendered) == SNAPSHOT_KEYS  # key order survives the render
    assert rendered["ready_to_merge"] is True


def test_main_reads_the_slug_from_gh_repo_env(seams, monkeypatch, capsys):
    monkeypatch.setenv("GH_REPO", "env/repo")
    gh, plain = seams()
    assert pr_state.main(["7"]) == 0
    assert plain.calls == []  # the slug came from the env, so no resolution call
    assert gh.calls[0][4:6] == ["-R", "env/repo"]
    capsys.readouterr()


def test_positional_repo_wins_over_gh_repo(seams, monkeypatch, capsys):
    monkeypatch.setenv("GH_REPO", "env/repo")
    gh, _ = seams()
    assert pr_state.main(["7", "arg/repo"]) == 0
    assert gh.calls[0][4:6] == ["-R", "arg/repo"]
    capsys.readouterr()


def test_main_errors_without_printing_a_snapshot_on_a_non_numeric_pr(seams, capsys):
    # jq's `tonumber` died here too (with its own code 5); what matters is the
    # shared behavior: non-zero exit, a diagnostic on stderr, no snapshot. The
    # exit code itself is a recorded deviation: the port has one non-zero code
    # for "couldn't build a snapshot", and no consumer branches on jq's 5.
    seams()
    assert pr_state.main(["not-a-number", "o/r"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("pr-state: ")


def test_fractional_pr_number_is_refused_instead_of_emitted(seams, capsys):
    # Recorded deviation, deliberately NOT restored: jq's `tonumber` accepted
    # "42.5" and printed a snapshot with a float PR. int() refuses, so the run
    # fails loudly instead of publishing a PR number nothing can act on.
    # Unreachable in practice — the number is gh-resolved or typed by a human.
    seams()
    assert pr_state.main(["42.5", "o/r"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("pr-state: ")


# ---- subprocess integration (gh deliberately absent from PATH) ---------------


@pytest.fixture
def no_gh(tmp_path):
    """A PATH with no gh on it — 'gh is missing' must degrade, not traceback."""
    empty = tmp_path / "no-gh-here"
    empty.mkdir()
    return {"PATH": str(empty)}


def test_cli_prints_the_no_pr_line_when_gh_is_unavailable(run_script, no_gh, tmp_path):
    r = run_script("ship-pr", "pr_state.py", cwd=tmp_path, env=no_gh)
    assert r.returncode == 0
    assert r.stdout == NO_PR_STDOUT


def test_cli_degrades_honestly_when_every_gh_read_fails(run_script, no_gh, tmp_path):
    r = run_script("ship-pr", "pr_state.py", "7", "acme/widgets", cwd=tmp_path, env=no_gh)
    assert r.returncode == 0
    state = json.loads(r.stdout)
    assert list(state) == SNAPSHOT_KEYS
    assert state["pr"] == 7
    assert state["checks"] == []
    assert state["threads_fetched"] is False
    assert state["review_threads_unresolved"] is None
    assert state["ready_to_merge"] is False
