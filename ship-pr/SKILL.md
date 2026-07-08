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
  pass and reviews resolve — and once it's merged, tidy the local checkout (switch
  back to the base branch, pull, delete the merged branch) when the working tree is
  clean. Not for reviewing a diff, opening a PR, rebasing,
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
  PR head branch; never force-push or rewrite history under review. The *one*
  sanctioned exception is the post-merge tidy-up in §5 — and only after the merge
  is confirmed and their tree is clean (see `pr-local-cleanup.sh`).
- **You may merge automatically** once the terminal condition holds (see below).
  This skill is meant to land PRs unattended. But *never* merge a PR that is
  `CONFLICTING`, has a required-review/required-check gap, or whose remaining
  failures you don't understand — surface those to the user instead.
- **Settle the merge authorization before you start waiting, not at merge time.**
  In auto-accept mode the harness screens the `gh pr merge` call and blocks
  merging a human-unreviewed PR unless the user has explicitly okayed merging
  without human review — a harness safety check, distinct from the GitHub
  required-review gap named above. By merge time the user has walked away and
  can't okay it, so the PR strands green-but-unmerged, the exact outcome this
  skill exists to prevent. Get that go-ahead up front, while they're still here
  (see "Merge authorization" below).
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

## Merge authorization — settle it before you wait

The final step is a `gh pr merge`. Under auto-accept mode the harness screens that
call with a safety classifier, and it will **deny** the merge when the PR has no
human approval visible — only CI and a reviewer-bot pass — with extra weight when
you authored the PR in this same session. It isn't wrong to flag that: merging your
own code with no human eyes on it is a real judgment call. What it looks for is the
**user** having explicitly authorized merging without human review — a reviewer-bot
green is not a substitute, and the skill cannot self-authorize on the user's
behalf. It's a model-based judgment, not a deterministic switch, so treat a
genuine, explicit user go-ahead as what *should* let the merge through; §5 covers
the case where it's denied anyway.

This collides head-on with how the skill runs: the user hands you the PR and walks
away. If you only learn about the block when you try to merge, it's too late — the
user is gone, the "just confirm and I'll merge" offer has nobody to answer it, and
the PR sits green-but-unmerged. So don't leave it to the end. Read the human-review
posture from your first `pr-state.sh` snapshot (§1) and the session itself, and
settle it before you start the unattended wait — before you arm the monitor, or on
your first `/loop` cycle:

- **A human already approved** (`reviewDecision: APPROVED`): the safety check is
  satisfied — go straight into the loop and merge when green. One caveat: an
  `APPROVED` that came from a *bot* rather than a person doesn't satisfy the
  human-review posture; if the only approver is a bot, treat it as no human
  approval and front-load anyway.
- **The user's invocation already authorized it** ("just land it, no review
  needed", "merge without waiting for a review"): you have it on record — proceed,
  don't re-ask.
- **No human approval** (`reviewDecision` anything other than `APPROVED` — empty,
  `null`, `REVIEW_REQUIRED`, or `CHANGES_REQUESTED`), the common case and
  near-certain for a PR you opened this session: the terminal merge will be
  human-unreviewed, so get an explicit go-ahead *now*, while the user is still
  here. Say it plainly, and be straight about the trade-off — e.g. "PR #N has no
  human review, just CI and the reviewer bot. Once those are green I'll
  squash-merge it without waiting for a human — confirm you want that, or tell me to
  hold for a human approver (or for you to click merge yourself). Heads up: this
  go-ahead is what keeps the run hands-off; if the harness still gates the merge
  once it's ready, the PR waits green-but-unmerged until you're back rather than
  landing on its own." That's a call the user should make knowingly — and it's also
  what the classifier looks for later. Once they okay it, **restate the
  authorization in your own turn, scoped to the PR** — e.g. "Recorded: user
  authorized squash-merging PR #N without human review" — so the session carries an
  unambiguous authorization the classifier can key on, not a bare "yes". (Strongest
  of all: the user can re-invoke with the authorization in the command, e.g.
  `/ship-pr N merge without human review`, which puts it exactly where the denial
  reason pointed — the invocation.) Don't start the wait until it's settled.
- **User says hold for a human:** honor it. Do the CI and reviewer-bot work as
  usual; when the PR is otherwise green, stop at the merge and tell them it's ready
  and waiting on a human approver — the same shape as a human "changes requested"
  (see "Reviewer bots beyond CodeRabbit"). Don't merge around a hold.

Settling this one thing up front is what keeps the rest of the run genuinely
unattended.

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

