"""Tests for ship-pr/scripts/pr_watch.py.

The watcher is a debounce state machine wrapped in a poll loop, so everything
here drives the pure pieces (normalize / keyof / Debouncer) or drives main()
with the poll boundary and the sleep replaced. No test ever sleeps, calls gh,
or touches the network.

The scenarios mirror the production incidents the shell script documents:
mergeStateStatus flashing null/UNKNOWN after a push, CodeRabbit jittering the
comment count while it edits its review, a one-poll ready=true flash that could
trip a premature merge, a degraded threads read, and the assess-then-arm TOCTOU
window that PR_WATCH_SEED_FILE closes.
"""

from __future__ import annotations

import json
import sys

import pytest

import pr_state
import pr_watch
from pr_watch import Debouncer, keyof, merge_value, normalize, normalize_text

REAL_SNAPSHOT = pr_watch.snapshot  # kept before the autouse guard replaces it


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def state(**over):
    """A clean pr_state snapshot dict (only the fields normalize reads)."""
    doc = {
        "mergeStateStatus": "BLOCKED",
        "head": "abc123",
        "ci_all_pass": False,
        "ci_failing": [],
        "review_bot_checks": [{"name": "CodeRabbit", "bucket": "pass"}],
        "threads_fetched": True,
        "review_threads_unresolved": 2,
        "review_comment_count": 7,
        "ready_to_merge": False,
    }
    doc.update(over)
    return doc


def sline(
    ci_pass="true",
    ci_fail=0,
    bot="pass",
    unresolved=0,
    comments=3,
    merge="CLEAN",
    head="abc123",
    ready="false",
):
    """A state line in the exact shape normalize() emits."""
    return (
        "fetched ci_pass={} ci_fail={} bot={} unresolved={} comments={} "
        "merge={} head={} ready={}".format(
            ci_pass, ci_fail, bot, unresolved, comments, merge, head, ready
        )
    )


class Poller:
    """Scripted poll boundary; the last line repeats forever so an endless
    loop under test never runs off the end of the queue."""

    def __init__(self, *lines):
        self.lines = list(lines) or [""]
        self.calls = []

    def __call__(self, pr, repo):
        self.calls.append((pr, repo))
        return self.lines.pop(0) if len(self.lines) > 1 else self.lines[0]


class LoopDone(Exception):
    """Breaks out of the watcher's endless loop from the sleep seam."""


class Clock:
    """Records the poll interval; aborts the loop after `limit` sleeps."""

    def __init__(self, limit=4):
        self.slept = []
        self.limit = limit

    def __call__(self, seconds):
        self.slept.append(seconds)
        if len(self.slept) >= self.limit:
            raise LoopDone()


def drive(machine, *raws):
    """Feed raw lines; return the list of emitted lines."""
    return [line for line in (machine.step(raw) for raw in raws) if line is not None]


@pytest.fixture(autouse=True)
def no_live_gh(monkeypatch):
    """Both boundaries that would shell out to gh fail loudly by default; a
    test that needs them installs its own scripted fake on top."""

    def forbidden(*args, **kwargs):
        raise AssertionError("a test tried to reach gh")

    monkeypatch.setattr(pr_watch, "snapshot", forbidden)
    monkeypatch.setattr(pr_watch, "_resolve_pr", forbidden)


# --------------------------------------------------------------------------
# normalize
# --------------------------------------------------------------------------


def test_normalize_renders_the_exact_shell_line():
    assert normalize(state()) == (
        "fetched ci_pass=false ci_fail=0 bot=pass unresolved=2 comments=7 "
        "merge=BLOCKED head=abc123 ready=false"
    )


def test_helper_line_matches_normalize_output():
    # Guards the machine tests below: they must be fed real-format lines.
    doc = state(
        ci_all_pass=True,
        review_threads_unresolved=0,
        review_comment_count=3,
        mergeStateStatus="CLEAN",
    )
    assert normalize(doc) == sline()


def test_normalize_error_payload_wakes_the_agent():
    assert normalize({"error": "no such PR"}) == "ERROR no such PR"


