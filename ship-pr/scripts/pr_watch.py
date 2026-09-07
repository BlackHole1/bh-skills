#!/usr/bin/env python3
"""Emit a single line whenever a PR's CI / reviewer-bot / review-thread state
changes *for real*. Built for the Monitor tool (run it with persistent: true):
each printed line becomes one notification, so you are woken on the *transition*
(CI finished, review posted, new comments, ready to merge) instead of on a timer.

Debounced on purpose. GitHub recomputes mergeability after every push (flashing
mergeStateStatus null/UNKNOWN) and CodeRabbit edits its comments mid-review
(jittering the comment count and momentarily dropping its status check), and a
transient read can briefly show unresolved=0/ready=true. Naively emitting on any
string change wakes you on this churn — and a one-poll ready=true flash could
even trip a premature merge. So this script:
  - carries forward the last stable mergeStateStatus when it reads null/UNKNOWN
  - treats an empty reviewer-bot check bucket as "pending" (bot vanishing
    mid-review is not a change)
  - keys the state on the PR head SHA, so a push is a real transition
  - requires a candidate state to PERSIST across 2 consecutive polls before
    emitting (so jitter and one-poll flashes never wake you), and never emits a
    line at all while the snapshot is a degraded read (threads fetch failed)

Usage (inside Monitor): pr_watch.py [PR_NUMBER] [OWNER/REPO]
Poll interval defaults to 5s; override with PR_WATCH_INTERVAL (seconds).

--once (or PR_WATCH_ONCE=1) exits after emitting the first real change, turning
  the same debounced logic into a foreground "block until something happens"
  call. That is the portable wait: an agent with no streaming-monitor tool just
  runs this and blocks, instead of hand-rolling a sleep/poll loop.
Seed baseline: set PR_WATCH_SEED_FILE to the pr_state.py JSON the agent already
  assessed, so a transition that lands between that assess and this watcher
  starting is emitted rather than swallowed into a fresh self-seed (see
  seed_baseline).

Requires: Python 3.9+, gh (authenticated) on PATH, and the sibling pr_state.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Mapping, Optional, Sequence

import pr_state
from gh_retry import force_utf8, loads_json

DEFAULT_INTERVAL = 5
CARRY_FORWARD = ("null", "UNKNOWN")  # merge states that are "GitHub is thinking"


# --------------------------------------------------------------------------
# jq-compatible scalar rendering (the shell rendered the line with `jq -r`)
# --------------------------------------------------------------------------


def _jq_str(value: Any) -> str:
    """jq string-interpolation of a value: null/true/false render lowercase,
    strings render verbatim, everything else as compact JSON."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    # Numbers, arrays and objects interpolate as their compact JSON text. Every
    # count pr_state produces is an int, so this is exact.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _jq_length(value: Any) -> int:
    """jq's `length`: null is 0 (not an error), arrays/strings/objects their
    size, numbers their absolute value."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise TypeError("boolean has no length")
    if isinstance(value, (list, str, dict)):
        return len(value)
    if isinstance(value, (int, float)):
        return abs(value)
    raise TypeError("has no length")


def _bot_buckets(checks: Any) -> str:
    """`.review_bot_checks | map(.bucket) | join("/")`. jq's join renders null
    elements as the empty string, and iterating null is an error (which the
    shell's `jq 2>/dev/null` turned into a degraded, held read)."""
    if checks is None:
        raise TypeError("Cannot iterate over null")
    parts = []
    for check in checks:
        if check is None:
            # jq indexes null happily (`null | .bucket` is null) and joins it as
            # "". A null ELEMENT is survivable malformation: it must render the
            # empty bucket the caller turns into bot=pending, not raise and cost
            # us the whole line (silence, where the shell emitted "pending").
            parts.append("")
            continue
        if not isinstance(check, dict):
            raise TypeError("Cannot index non-object")
        bucket = check.get("bucket")
        parts.append("" if bucket is None else _jq_str(bucket))
    return "/".join(parts)


def normalize(state: Mapping[str, Any]) -> str:
    """One normalized state line. A transient degraded read (threads fetch
    failed) yields "" so we HOLD; a permanent error (no such PR, deleted or
    transferred) yields an "ERROR …" sentinel so the agent is woken instead of
    waiting forever. bot="" -> "pending".

    Split out from snapshot() so the very same normalization can seed the
    baseline from a snapshot the agent already captured (PR_WATCH_SEED_FILE)."""
    if not isinstance(state, Mapping):
        raise TypeError("Cannot index non-object")
    error = state.get("error")
    # jq truthiness: only null and false are falsy, so `{"error": ""}` still
    # takes the error branch.
    if error is not None and error is not False:
        return "ERROR " + _jq_str(error)
    if state.get("threads_fetched") is False:
        return ""
    bot = _bot_buckets(state.get("review_bot_checks"))
    return (
        "fetched ci_pass={} ci_fail={} ".format(
            _jq_str(state.get("ci_all_pass")), _jq_length(state.get("ci_failing"))
        )
        + "bot={} ".format("pending" if bot == "" else bot)
        + "unresolved={} comments={} ".format(
            _jq_str(state.get("review_threads_unresolved")),
            _jq_str(state.get("review_comment_count")),
        )
        + "merge={} head={} ready={}".format(
            _jq_str(state.get("mergeStateStatus")),
            _jq_str(state.get("head")),
            _jq_str(state.get("ready_to_merge")),
        )
    )


def normalize_text(text: str) -> str:
    """normalize() over a pr_state JSON document. Anything unparseable yields
    "" — exactly what the shell's `jq -r … 2>/dev/null` produced, i.e. a held
    (degraded) read rather than a bogus state line. A leading UTF-8 BOM is
    parseable (jq skipped it, so does loads_json): rejecting a BOM-prefixed
    PR_WATCH_SEED_FILE would silently discard the agent's pre-arm baseline and
    reopen the assess -> arm window the seed exists to close."""
    if not text or not text.strip():
        return ""
    try:
        return normalize(loads_json(text))
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Poll boundary (module-level so tests can monkeypatch it — no gh, no sleeping)
# --------------------------------------------------------------------------


def snapshot(pr: Any, repo: str) -> str:
    """Live poll: one pr_state snapshot, normalized. A snapshot that blows up
    (the shell's `pr-state.sh … 2>/dev/null` dying) reads as degraded -> hold."""
    if not pr:
        pr = _resolve_pr(repo)
    if not pr:
        return normalize({"error": pr_state.NO_PR_MESSAGE})
    try:
        return normalize(pr_state.collect_state(pr, repo))
    except Exception:
        return ""


def _resolve_pr(repo: str) -> str:
    """PR number of the current branch, or "" (seam: tests never call gh)."""
    return pr_state.resolve_pr(repo)


def _sleep(seconds: float) -> None:
    """Poll delay (seam: tests never actually wait)."""
    time.sleep(seconds)


# --------------------------------------------------------------------------
# Debounce / dedup
# --------------------------------------------------------------------------

# The debounce/dedup KEY is the semantic state with the comment count stripped:
# `review_comment_count` jitters continuously while CodeRabbit edits its
# comments, so keying on it would starve a real transition (it could never hold
# for 2 polls). The comment count still rides along in the emitted line for
# context. Greedy, leftmost, single replacement — the shell's
# `${1/comments=*merge=/merge=}`.
_KEY_RE = re.compile(r"comments=.*merge=", re.S)


def keyof(line: str) -> str:
    return _KEY_RE.sub("merge=", line, count=1)


def merge_value(line: str) -> str:
    """The mergeStateStatus token of a state line, mirroring the shell's
    `${raw#*merge=}` then `${merge%% *}`: the text after the FIRST "merge="
    up to the first space (or, with no "merge=" at all, the first word)."""
    idx = line.find("merge=")
    rest = line[idx + len("merge=") :] if idx >= 0 else line
    return rest.split(" ", 1)[0]


class Debouncer:
    """The pure state machine behind the watcher. Feed it one raw state line
    per poll; it returns the line to emit, or None to stay quiet."""

    def __init__(self, emitted: str = "", last_merge: str = "?") -> None:
        self.emitted = emitted  # key of the last line we emitted
        self.cand = ""  # current candidate key
        self.cand_n = 0  # how many consecutive polls it has held
        self.last_merge = last_merge  # last non-null/UNKNOWN mergeStateStatus

    @classmethod
    def seeded(cls, seed: str) -> "Debouncer":
        """Baseline from an already-normalized state line (see seed_baseline)."""
        machine = cls()
        if seed:
            machine.emitted = keyof(seed)
            value = merge_value(seed)
            if value not in CARRY_FORWARD:
                machine.last_merge = value
        return machine

    def step(self, raw: str) -> Optional[str]:
        if not raw:
            return None  # transient degraded read — hold, candidate untouched

        if raw.startswith("ERROR"):
            line = raw  # permanent error — wake on it (same 2-poll persistence)
        else:
            merge = merge_value(raw)
            if merge in CARRY_FORWARD:
                # GitHub recomputes mergeability after every push; a null/UNKNOWN
                # flash is not a state change.
                line = raw.replace("merge=" + merge, "merge=" + self.last_merge, 1)
            else:
                line = raw
                self.last_merge = merge
        key = keyof(line)

        if key == self.cand:
            self.cand_n += 1
        else:
            self.cand = key
            self.cand_n = 1  # new candidate — start its streak

        # Emit a state that has held for 2 polls and differs (semantically)
        # from the last one emitted.
        if self.cand_n >= 2 and key != self.emitted:
            self.emitted = key
            return line
        return None


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def seed_baseline(pr: Any, repo: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Seed once so the first emission is the first real, debounced change.

    Prefer the snapshot the agent already assessed (PR_WATCH_SEED_FILE) as the
    baseline. The agent reads PR state, decides "still pending — wait", then
    arms this watcher; in the seconds between that read and this process
    starting, the very event it is waiting for can land (CI flips, the bot posts
    its review). A fresh self-seed would absorb that already-completed
    transition as the baseline and stay silent until the fallback heartbeat
    fires ~15 min later. Seeding from the agent's own pre-arm snapshot instead
    means any change since then differs from the baseline and is emitted on the
    first qualifying poll. A missing, unreadable, or degraded seed file falls
    back to a self-snapshot — the original behavior, fully intact."""
    env = os.environ if env is None else env
    seed = ""
    path = env.get("PR_WATCH_SEED_FILE", "")
    if path and os.access(path, os.R_OK):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            text = ""
        candidate = normalize_text(text)
        if candidate.startswith("fetched "):
            seed = candidate  # only a clean snapshot is a valid baseline
    if not seed:
        seed = snapshot(pr, repo)
    return seed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def poll_interval(value: Optional[str]) -> int:
    """PR_WATCH_INTERVAL -> whole seconds. Empty, zero, or any non-digit falls
    back to the 5s default so a bad value can never become a busy spin."""
    if not value or not all(c in "0123456789" for c in value):
        return DEFAULT_INTERVAL
    return int(value) or DEFAULT_INTERVAL


def main(argv: Sequence[str]) -> int:
    force_utf8()
    env = os.environ

    # Hand-rolled flag loop, mirroring the shell `case`: --once is consumed
    # anywhere in the argv, everything else stays positional. Any non-empty
    # PR_WATCH_ONCE arms it too (the shell tested `[ -n "$once" ]`).
    once = bool(env.get("PR_WATCH_ONCE", ""))
    rest = []
    for arg in argv:
        if arg == "--once":
            once = True
        else:
            rest.append(arg)

    pr = rest[0] if rest else ""
    repo = (rest[1] if len(rest) > 1 else "") or env.get("GH_REPO", "")
    interval = poll_interval(env.get("PR_WATCH_INTERVAL"))
    if not pr:
        pr = _resolve_pr(repo)

    machine = Debouncer.seeded(seed_baseline(pr, repo, env))

    while True:
        _sleep(interval)
        line = machine.step(snapshot(pr, repo))
        if line is None:
            continue
        # One line == one Monitor notification, so flush immediately: a line
        # sitting in a pipe buffer is a missed wake-up.
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if once:
            return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