Take one snapshot of the PR's health. The bundled helper does this in one shot —
`tee` it to a file as you read it, so if this assess sends you to Wait you can
seed the watcher from the *exact* state you classified on (this snapshot, taken
before you spend time classifying and arming, is what lets the watcher see a
transition that lands in that gap — see §4):

```bash
scripts/pr-state.sh <PR> | tee /tmp/ship-pr-<PR>.json
```

It prints a compact JSON with: each check's bucket, whether the non-bot checks
all pass, the reviewer-bot check state, counts of review threads and *unresolved*
review threads, `mergeable`, and `mergeStateStatus`. It also reports
`threads_fetched`: if a transient GraphQL hiccup makes the thread fetch fail, the
script reports `threads_fetched:false`, nulls the thread counts, and forces
`ready_to_merge:false` — so a stale read can never green-light a merge with
hidden unresolved threads. `ready_to_merge:false` with `threads_fetched:false`
means *retry the snapshot*, not *merge*. Read `references/gh-cookbook.md` for the
raw commands if you need detail or the script is unavailable.

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
("No actionable comments were generated…" or "Actionable comments posted: N");
`scripts/pr-comments.sh` extracts that verdict for you into its header line, so
you don't have to fetch and scan the summary comment by hand. See
`references/reviewer-bots.md` for what the verdict means.

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

Get the findings in triage-ready form — don't hand-fetch raw GraphQL/REST and
parse it yourself (that burns tokens on HTML comments, base64 state blobs, and
duplicated suggestion blocks the bots bury their point in):

```bash
scripts/pr-comments.sh <PR>            # cleaned Markdown of every unresolved finding
```

It prints, per finding: severity/type tags, `path:line`, the cleaned finding
text, one suggested-fix diff, and the `reply-to id` — plus a header tally
(unresolved/resolved counts, by-severity, and the bot's "Actionable comments
posted: N" / "No actionable comments" verdict). It strips the noise in the
script so you read only signal. Add `--all` to include resolved threads,
`--full` to keep every collapsible section, `--json` for structured output.

Then read the code each finding points at **in the worktree**
(`scripts/pr-worktree.sh ensure <PR>`, synced to the PR head) — so you judge the
PR's real current code, not the user's checkout. For each, make a judgment call:

- **Real issue** → fix it in the worktree. For a handful of small, clear
  findings, just fix them. For many findings, or subtle/cross-cutting ones, use
  `/workflows` to fan out analysis and adversarially verify before changing code
  — it's worth the tokens to avoid thrashing. After fixing: run the project's
  checks in the worktree, commit, and push the head
  (`git -C "$wt" push origin HEAD:<headRefName>`). The bot re-reviews the new
  commit and resolves the thread itself.
- **False positive / won't-fix** → **reply** to that specific review comment
  explaining why (concise, technical, points at the code). Use the `reply-to id`
  that `pr-comments.sh` printed for the finding and pipe the body in (never inline
  — an apostrophe or backtick in your reasoning breaks the shell):
  ```bash
  printf '%s' "@coderabbitai This is intentional: <code-grounded reason>." \
    | scripts/pr-reply.sh <PR> <reply-to id>
  ```
  It posts once, won't double-post on a transient EOF, and is a no-op if you
  already replied. Then wait for the bot to respond. Do **not** resolve the
  thread. CodeRabbit will either accept (and resolve) or push back; if it pushes
  back with a fair point, reconsider.

Batch your response: fix all the real ones in one commit/push where they're
related, and post replies to the false positives, then go wait. One push +
several replies is better than many tiny round-trips.

### 4. Wait (the part that needs patience)

You're waiting for an external event — CI finishing, or the bot posting/finishing
a review. Don't burn cycles polling by hand.

**Standalone (default):** arm a persistent background monitor that emits only
when something changes, then let it wake you. Seed it from the §1 snapshot you
classified on — pass that file's literal path (a shell `$var` from a prior command
won't survive into the monitor's separate process):

```text
Monitor(
  description: "PR <N>: CI + reviewer-bot state changes",
  persistent: true,
  command: PR_WATCH_SEED_FILE=/tmp/ship-pr-<N>.json scripts/pr-watch.sh <N>
)
```
`pr-watch.sh` polls every ~10s (override with `PR_WATCH_INTERVAL`) and prints a
line only when something meaningful changes: check buckets, unresolved-thread
count, review-comment count, head SHA, or merge state. It is **debounced** — it
carries the last stable merge state through GitHub's post-push `UNKNOWN`/`null`
recompute, treats a vanishing bot check as `pending`, and requires a state to
hold for two polls before emitting — so reviewer-bot churn and one-poll flashes
(including a transient `ready=true`) never wake you. You're notified on the real
*transition* (CI done, review posted, bot replied/resolved, ready to merge)
instead of on a timer.

