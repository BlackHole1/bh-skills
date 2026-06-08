# Reviewer bots

How to read automated reviewers so you triage correctly. CodeRabbit is the
primary case; the last section generalizes.

## CodeRabbit

**Status check.** A check named `CodeRabbit` flips `pending` → `pass` when a
review round finishes. `pending` with everything else green means *keep waiting*,
not *ready*.

**Summary comment.** CodeRabbit posts (and updates) one PR-level *issue* comment.
Read it to know whether there's anything to do — look for either:
- `No actionable comments were generated in the recent review.` → nothing to fix;
  the review is clear.
- `Actionable comments posted: N` → there are N line-level review comments to
  triage.
It also lists files reviewed and files skipped by path filters (e.g. `*.snap`),
which explains why a changed file may not have been commented on.

**Actionable findings** arrive as line-level *review comments* (the REST
`pulls/{pr}/comments` list / GraphQL review threads). Each often includes a
committable suggestion and a collapsible AI prompt. Treat each as a thread.

**Resolution.** CodeRabbit resolves a thread itself once it sees the concern
addressed — either by a new commit that fixes it, or by a reply it accepts. So:
- Real issue → fix, push. The incremental re-review picks up the new commit and
  resolves the thread.
- False positive → reply in-thread starting with `@coderabbitai` and a concise,
  code-grounded reason. It will respond; if it agrees it resolves, if not it
  pushes back (reconsider — it's sometimes right).
Never resolve on its behalf: an unresolved thread is the only honest signal that
a concern is still open.

**Re-review cadence.** Pushing a commit triggers an incremental review (seconds
to a few minutes). You can also comment `@coderabbitai review` to force one, or
`@coderabbitai resolve` to ask it to resolve threads it considers done — but
prefer letting it resolve naturally.

**Profiles.** A `CHILL` profile raises fewer nits than `ASSERTIVE`; don't assume
silence means it didn't look — confirm via the summary comment.

## Generalizing to other reviewers

The control loop is reviewer-agnostic: **any unresolved review thread or a
`reviewDecision` of `CHANGES_REQUESTED` is blocking.** Fix or reply, then let the
reviewer resolve.

- **Other bots** (Sourcery, Qodo/Codium, Greptile, Ellipsis, …) behave like
  CodeRabbit: a status check, line comments, self-resolution. Add their check
  name to the `botre` pattern in `scripts/pr-state.sh` so they're classed as
  review rather than CI.
- **Human reviewers** don't resolve on a timer. After you push a fix, leave a
  short reply noting what changed, then *don't block forever waiting* — tell the
  user a human re-review is pending and let them decide. Auto-merging past a
  human's requested changes is not your call.

## Don't confuse review checks with CI

`scripts/pr-state.sh` splits checks into `review_bot_checks` and CI. The merge
gate needs both green, but they fail for different reasons: a red CI job is a code
problem you fix; a red/pending bot check is a review you respond to. Keep them
distinct when you classify.