def test_normalize_error_is_jq_truthy_even_when_empty():
    # jq treats only null and false as falsy, so `{"error": ""}` still errors.
    assert normalize({"error": ""}) == "ERROR "
    assert normalize({"error": None, **state()}).startswith("fetched ")
    assert normalize({"error": False, **state()}).startswith("fetched ")


def test_normalize_degraded_read_is_empty_so_the_caller_holds():
    assert normalize(state(threads_fetched=False)) == ""


def test_normalize_missing_threads_flag_is_not_degraded():
    # The shell compared `.threads_fetched == false` strictly; a missing flag
    # renders a line (with null counts), it does not hold.
    doc = state(review_threads_unresolved=None, review_comment_count=None)
    doc.pop("threads_fetched")
    assert "unresolved=null comments=null" in normalize(doc)


def test_normalize_empty_bot_bucket_is_pending():
    # A bot vanishing mid-review must not read as a state change.
    assert " bot=pending " in normalize(state(review_bot_checks=[]))


def test_normalize_joins_bot_buckets_with_slash():
    doc = state(
        review_bot_checks=[
            {"name": "coderabbit", "bucket": "pass"},
            {"name": "sourcery", "bucket": "pending"},
        ]
    )
    assert " bot=pass/pending " in normalize(doc)


def test_normalize_null_bucket_renders_as_empty_like_jq_join():
    doc = state(review_bot_checks=[{"name": "coderabbit", "bucket": None}])
    # join("/") over [null] is "", which the empty-case rule turns into pending.
    assert " bot=pending " in normalize(doc)


def test_normalize_null_bot_check_element_is_pending_not_a_hold():
    # jq's map(.bucket) indexes a null ELEMENT happily (null) and join renders
    # it as "", which the empty-case rule turns into pending. Raising here would
    # cost the whole line — silence — where the shell emitted a pending state.
    assert " bot=pending " in normalize(state(review_bot_checks=[None]))
    doc = state(review_bot_checks=[None, {"name": "sourcery", "bucket": "pass"}])
    assert " bot=/pass " in normalize(doc)
    # …and the caller that used to swallow the raise now gets a real line.
    assert normalize_text(json.dumps(state(review_bot_checks=[None]))).startswith(
        "fetched "
    )


def test_normalize_still_raises_on_a_non_object_bot_check():
    # Only null is survivable: jq's `.bucket` on a string or number IS an error,
    # and that stays a degraded (held) read.
    with pytest.raises(TypeError):
        normalize(state(review_bot_checks=["pass"]))
    assert normalize_text(json.dumps(state(review_bot_checks=[7]))) == ""


def test_normalize_null_merge_and_null_counts():
    doc = state(
        mergeStateStatus=None,
        head=None,
        ci_all_pass=None,
        ci_failing=None,
        review_threads_unresolved=None,
        review_comment_count=None,
        ready_to_merge=None,
    )
    assert normalize(doc) == (
        "fetched ci_pass=null ci_fail=0 bot=pass unresolved=null comments=null "
        "merge=null head=null ready=null"
    )


def test_normalize_counts_failing_checks():
    assert " ci_fail=2 " in normalize(state(ci_failing=["build", "lint"]))


def test_normalize_booleans_render_lowercase():
    assert normalize(state(ci_all_pass=True, ready_to_merge=True)).endswith(
        "ready=true"
    )


def test_normalize_text_swallows_malformed_and_degraded_documents():
    assert normalize_text(json.dumps(state())).startswith("fetched ")
    assert normalize_text("") == ""
    assert normalize_text("   ") == ""
    assert normalize_text("not json at all") == ""
    assert normalize_text("[1,2,3]") == ""  # jq would error on .error
    assert normalize_text(json.dumps(state(threads_fetched=False))) == ""
    assert normalize_text(json.dumps({"threads_fetched": True})) == ""


def test_normalize_text_skips_a_leading_bom_like_jq():
    assert normalize_text("\ufeff" + json.dumps(state())) == normalize(state())


def test_normalize_raises_when_bot_checks_are_null():
    # jq's `null | map(...)` is an error; the caller degrades to a held read.
    with pytest.raises(TypeError):
        normalize(state(review_bot_checks=None))