`PR_WATCH_SEED_FILE` matters more than it looks. Without it the watcher takes a
*fresh* snapshot as its baseline at startup — but you read the PR back in §1 and
then spent the seconds since classifying and arming. If the bot finished or CI
flipped in that window, a fresh self-seed bakes the already-changed state in as
"normal" and the watcher sits silent until the fallback fires ~25 min later.
Seeding from the §1 snapshot — taken *before* that window — makes the gap-change
differ from the baseline, so it's emitted instead of swallowed. The seed must be
the state you classified on, **not** a fresh read taken at arm time: a read taken
now lands *after* the gap and seeds straight into the settled state, which is the
exact bug this guards against.

Belt and suspenders: **right after arming the watcher, assess once more.** The
seed closes the gap from the watcher's side; this fresh read closes it from yours,
independently — if either mechanism alone misfires, the other still catches it. If
the bot finished or CI flipped between your §1 assess and arming, you see it this
turn and loop straight back to Classify instead of waiting for a heartbeat to
notice. If that re-read still shows "bot pending / nothing changed", you're
genuinely waiting: let the monitor plus a long fallback heartbeat (e.g.
`ScheduleWakeup` ~1500s, in case an event is missed) wake you. When the terminal
condition is reached, `TaskStop` the monitor.

**Never `sleep N && <poll>`.** Chaining a sleep before a command is blocked by
the harness. To wait on a *state transition*, use the Monitor above; to wait on a
command you started, run it in the background. A bare post-push `merge=UNKNOWN`
or an empty bot bucket is expected recompute churn, not a stuck PR — the watcher
already absorbs it, so don't hand-roll *repeated* "real snapshot" confirmation
reads. (The single post-arm re-assess above is the one deliberate confirmation
read — it catches an assess→arm gap transition, not churn, so it doesn't loop.)

**Composed under /loop:** if the user wrapped this in `/loop`, don't arm your own
monitor — do a single assess→classify→act cycle and return. `/loop` re-invokes
you on its own cadence. (`/loop` with no interval self-paces and is a good fit.)
The "Merge authorization" step matters *more* here, not less: `/loop` detaches
from the user just like the standalone wait, so no later cycle can answer a
merge-time question either. Settle authorization on the very first cycle (or read
it from the invocation itself, e.g. `/ship-pr N merge without human review`). If it
isn't settled and the PR has no human approval, don't keep looping toward a merge
the harness will deny — surface that the run needs the user's go-ahead and stop.

### 5. Merge

Only when **all** of: every check `pass`, **0 unresolved** review threads,
`mergeable=MERGEABLE`, `mergeStateStatus=CLEAN` — i.e. `pr-state.sh` reports
`ready_to_merge:true` (which already requires `threads_fetched:true`, so a
degraded read can't green-light a merge).

Use the bundled helper — it re-confirms that gate, preserves the repo's DCO
sign-off, retries a transient `EOF`, and verifies the result so a flaky merge
call never double-merges or looks like a failure:

```bash
scripts/pr-merge.sh <N>          # squash by default; --strategy merge|rebase, --subject/--body to override
```

It refuses (exit 3) if the PR isn't ready, is a no-op if already merged, and
prints the merge commit. Match the repo's convention (most squash-merge repos
show single-line `type(scope): subject (#NNN)` history; the default subject is
`<PR title> (#N)`).

Two very different things can stop this merge — don't conflate them:

- **`pr-merge.sh` exits 3** — the *PR* isn't ready (not `CLEAN`, an unresolved
  thread, a degraded `threads_fetched:false` read). That's a code/state signal:
  loop back to Assess or Wait.
- **The harness denies the `gh pr merge` call** (an auto-accept "merge without
  review"-type permission denial) — the PR *is* ready; what's missing is the
  user's authorization to merge it without human review. If you settled that up
  front ("Merge authorization"), you most likely won't land here. If you do — the
  run wasn't front-loaded, or the go-ahead wasn't explicit enough for the
  classifier — treat it as the authorization gap it is, not a dead end. The PR is
  green and the *only* blocker is the user's go-ahead; but since they may be away,
  don't just end the turn with a dead pending ask — that's the strand again. Fire a
  `PushNotification` that the sole blocker is their explicit "merge without human
  review", leave that request as your last word so their reply resumes you, and
  re-attempt the gated merge the moment they authorize — so the PR still lands when
  they're back instead of stranding. (Under `/loop`, a fresh cycle fires on cadence,
  not on the user's reply: notify *once*, then on later cycles just report "ready —
  awaiting your go-ahead to merge without human review" rather than re-firing the
  notification or re-hammering the denied merge every cycle.) Landing it is your
  job, so don't demote it to a menu that offers "or merge it yourself." (And don't
  route around the denial with a raw `gh pr merge` or `--admin`; the classifier is
  a deliberate gate, and the fix is the user's word, not a workaround.)

