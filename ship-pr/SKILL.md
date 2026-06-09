---
name: ship-pr
description: >-
  Use this when the user wants an open pull request merged for them once it's
  ready — not right now, but after CI finishes and the review clears. Covers
  "merge it once CI's green and the review's done," "land it when the pipeline
  passes," "wait for github actions then squash-merge," and "go through
  CodeRabbit's comments, fix the real ones, reply to the wrong ones, merge when
  threads resolve." Also hand-offs — babysit, watch, keep an eye on, finish,
  land, or ship a PR (by #, URL, or "my PR") until it's in main, often while the
  user steps away. The work: wait for green CI, handle reviewer-bot findings (fix
  and push real issues, reply to false positives), then squash-merge when checks
  pass and reviews resolve. Not for reviewing a diff, opening a PR, rebasing,
  squashing commits, or writing commit messages. Standalone or under /loop; uses
  gh CLI.
---

# ship-pr

Drive a pull request from "open" to "merged" without supervision: get CI green,
resolve everything the reviewers raise, then merge. The hard part isn't any one
step — it's the *waiting* and the *judgment*: knowing when to keep waiting, when
a finding is real versus noise, and when the PR is truly safe to land.

## Operating principles

- **One PR at a time.** Everything you do targets a single, explicit PR. Never
  touch other PRs or branches. Resolve the target once at the start and reuse it.
- **Read and change PR code only in an isolated worktree, never the user's
  checkout.** The user may be developing in this same repo while you babysit, so
  never touch their working tree, index, or branch. Do all code inspection,
  diagnosis, and fixes in a detached worktree synced to the PR head (see
  "Isolated worktree" below). This also keeps your *triage* honest: you judge the
  real PR head, not whatever the user has checked out. Fix-and-push targets the
  PR head branch; never force-push or rewrite history under review.
- **You may merge automatically** once the terminal condition holds (see below).
  This skill is meant to land PRs unattended. But *never* merge a PR that is
  `CONFLICTING`, has a required-review/required-check gap, or whose remaining
  failures you don't understand — surface those to the user instead.
- **Only the reviewer bot resolves its own threads.** You must not resolve review
  conversations. The bot opened them to verify a concern; resolving on its behalf
  hides whether the concern was actually addressed and breaks the safety loop.
  You either *fix and push* (the bot re-reviews and resolves) or *reply* (the bot
  responds and resolves). See "Why you don't resolve threads" below.
- **Be honest about uncertainty.** If a CI failure isn't explained by the diff,
  or a reviewer ask is ambiguous or large, stop and tell the user what you see
  rather than guessing.

## Inputs

- **Target PR**: an explicit number if given, otherwise the PR for the current
  branch. Resolve and pin it:
  ```bash
  gh pr view --json number,url,headRefName,baseRefName,state -q '.number'   # current branch's PR
  gh repo view --json nameWithOwner -q .nameWithOwner                        # owner/repo
  ```
  If there is no PR for the current branch and none was named, ask which PR (or
  offer to open one). If the PR is already `MERGED`/`CLOSED`, report and stop.

## Isolated worktree (your view of the PR's code)

You touch the PR's files for exactly two reasons — to *triage* a finding (decide
if it is real) and to *fix* one. Both must act on the PR's **actual head code**,
not the user's working tree, which may be a different branch or stale. So the
first time in a babysit session that you need to read or change PR code, create
one isolated worktree and reuse it:

```bash
wt=$(scripts/pr-worktree.sh ensure <PR> [OWNER/REPO])   # prints the path
```

- **Lazy:** don't create it for a clean PR that only needs wait-then-merge —
  only when there's a finding to triage or a CI failure to diagnose.
- **Always at the current head:** call `ensure` again at the start of every
  round; it hard-resets the worktree to the *current* PR head, so after you push
  a fix (or anyone else updates the PR) your next triage reads the new code, not
  a stale copy.
- **Detached on purpose:** git refuses to check out a branch already checked out
  in another worktree, so if the user is sitting on the PR branch a normal
  checkout would fail. The worktree parks on the head *commit* (detached) and you
  push fixes with `HEAD:<headRefName>`, which never collides with their checkout.