# --------------------------------------------------------------------------
# keyof / merge_value
# --------------------------------------------------------------------------


def test_keyof_strips_the_jittering_comment_count():
    assert keyof(sline(comments=3)) == keyof(sline(comments=41))
    assert "comments=" not in keyof(sline())


def test_keyof_keeps_everything_else_of_the_line():
    assert keyof(sline()) == (
        "fetched ci_pass=true ci_fail=0 bot=pass unresolved=0 "
        "merge=CLEAN head=abc123 ready=false"
    )


def test_keyof_leaves_error_lines_untouched():
    assert keyof("ERROR no such PR") == "ERROR no such PR"


def test_keyof_replacement_is_greedy_and_single():
    # Mirrors bash ${line/comments=*merge=/merge=}: leftmost start, longest span.
    assert keyof("a comments=1 merge=A x merge=B y") == "a merge=B y"


def test_merge_value_reads_the_first_merge_token():
    assert merge_value(sline(merge="UNKNOWN")) == "UNKNOWN"
    assert merge_value(sline(merge="null")) == "null"
    # No merge= at all: the shell's ${raw#*merge=} leaves the line alone and
    # ${v%% *} takes its first word.
    assert merge_value("ERROR no such PR") == "ERROR"


# --------------------------------------------------------------------------
# debounce state machine
# --------------------------------------------------------------------------


def test_steady_state_never_emits():
    machine = Debouncer.seeded(sline())
    assert drive(machine, *[sline()] * 6) == []


def test_new_state_must_persist_two_polls_before_emitting():
    machine = Debouncer.seeded(sline(ready="false"))
    changed = sline(ready="true", merge="CLEAN")
    assert machine.step(changed) is None  # first sighting: candidate only
    assert machine.step(changed) == changed  # held for 2 polls: wake up


def test_one_poll_ready_flash_is_swallowed():
    # A transient read showing ready=true could otherwise trip a premature merge.
    base = sline(ready="false", unresolved=1)
    flash = sline(ready="true", unresolved=0)
    machine = Debouncer.seeded(base)
    assert drive(machine, base, flash, base, base, flash, base) == []


def test_comment_count_jitter_alone_never_emits():
    # CodeRabbit edits its comments continuously; keying on the count would
    # starve real transitions instead of just being noisy.
    machine = Debouncer.seeded(sline(comments=1))
    assert drive(machine, *[sline(comments=n) for n in (2, 3, 4, 5, 6)]) == []


def test_real_transition_emits_exactly_once():
    machine = Debouncer.seeded(sline(ci_pass="false", ready="false"))
    done = sline(ci_pass="true", ready="true")
    emitted = drive(machine, done, done, done, done, done)
    assert emitted == [done]


def test_transition_emits_with_the_live_comment_count():
    machine = Debouncer.seeded(sline(comments=1))
    first = sline(comments=9, unresolved=4)
    second = sline(comments=11, unresolved=4)
    # The count jitters between the two polls, but the KEY holds, so the line
    # emitted is the latest one — count included, for context.
    assert drive(machine, first, second) == [second]


def test_null_merge_carries_forward_the_last_stable_value():
    machine = Debouncer.seeded(sline(merge="CLEAN"))
    # A push makes GitHub recompute mergeability: merge flashes null/UNKNOWN
    # while everything else is unchanged. That is not a transition.
    assert drive(machine, sline(merge="null"), sline(merge="UNKNOWN")) == []
    assert machine.last_merge == "CLEAN"


def test_carry_forward_uses_the_line_as_emitted():
    machine = Debouncer.seeded(sline(merge="BLOCKED", head="old"))
    pushed = sline(merge="null", head="new")
    emitted = drive(machine, pushed, pushed)
    # The head SHA changed, so it IS a transition; the null merge is rendered
    # as the last stable value rather than as "null".
    assert emitted == [sline(merge="BLOCKED", head="new")]


def test_unseeded_machine_starts_with_the_placeholder_merge():
    machine = Debouncer()
    pushed = sline(merge="UNKNOWN")
    assert drive(machine, pushed, pushed) == [sline(merge="?")]