Once the merge lands, tidy up — two halves:

- **The work area:** `scripts/pr-worktree.sh remove <PR>` if you created one, and
  `TaskStop` any monitor you armed.
- **The user's local checkout:** `scripts/pr-local-cleanup.sh <PR>`. All session
  long you kept your hands off the user's checkout and worked in the worktree;
  this is the one sanctioned moment to touch it, now that the PR is safely in. The
  script self-gates every step — it returns the user to the base branch,
  fast-forwards it, and deletes the now-merged local branch, but **only** when the
  tree is clean and it can *prove* the branch holds no unpushed work (it confirms
  the merge, force-deletes a squash-merged branch only after showing the local tip
  is contained in what GitHub merged, and refuses on a dirty/mid-rebase/detached
  tree, an unrelated branch, or a fork PR it can't match). On any of those it exits
  3 and changes nothing — read its one-line reason and pass it along; exit 3 is
  "left as-is", not a failure. Don't hand-roll `git checkout main && git pull &&
  git branch -D` yourself — that skips the proof-of-merge check and can drop
  unpushed commits.

Then report: merge commit, what was fixed, what was replied to, and what the local
cleanup did (or why it skipped). Don't schedule further work — the goal is done.

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

All bundled scripts retry transient GitHub API failures (`EOF`/5xx) internally,
so prefer them over hand-issued `gh api` — you won't have to babysit a flaky read.

- `scripts/pr-state.sh <PR> [OWNER/REPO]` — one-shot JSON snapshot of CI +
  review + merge state, incl. `threads_fetched` and `ready_to_merge`. Use it for
  every Assess. The repo arg is optional when you run inside the target repo.
- `scripts/pr-comments.sh <PR> [OWNER/REPO] [--all|--full|--json|--no-summary]` —
  fetch review threads + the reviewer-bot summary and print **cleaned,
  triage-ready Markdown**: per-finding severity/type tags, `path:line`, the
  finding text with HTML comments / base64 / duplicated suggestion blocks
  stripped, one fix diff, the reply-to id, and a header tally with the bot's
  actionable verdict. The moment a bot posts findings or `unresolved>0`, run this
  **first** — don't re-fetch raw `gh api graphql` or grep the summary by hand.
- `scripts/pr-reply.sh <PR> <reply-to id> [OWNER/REPO]` — post an in-thread reply
  (body from stdin or `--body-file`, never inline) to dispute a false positive;
  idempotent, retries transient EOF without double-posting.
- `scripts/pr-merge.sh <PR> [OWNER/REPO] [--subject|--body|--strategy]` —
  gated, DCO-preserving, idempotent, verify-after merge. Refuses unless ready.
- `scripts/pr-watch.sh <PR> [OWNER/REPO]` — **debounced** change-emitting poll
  loop for the `Monitor` tool (absorbs merge-recompute / bot-edit churn). Set
  `PR_WATCH_SEED_FILE` to your pre-arm `pr-state.sh` snapshot so a transition in
  the assess→arm gap is emitted, not swallowed into a fresh self-seed.
- `scripts/pr-worktree.sh ensure|remove|path <PR> [OWNER/REPO]` — create / refresh
  / tear down an isolated detached worktree (or clone) synced to the PR head, so
  triage and fixes never touch the user's working tree.
- `scripts/pr-local-cleanup.sh <PR> [OWNER/REPO] [--base B] [--dry-run]` —
  **post-merge only.** Return the user's *local* checkout to a clean base: switch
  off the merged branch to base, fast-forward it, and delete the merged local
  branch — each step gated (confirmed `MERGED`, clean tree, no in-progress git op,
  branch proven fully merged before a force-delete). Exits 3 and does nothing if
  any gate fails. pr-merge.sh is remote-only by design, so this is what tidies the
  local side.
- `scripts/gh-retry.sh` — `gh_retry <cmd…>` wrapper the other scripts source to
  retry transient GitHub API failures; rarely called directly.
- `references/gh-cookbook.md` — exact `gh` / `gh api` / GraphQL commands for
  reading checks, reading and replying to review threads, re-running jobs, and
  merging.
- `references/reviewer-bots.md` — how to read CodeRabbit's summary and comments,
  what "resolved" means, and how to generalize to other reviewers.