- **Push the fix back** from the worktree:
  - same-repo PR: `git -C "$wt" push origin HEAD:<headRefName>`
  - fork PR: push to the fork instead
    (`git -C "$wt" push https://github.com/<headOwner>/<headRepo>.git HEAD:<headRefName>`,
    needs write access to the fork). If you can't push to the fork, surface to
    the user rather than merging around the finding.
- **Cleanup:** after the merge, `scripts/pr-worktree.sh remove <PR> [OWNER/REPO]`.

`ensure` uses a real `git worktree` when you're inside a local clone of the
target repo (cheap — shares the object store) and falls back to a throwaway clone
when you're not. Assessing status and merging never need the worktree — they're
pure `gh` calls.

## The loop

Each cycle is **assess → classify → act → wait**. Repeat until merged.

### 1. Assess

Take one snapshot of the PR's health. The bundled helper does this in one shot:

```bash
scripts/pr-state.sh <PR>
```

It prints a compact JSON with: each check's bucket, whether the non-bot checks
all pass, the reviewer-bot check state, counts of review threads and *unresolved*
review threads, `mergeable`, and `mergeStateStatus`. Read
`references/gh-cookbook.md` for the raw commands if you need detail or the script
is unavailable.

### 2. Classify the current state

Decide which situation you're in (check in this order):

| Situation | Signal | Go to |
|---|---|---|
| **Conflicts / blocked merge** | `mergeable=CONFLICTING`, or a required gate that can't be satisfied automatically | Surface to user, stop |
| **Real CI failure** | a non-bot check is `fail` | Fix CI (§3a) |
| **Review findings to handle** | any unresolved review thread, or "changes requested" | Triage findings (§3b) |
| **Reviewer bot still working** | bot check `pending` / no review yet, other checks green | Wait (§4) |
| **All green, nothing unresolved** | all checks pass, 0 unresolved threads, `mergeable=MERGEABLE` | Merge (§5) |

A note on the reviewer bot: a *passing* bot check with **zero actionable
comments** is the clean case. CodeRabbit states this explicitly in its summary
comment ("No actionable comments were generated…" or "Actionable comments
posted: N"). Read that summary to confirm before treating the review as clear —
see `references/reviewer-bots.md`.

### 3. Act

#### 3a. Real CI failure

Pull the failing job's log, find the cause, and decide:
- **Caused by the diff** (test/lint/type/build broke) → fix it **in the worktree**
  (`wt=$(scripts/pr-worktree.sh ensure <PR>)`, synced to the PR head). Run the
  project's own checks there first (discover them from `CLAUDE.md`,
  `package.json`/`Makefile`/CI config — e.g. lint, type-check, dead-code, tests),
  commit with a conventional message, and push the head back
  (`git -C "$wt" push origin HEAD:<headRefName>`). Pushing re-triggers CI and
  re-review, so you loop back to Assess.
- **Flaky or infra** (network, runner, unrelated) → re-run the job
  (`gh run rerun <run-id> --failed`) once; if it still fails, surface to user.
- **Not a code problem you can own** (secrets, required approval) → surface.

#### 3b. Triage review findings

Read every unresolved thread (commands in `references/gh-cookbook.md`), and read
the code each one points at **in the worktree** (`scripts/pr-worktree.sh ensure
<PR>`, synced to the PR head) — so you judge the PR's real current code, not the
user's checkout. For each, make a judgment call:

- **Real issue** → fix it in the worktree. For a handful of small, clear
  findings, just fix them. For many findings, or subtle/cross-cutting ones, use
  `/workflows` to fan out analysis and adversarially verify before changing code
  — it's worth the tokens to avoid thrashing. After fixing: run the project's
  checks in the worktree, commit, and push the head
  (`git -C "$wt" push origin HEAD:<headRefName>`). The bot re-reviews the new
  commit and resolves the thread itself.
- **False positive / won't-fix** → **reply** to that specific review comment
  explaining why (concise, technical, points at the code). Then wait for the bot
  to respond. Do **not** resolve the thread. CodeRabbit will either accept (and
  resolve) or push back; if it pushes back with a fair point, reconsider.