def test_degraded_poll_holds_without_disturbing_an_inflight_candidate():
    machine = Debouncer.seeded(sline(ready="false"))
    changed = sline(ready="true")
    assert machine.step(changed) is None
    assert machine.step("") is None  # threads fetch failed — hold
    assert machine.step("") is None
    assert machine.step(changed) == changed  # streak survived the degraded reads
    assert machine.cand_n == 2


def test_error_wakes_after_the_same_persistence_and_only_once():
    machine = Debouncer.seeded(sline())
    err = "ERROR no such PR"
    assert drive(machine, err, err, err, err) == [err]


def test_error_then_recovery_both_emit():
    machine = Debouncer.seeded(sline(ready="false"))
    err = "ERROR Could not resolve to a PullRequest"
    good = sline(ready="false")
    assert drive(machine, err, err, good, good) == [err, good]


def test_alternating_states_never_settle_and_never_emit():
    a = sline(unresolved=1)
    b = sline(unresolved=2)
    machine = Debouncer.seeded(a)
    assert drive(machine, b, a, b, a, b) == []


# --------------------------------------------------------------------------
# the live poll boundary (pr_state wiring, still no gh)
# --------------------------------------------------------------------------


def test_snapshot_normalizes_a_collected_state(monkeypatch):
    seen = []

    def fake_collect(pr, repo):
        seen.append((pr, repo))
        return state()

    monkeypatch.setattr(pr_state, "collect_state", fake_collect)
    assert REAL_SNAPSHOT("7", "o/r") == normalize(state())
    assert seen == [("7", "o/r")]


def test_snapshot_degrades_to_a_held_read_when_collection_blows_up(monkeypatch):
    def boom(pr, repo):
        raise ValueError("malformed gh output")

    monkeypatch.setattr(pr_state, "collect_state", boom)
    assert REAL_SNAPSHOT("7", "") == ""


def test_snapshot_without_a_resolvable_pr_yields_the_error_sentinel(monkeypatch):
    monkeypatch.setattr(pr_watch, "_resolve_pr", lambda repo: "")
    assert REAL_SNAPSHOT("", "") == "ERROR " + pr_state.NO_PR_MESSAGE


def test_snapshot_resolves_an_omitted_pr(monkeypatch):
    monkeypatch.setattr(pr_watch, "_resolve_pr", lambda repo: "99")
    monkeypatch.setattr(pr_state, "collect_state", lambda pr, repo: state(head=pr))
    assert " head=99 " in REAL_SNAPSHOT("", "")


# --------------------------------------------------------------------------
# seeding (the assess -> arm TOCTOU window)
# --------------------------------------------------------------------------


def write_seed(tmp_path, doc, name="seed.json"):
    path = tmp_path / name
    path.write_text(
        doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8"
    )
    return path


def test_seed_file_is_preferred_over_a_live_snapshot(tmp_path, monkeypatch):
    path = write_seed(tmp_path, state())
    poller = Poller("fetched live")
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setenv("PR_WATCH_SEED_FILE", str(path))
    assert pr_watch.seed_baseline("7", "o/r") == normalize(state())
    assert poller.calls == []  # no self-snapshot needed


def test_bom_prefixed_seed_file_is_still_a_valid_baseline(tmp_path, monkeypatch):
    # jq skipped a leading BOM. Rejecting one would discard the agent's pre-arm
    # snapshot, self-seed from a fresh poll, and swallow the very transition the
    # seed exists to catch — silence until the ~15 min heartbeat.
    doc = state(ready_to_merge=False)
    path = write_seed(tmp_path, "\ufeff" + json.dumps(doc))
    poller = Poller("fetched live")
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setenv("PR_WATCH_SEED_FILE", str(path))
    assert pr_watch.seed_baseline("7", "o/r") == normalize(doc)
    assert poller.calls == []  # the seed was usable — no self-snapshot
    # And it really is the baseline: a change since then still emits.
    machine = Debouncer.seeded(pr_watch.seed_baseline("7", "o/r"))
    landed = normalize(state(ready_to_merge=True, ci_all_pass=True))
    assert drive(machine, landed, landed) == [landed]


