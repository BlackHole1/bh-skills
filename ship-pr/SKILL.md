---
name: ship-pr
description: >-
  Babysit an open pull request until it lands: wait out CI, work through
  reviewer-bot findings (fix and push the real ones, reply to false positives),
  and watch until every check passes and every review thread resolves. Use when
  the user wants a PR (by #, URL, or "my PR") landed, shipped, merged once
  ready, or watched while they step away, or wants reviewer-bot comments worked
  through. Merges unattended only with -y or the user's explicit go-ahead;
  otherwise stops at "ready to merge". Not for reviewing a diff, opening a PR,
  rebasing, or writing commit messages. Standalone or under /loop; uses gh CLI.
argument-hint: "[PR] [-y]"
allowed-tools: Bash(*pr_state.py*), Bash(*pr_comments.py*), Bash(*pr_reply.py*), Bash(*pr_merge.py*), Bash(*pr_watch.py*), Bash(*pr_worktree.py*), Bash(*pr_local_cleanup.py*), Bash(*commit_helper.py*), Bash(git -C * push origin HEAD:*), Bash(git -C * push https://github.com/* HEAD:*), Bash(git -C * status*), Bash(git -C * diff*), Bash(git -C * log*), Bash(git -C * show*), Bash(git -C * switch*), Bash(git -C * branch -D *), Bash(gh pr view *), Bash(gh pr checks *), Bash(gh pr diff *), Bash(gh pr edit *), Bash(gh repo view *), Bash(gh run view *), Bash(gh run rerun *), Bash(gh api graphql *), Bash(gh api repos/*), Read, Edit, Write, Grep, Glob, Skill, Monitor, TaskStop, ScheduleWakeup, PushNotification
---

# ship-pr

Drive a pull request from "open" to "merged" — or, without merge authorization,
to "ready to merge" — unattended. The hard part isn't any one step; it's the
waiting and the judgment: when to keep waiting, whether a finding is real, and
when the PR is truly safe to land. Trust your own judgment on the code and the
process; the rules below exist only where a wrong move is expensive — merging,
touching the user's checkout, resolving review threads.

Running this skill is the user's acceptance of its writes: pushing fix commits
to the PR head and, when authorized, merging (on Claude Code the frontmatter
pre-approves the helper scripts and worktree pushes, so don't hesitate before
them).

> `<skill-dir>` below is this skill's base directory, the folder holding this
> SKILL.md, which your harness names when it loads a skill. Substitute the real
> absolute path: your working directory is the user's repo, not the skill.
> The bundled scripts are stdlib-only Python 3; run them with `python3`, or
> `python` where that alias is missing (typical on Windows).

## Operating principles

- **One PR at a time.** Resolve the target once, pin it, and never touch other
  PRs or branches.
- **PR code lives in the isolated worktree, never the user's checkout.** The
  user may be developing in this same repo while you babysit. All inspection,
  triage, and fixes happen in a detached worktree synced to the PR head (below);
  this also keeps triage honest — you judge the real PR head, not whatever the
  user has checked out. Never force-push or rewrite history under review. The
  one sanctioned exception is the post-merge tidy-up in §5.
- **The merge decision is made at invocation, never by a mid-run question.**
  See "Merge policy". And regardless of authorization, never merge a PR that is
  `CONFLICTING`, has a required-review/required-check gap, or whose remaining
  failures you don't understand — surface those instead.
- **Only the reviewer resolves its own threads.** Fix-and-push, or reply, and
  let the bot verify and resolve; never call `resolveReviewThread` yourself
  (why: `<skill-dir>/references/reviewer-bots.md`).
- **Surface, don't guess.** A CI failure the diff doesn't explain, or an
  ambiguous or large reviewer ask, goes to the user with what you see.

## Merge policy (`-y`)

Whether the run ends in a merge is settled the moment you're invoked. Never ask
"merge now or wait for a human?" once the run has started.

- **`-y` present — or the user's own words authorize an unattended merge**
  ("land it, no review needed", "绿了就直接合并"): when the terminal gate holds
  (§5), merge without further confirmation. Restate the authorization once, up
  front — "Recorded: user authorized merging PR #N unattended" — so the session
  carries it unambiguously.
- **Otherwise: everything except the merge.** Get CI green, settle every
  finding, and when the PR is fully ready, report it ready, ping the user once
  if your harness can (Claude Code `PushNotification`), and end the run. The
  report is what matters; the ping is a nicety. The user merges it themselves or
  re-invokes with `-y`. Under `/loop`, later cycles just restate "ready —
  awaiting your merge".

One override in both modes: a human reviewer's `CHANGES_REQUESTED` outranks
`-y`. Push fixes and reply, but leave the merge to a human re-review
(`<skill-dir>/references/reviewer-bots.md`).

## Inputs

Resolve the target **once** at the start of the run and pin the result for every
later command. Accept a bare number, an `owner/repo#N` token, or a full PR URL;
never pass a raw URL into paths like `/tmp/ship-pr-<PR>.json`.

```bash
# 1) Number + repository (from URL, owner/repo#N, bare N, or current branch)
#    PR URL  -> number + owner/repo from the path
#    bare N  -> number N, repo from `gh repo view` of the checkout
#    omitted -> `gh pr view --json number,...` for the current branch
gh pr view <N> --repo <owner/repo> --json number,url,headRefName,baseRefName,state,headRepository \
  -q '{number,url,headRefName,baseRefName,state,headRepo:(.headRepository.nameWithOwner // .headRepository.owner.login + "/" + .headRepository.name)}'
# fallback when headRepository is absent on older gh:
#   gh pr view <N> -R <owner/repo> --json number,url,headRefName,baseRefName,state
#   plus gh api repos/<owner>/<repo>/pulls/<N> -q .head.repo.full_name for forks
```

Pin and reuse these fields for the whole run:

| Field | Use |
|---|---|
| `PR` | integer only — every helper arg and `/tmp/ship-pr-<PR>.json` |
| `REPO` | `owner/repo` of the PR's base repository — pass as the helpers' optional second arg / `-R` |
| `headRefName` | push destination branch name |
| `headRepo` | push URL for fork PRs (`https://github.com/<headRepo>.git`); same as `REPO` for same-repo heads |

No PR resolvable → ask which (or offer create-pr). Already `MERGED`/`CLOSED` →
report and stop.

## Isolated worktree

Create it the first time you need to read or change PR code — lazy, a clean
wait-then-merge PR never needs one:

```bash
wt=$(python3 <skill-dir>/scripts/pr_worktree.py ensure <PR> <REPO>)   # prints the path
```

Use the pinned `PR` (integer) and `REPO` from Inputs — never a URL — for the
worktree key, seed file `/tmp/ship-pr-<PR>.json`, and every helper.

- Re-run `ensure` at the start of every round: it hard-resets to the *current*
  PR head, so you always judge and edit the real code, never a stale copy.
- It parks detached on the head commit, so it can never collide with the user's
  checkout of the same branch.
- Push fixes with `git -C "$wt" push origin HEAD:<headRefName>`. Fork PR: push
  to the pinned head repository instead (`git -C "$wt" push
  https://github.com/<headRepo>.git HEAD:<headRefName>`); if you can't push to
  the fork, surface it rather than merging around the finding.
- After the merge: `python3 <skill-dir>/scripts/pr_worktree.py remove <PR> <REPO>`.

## The loop

Each cycle: **assess → classify → act → wait**. Repeat until merged (or, without
merge authorization, until ready).

### 1. Assess

```bash
python3 <skill-dir>/scripts/pr_state.py <PR> | tee /tmp/ship-pr-<PR>.json
```

One JSON snapshot: per-check buckets, `ci_all_pass`, reviewer-bot check state,
unresolved-thread count, `mergeable`, `mergeStateStatus`, `ready_to_merge`.
Always `tee` it — §4 seeds the watcher from this exact file.
`threads_fetched:false` means the thread read degraded and `ready_to_merge` is
forced false: retry the snapshot, never merge on it. Raw commands, if you need
detail beyond the script: `<skill-dir>/references/gh-cookbook.md`.

### 2. Classify

First match wins:

| Situation | Signal | Go to |
|---|---|---|
| **Conflicts / blocked merge** | `mergeable=CONFLICTING`, or a required gate you can't satisfy | Surface, stop |
| **Real CI failure** | a non-bot check is `fail` | §3a |
| **Review findings to handle** | any unresolved review thread, or "changes requested" | §3b |
| **Reviewer bot still working** | bot check `pending` / no review yet, rest green | §4 |
| **All green, nothing unresolved** | `ready_to_merge:true` | §5 |

A *passing* bot check with zero actionable comments is the clean case;
`pr_comments.py` surfaces the bot's own verdict ("No actionable comments…" /
"Actionable comments posted: N") in its header line.

### 3. Act

#### 3a. Real CI failure

Pull the failing job's log (`gh run view <id> --log-failed`) and decide:

- **Caused by the diff** (test/lint/type/build broke) → fix it in the worktree.
  Prefer checks the allowlist already covers (read project docs, re-read the
  diff, lean on the next CI run after push). Do not invent unrestricted shell
  access to run arbitrary `npm test` / `make` / CI scripts — if a local check
  is essential and outside the allowlist, surface that gap instead of
  widening permissions. Then commit and push (see "Committing and PR
  hygiene"). The push re-triggers CI and re-review → back to Assess.
- **Flaky or infra** (network, runner, unrelated) → `gh run rerun <run-id>
  --failed` once; if it fails again, surface.
- **Not yours to own** (secrets, required approvals) → surface.

#### 3b. Review findings

```bash
python3 <skill-dir>/scripts/pr_comments.py <PR>   # cleaned, triage-ready Markdown of every unresolved finding
```

Don't hand-fetch raw GraphQL/REST — the script strips the HTML comments, base64
state blobs, and duplicated suggestion blocks the bots bury their point in, and
prints per finding: severity/type tags, `path:line`, the finding text, one fix
diff, and the `reply-to id` (flags: `--all`, `--full`, `--json`).

Read the code each finding points at in the worktree, then judge each:

- **Real** → fix in the worktree, run the project's checks, commit and push
  (below). The bot re-reviews the new commit and resolves its own thread. For
  many or subtle cross-cutting findings, fan the analysis out over subagents if
  your harness has them (Claude Code `/workflows` or the Agent tool, Grok
  `spawn_subagent`), and verify before changing code.
- **False positive / won't-fix** → reply in-thread with a concise,
  code-grounded reason. Pipe the body — inline quoting breaks on apostrophes:
  ```bash
  printf '%s' "@coderabbitai This is intentional: <reason>." \
    | python3 <skill-dir>/scripts/pr_reply.py <PR> <reply-to id>
  ```
  It posts once, retries transient EOF without double-posting. Then wait for
  the bot; if it pushes back with a fair point, reconsider.

Batch the round: related fixes in one commit, all replies posted, then one wait.
One push + several replies beats many tiny round-trips.

#### Committing and PR hygiene

- **Every commit goes through the commit skill** — Claude Code: the `Skill`
  tool, name `commit`; Codex: `$commit`; Grok and others: the `commit` skill. If
  your harness cannot invoke another skill, read `<skill-dir>/../commit/SKILL.md`
  and follow it yourself. Either way, run its steps with the worktree as the
  working directory. On the worktree's
  detached HEAD it carves a temporary branch — fine. Push the head back with
  `git -C "$wt" push origin HEAD:<headRefName>`, then drop the temp branch
  (`git -C "$wt" switch --detach && git -C "$wt" branch -D <temp>`) so the
  shared repo gains no stray branches.
- **If your accumulated fixes change what the PR means** — its title or body no
  longer honest about the diff — refresh them with the **create-pr skill**,
  which updates an open PR's title and body in place and preserves
  human-written content. Its `prepare` routing stops on a detached checkout;
  you already hold the PR number and diff, so apply its update path
  (`gh pr edit <N>`) directly from here.

### 4. Wait

You're waiting on an external event — CI finishing, the bot posting. Never
hand-roll `sleep N && <poll>`: `pr_watch.py` already owns the polling, debounced
so churn never wakes you. Run it whichever of these two ways your harness allows.

**With a streaming-monitor tool** (Claude Code `Monitor`, Grok `monitor`) — arm
it and let it wake you:

```text
Monitor(
  description: "PR <N>: CI + reviewer-bot state changes",
  persistent: true,
  command: PR_WATCH_SEED_FILE=/tmp/ship-pr-<N>.json python3 <skill-dir>/scripts/pr_watch.py <N>
)
```

**Without one** (Codex, or any harness with no background-event tool) — run it
in the foreground with `--once`. It blocks and returns on the first real
transition, then you loop straight back to Assess:

```bash
PR_WATCH_SEED_FILE=/tmp/ship-pr-<N>.json python3 <skill-dir>/scripts/pr_watch.py <N> --once
```

A shell tool that caps command runtime just ends the wait early with no output;
that is a plain "nothing happened yet", so re-run it.

- **Seed from the §1 snapshot file**, passing its literal path (a shell `$var`
  won't survive into the watcher's process). The seed must be the state you
  *classified on*: a fresh read taken at start-up lands after the assess→wait
  gap and swallows any transition inside it, exactly the miss the seed exists
  to close.
- `pr_watch.py` polls every ~5s (`PR_WATCH_INTERVAL` to override) and emits one
  line per debounced, real transition — merge-recompute churn, bot comment
  edits, and one-poll flashes never wake you.
- **After arming a monitor, assess once more.** A monitor runs detached, so the
  state can move between arming it and your next turn. If it moved, loop back to
  Classify now instead of waiting for a heartbeat. If it still shows "pending",
  you're genuinely waiting: where the harness offers one, add a fallback
  heartbeat (Claude Code `ScheduleWakeup` ~900s, Grok `scheduler_create`) in
  case an event is missed, and stop the monitor (Claude Code `TaskStop`, Grok
  `kill_command_or_subagent`) once the terminal state is reached. The `--once`
  path needs none of this — it is already synchronous.

**Under `/loop`** (Claude Code or Grok): don't arm anything — do one
assess→classify→act cycle per invocation and return; `/loop` provides the cadence.

### 5. Merge — or report ready

Terminal gate: `pr_state.py` reports `ready_to_merge:true` (every check `pass`,
0 unresolved threads, `MERGEABLE`, `CLEAN`, `threads_fetched:true`).

- **Not authorized (no `-y`, no explicit go-ahead):** report the PR ready, ping
  the user once if your harness can (Claude Code `PushNotification`), stop. The
  run is complete.
- **Authorized:**
  ```bash
  python3 <skill-dir>/scripts/pr_merge.py <N>   # squash by default; --strategy merge|rebase, --subject/--body
  ```
  It re-confirms the gate (exit 3 = not actually ready → back to Assess),
  preserves the repo's DCO sign-off, retries transient EOF, is a no-op if
  already merged, and verifies the result. Match the repo's squash-subject
  convention (default `<PR title> (#N)`). If the harness itself still gates the
  call despite the recorded authorization, don't route around it with a raw
  `gh pr merge`: notify once, leave the go-ahead request as your last word so
  the user's reply resumes you, and merge then.

Once merged, tidy up:

- **Your work area:** `python3 <skill-dir>/scripts/pr_worktree.py remove <PR>`, and stop
  any monitor you armed (§4).
- **The user's checkout:** `python3 <skill-dir>/scripts/pr_local_cleanup.py <PR>` — the one
  sanctioned touch of it. Returns them to the base branch, fast-forwards, and
  deletes the merged local branch, each step self-gated (confirmed `MERGED`,
  clean tree, branch proven contained in what GitHub merged). Exit 3 means
  "left as-is", not a failure — relay its one-line reason. Don't hand-roll
  `checkout && pull && branch -D` — that skips the proof-of-merge gates.

Then report: merge commit (or "ready to merge"), what was fixed, what was
replied to, and what the local cleanup did or why it skipped.

## Bundled resources

All scripts retry transient GitHub API failures (`EOF`/5xx) internally — prefer
them over hand-issued `gh api`.

- `<skill-dir>/scripts/pr_state.py <PR> [OWNER/REPO]` — one-shot JSON health snapshot; use
  for every Assess.
- `<skill-dir>/scripts/pr_comments.py <PR> [OWNER/REPO] [--all|--full|--json|--no-summary]`
  — unresolved findings as cleaned, triage-ready Markdown; run it the moment a
  bot posts findings or `unresolved>0`.
- `<skill-dir>/scripts/pr_reply.py <PR> <reply-to id> [OWNER/REPO]` — idempotent in-thread
  reply (body from stdin or `--body-file`).
- `<skill-dir>/scripts/pr_merge.py <PR> [OWNER/REPO] [--subject|--body|--strategy]` —
  gated, DCO-preserving, idempotent, verify-after merge.
- `<skill-dir>/scripts/pr_watch.py <PR> [OWNER/REPO] [--once]` — debounced
  change-emitting poll loop; seed via `PR_WATCH_SEED_FILE`. Streams for a
  monitor tool, or blocks until the first real change with `--once`.
- `<skill-dir>/scripts/pr_worktree.py ensure|remove|path <PR> [OWNER/REPO]` — the isolated
  detached worktree (or clone) synced to the PR head.
- `<skill-dir>/scripts/pr_local_cleanup.py <PR> [OWNER/REPO] [--base B] [--dry-run]` —
  post-merge only; self-gated tidy of the user's local checkout.
- `<skill-dir>/scripts/gh_retry.py` — retry wrapper the other scripts import.
- `<skill-dir>/references/gh-cookbook.md` — raw `gh` / GraphQL commands behind the scripts.
- `<skill-dir>/references/reviewer-bots.md` — reading CodeRabbit and other reviewers:
  verdicts, resolution semantics, human reviewers, review-vs-CI checks.