Batch your response: fix all the real ones in one commit/push where they're
related, and post replies to the false positives, then go wait. One push +
several replies is better than many tiny round-trips.

### 4. Wait (the part that needs patience)

You're waiting for an external event — CI finishing, or the bot posting/finishing
a review. Don't burn cycles polling by hand.

**Standalone (default):** arm a persistent background monitor that emits only
when something changes, then let it wake you:

```
Monitor(
  description: "PR <N>: CI + reviewer-bot state changes",
  persistent: true,
  command: scripts/pr-watch.sh <N>
)
```
`pr-watch.sh` polls every ~10s (override with `PR_WATCH_INTERVAL`) and prints a
line only when something meaningful
changes: check buckets, unresolved-thread count, the total review-comment count
(so a reviewer's *reply* that doesn't resolve a thread still wakes you), or the
merge state. You're notified on the *transition* (CI done, review posted, bot
replied/resolved, ready to merge) instead of on a timer. Pair it with a long
fallback heartbeat (e.g. `ScheduleWakeup` ~1500s) in case an event is missed.
When the terminal condition is reached, `TaskStop` the monitor.

**Composed under /loop:** if the user wrapped this in `/loop`, don't arm your own
monitor — do a single assess→classify→act cycle and return. `/loop` re-invokes
you on its own cadence. (`/loop` with no interval self-paces and is a good fit.)

### 5. Merge

Only when **all** of: every check `pass`, **0 unresolved** review threads,
`mergeable=MERGEABLE`, `mergeStateStatus=CLEAN`.

Match the repo's merge convention (most squash-merge repos show single-line
`type(scope): subject (#NNN)` history). Default to squash:

```bash
gh pr merge <N> --squash --delete-branch \
  --subject "<PR title> (#<N>)" \
  --body "<short rationale; preserve any Signed-off-by if the repo uses DCO>"
```
Then clean up — `scripts/pr-worktree.sh remove <PR>` if you created one, and
`TaskStop` any monitor you armed — and report: merge commit, what was fixed, what
was replied to. Don't schedule further work — the goal is done.

## Why you don't resolve threads

GitHub lets anyone with write access resolve a review thread (GraphQL
`resolveReviewThread`). It's tempting to resolve threads to make the PR look
clean. Don't. The reviewer bot tracks its own findings by whether *it* resolved
them after seeing your fix or reply. If you resolve a thread the bot still
considers open, you (a) suppress a concern that may be unaddressed and (b) can
desync the bot, which may re-open or re-comment. Let the loop work: fix→push or
reply→wait, and the bot resolves when satisfied. This skill never calls
`resolveReviewThread`.

## Reviewer bots beyond CodeRabbit

CodeRabbit is the primary case, but the logic generalizes: treat **any**
unresolved review thread or "changes requested" as blocking, fix or reply, and
let the reviewer (bot or human) resolve. Human reviewers won't auto-resolve on a
timer — if a human requested changes, push your fix and then it's reasonable to
leave a short reply and let the user know a human re-review is pending rather than
waiting indefinitely. See `references/reviewer-bots.md`.

## Bundled resources

- `scripts/pr-state.sh <PR> [OWNER/REPO]` — one-shot JSON snapshot of CI +
  review + merge state. Use it for every Assess. The repo arg is optional when
  you run inside the target repo (gh infers it from the working directory).
- `scripts/pr-watch.sh <PR> [OWNER/REPO]` — change-emitting poll loop for the
  `Monitor` tool.
- `scripts/pr-worktree.sh ensure|remove|path <PR> [OWNER/REPO]` — create / refresh
  / tear down an isolated detached worktree (or clone) synced to the PR head, so
  triage and fixes never touch the user's working tree.
- `references/gh-cookbook.md` — exact `gh` / `gh api` / GraphQL commands for
  reading checks, reading and replying to review threads, re-running jobs, and
  merging.
- `references/reviewer-bots.md` — how to read CodeRabbit's summary and comments,
  what "resolved" means, and how to generalize to other reviewers.