def test_seed_baseline_suppresses_a_same_state_emission(tmp_path, monkeypatch):
    doc = state(review_comment_count=5)
    monkeypatch.setenv("PR_WATCH_SEED_FILE", str(write_seed(tmp_path, doc)))
    machine = Debouncer.seeded(pr_watch.seed_baseline("7", ""))
    same = normalize(doc)
    assert drive(machine, same, same, same) == []


def test_change_since_the_seed_emits_on_the_first_qualifying_poll(
    tmp_path, monkeypatch
):
    # The event landed between the agent's assess and this watcher arming: with
    # the agent's own pre-arm snapshot as the baseline it is still a change.
    monkeypatch.setenv(
        "PR_WATCH_SEED_FILE", str(write_seed(tmp_path, state(ready_to_merge=False)))
    )
    machine = Debouncer.seeded(pr_watch.seed_baseline("7", ""))
    landed = normalize(state(ready_to_merge=True, ci_all_pass=True))
    assert machine.step(landed) is None
    assert machine.step(landed) == landed


def test_seed_carries_the_last_stable_merge(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PR_WATCH_SEED_FILE",
        str(write_seed(tmp_path, state(mergeStateStatus="CLEAN"))),
    )
    machine = Debouncer.seeded(pr_watch.seed_baseline("7", ""))
    assert machine.last_merge == "CLEAN"
    # A post-push null merge is rendered back to CLEAN, matching the baseline,
    # so the watcher stays quiet.
    flashed = normalize(state(mergeStateStatus=None))
    assert drive(machine, flashed, flashed) == []


def test_seed_with_null_merge_leaves_the_placeholder():
    machine = Debouncer.seeded(sline(merge="null"))
    assert machine.last_merge == "?"


@pytest.mark.parametrize(
    "doc",
    [
        state(threads_fetched=False),  # degraded seed
        "{not json",  # malformed seed
        "",  # empty seed
        {"error": "no such PR"},  # error seed: only "fetched " is a baseline
    ],
)
def test_unusable_seed_falls_back_to_a_self_snapshot(tmp_path, monkeypatch, doc):
    monkeypatch.setenv("PR_WATCH_SEED_FILE", str(write_seed(tmp_path, doc)))
    poller = Poller("fetched live")
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    assert pr_watch.seed_baseline("7", "o/r") == "fetched live"
    assert poller.calls == [("7", "o/r")]


def test_missing_seed_file_falls_back_to_a_self_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_WATCH_SEED_FILE", str(tmp_path / "nope.json"))
    poller = Poller("fetched live")
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    assert pr_watch.seed_baseline("7", "") == "fetched live"


def test_unreadable_seed_path_falls_back_to_a_self_snapshot(tmp_path, monkeypatch):
    # A directory passes `[ -r ]` but cannot be read as a document.
    monkeypatch.setenv("PR_WATCH_SEED_FILE", str(tmp_path))
    poller = Poller("fetched live")
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    assert pr_watch.seed_baseline("7", "") == "fetched live"


def test_no_seed_env_uses_a_self_snapshot(monkeypatch):
    monkeypatch.delenv("PR_WATCH_SEED_FILE", raising=False)
    poller = Poller("fetched live")
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    assert pr_watch.seed_baseline("7", "") == "fetched live"


# --------------------------------------------------------------------------
# interval validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 5),
        ("", 5),
        ("0", 5),
        ("00", 5),
        ("abc", 5),
        ("3s", 5),
        (" 3", 5),
        ("-1", 5),
        ("1.5", 5),
        ("1", 1),
        ("007", 7),
        ("30", 30),
    ],
)
def test_poll_interval_validation(raw, expected):
    assert pr_watch.poll_interval(raw) == expected


# --------------------------------------------------------------------------
# CLI (no gh: the poll boundary and the sleep are replaced)
# --------------------------------------------------------------------------


def test_once_flag_exits_after_one_emission(monkeypatch, capsys):
    base, changed = sline(ready="false"), sline(ready="true")
    poller = Poller(base, base, changed, changed, changed)
    clock = Clock(limit=99)
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setattr(pr_watch, "_sleep", clock)
    assert pr_watch.main(["--once", "42", "o/r"]) == 0
    assert capsys.readouterr().out == changed + "\n"
    assert poller.calls == [("42", "o/r")] * 4  # seed + 3 polls
    assert clock.slept == [5, 5, 5]


def test_once_env_var_arms_the_same_exit(monkeypatch, capsys):
    changed = sline(ready="true")
    monkeypatch.setenv("PR_WATCH_ONCE", "1")
    monkeypatch.setattr(pr_watch, "snapshot", Poller(sline(), changed))
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=99))
    assert pr_watch.main(["42"]) == 0
    assert capsys.readouterr().out == changed + "\n"


def test_gh_repo_env_supplies_the_repo(monkeypatch):
    monkeypatch.setenv("GH_REPO", "acme/widgets")
    poller = Poller(sline())
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=2))
    with pytest.raises(LoopDone):
        pr_watch.main(["42"])
    assert poller.calls[0] == ("42", "acme/widgets")


def test_positional_repo_wins_over_gh_repo(monkeypatch):
    monkeypatch.setenv("GH_REPO", "acme/widgets")
    poller = Poller(sline())
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=2))
    with pytest.raises(LoopDone):
        pr_watch.main(["42", "other/repo"])
    assert poller.calls[0] == ("42", "other/repo")


def test_interval_env_drives_the_sleep(monkeypatch):
    monkeypatch.setenv("PR_WATCH_INTERVAL", "12")
    clock = Clock(limit=3)
    monkeypatch.setattr(pr_watch, "snapshot", Poller(sline()))
    monkeypatch.setattr(pr_watch, "_sleep", clock)
    with pytest.raises(LoopDone):
        pr_watch.main(["42"])
    assert clock.slept == [12, 12, 12]


def test_bad_interval_env_falls_back_to_five(monkeypatch):
    monkeypatch.setenv("PR_WATCH_INTERVAL", "0")
    clock = Clock(limit=2)
    monkeypatch.setattr(pr_watch, "snapshot", Poller(sline()))
    monkeypatch.setattr(pr_watch, "_sleep", clock)
    with pytest.raises(LoopDone):
        pr_watch.main(["42"])
    assert clock.slept == [5, 5]


def test_watcher_keeps_running_and_emits_each_real_change(monkeypatch, capsys):
    a, b, c = sline(unresolved=3), sline(unresolved=1), sline(unresolved=0)
    poller = Poller(a, a, b, b, c, c, c)
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=6))
    with pytest.raises(LoopDone):
        pr_watch.main(["42"])
    assert capsys.readouterr().out == b + "\n" + c + "\n"


def test_degraded_polls_emit_nothing_at_all(monkeypatch, capsys):
    monkeypatch.setattr(pr_watch, "snapshot", Poller(sline(), "", "", ""))
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=4))
    with pytest.raises(LoopDone):
        pr_watch.main(["42"])
    assert capsys.readouterr().out == ""


def test_every_emitted_line_is_flushed_immediately(monkeypatch):
    """One line == one Monitor notification: a buffered line is a missed
    wake-up, so the flush must follow the write."""

    class RecordingStdout:
        def __init__(self):
            self.events = []

        def write(self, text):
            self.events.append(("write", text))
            return len(text)

        def flush(self):
            self.events.append(("flush", None))

    rec = RecordingStdout()
    changed = sline(ready="true")
    monkeypatch.setattr(sys, "stdout", rec)
    monkeypatch.setattr(pr_watch, "snapshot", Poller(sline(), changed))
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=99))
    assert pr_watch.main(["--once", "42"]) == 0
    assert rec.events == [("write", changed + "\n"), ("flush", None)]


def test_unknown_flags_stay_positional_like_the_shell(monkeypatch):
    # The shell's arg loop only recognizes --once; everything else is a
    # positional, so a stray flag lands in the PR slot rather than erroring.
    poller = Poller(sline())
    monkeypatch.setattr(pr_watch, "snapshot", poller)
    monkeypatch.setattr(pr_watch, "_sleep", Clock(limit=2))
    with pytest.raises(LoopDone):
        pr_watch.main(["-x", "o/r"])
    assert poller.calls[0] == ("-x", "o/r")
