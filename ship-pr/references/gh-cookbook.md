# gh cookbook for ship-pr

Exact commands for each operation. `OWNER`, `REPO`, and `PR` are placeholders;
derive the first two with `gh repo view --json owner,name` and pin `PR` once.

## Resolve the target

```bash
PR=$(gh pr view --json number -q .number)                 # current branch's PR
read OWNER REPO < <(gh repo view --json owner,name -q '.owner.login+" "+.name')
gh pr view "$PR" --json number,url,state,headRefName,baseRefName,isDraft
```

## Assess state

Prefer `scripts/pr_state.py "$PR"` for a one-shot JSON snapshot. Raw pieces:

```bash
gh pr checks "$PR" --json name,bucket,state          # bucket: pass|fail|pending|skipping|cancel
gh pr view "$PR" --json mergeable,mergeStateStatus,reviewDecision,state
```
- `mergeable`: `MERGEABLE` | `CONFLICTING` | `UNKNOWN`.
- `mergeStateStatus`: `CLEAN` (mergeable, checks done) | `BLOCKED` | `UNSTABLE`
  (a non-required/ pending check) | `DIRTY` (conflicts) | `BEHIND`.

## Read review threads (with bodies, authors, resolution, reply ids)

**Prefer the bundled script** — `scripts/pr_comments.py "$PR"` fetches these
threads *and* the bot summary, strips the noise (HTML comments, base64 state,
duplicated suggestion blocks), and prints cleaned Markdown with severity tags,
`path:line`, fix diffs, reply-to ids, and the actionable verdict. Reach for the
raw commands below only when you need a field the script doesn't surface.

Review threads live in the GraphQL API. This returns each thread's resolution
state plus every comment's `databaseId` (the REST id you reply to):

```bash
gh api graphql -F owner="$OWNER" -F repo="$REPO" -F pr="$PR" -f query='
query($owner:String!,$repo:String!,$pr:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      reviewThreads(first:100){nodes{
        isResolved isOutdated path line
        comments(first:50){nodes{ databaseId author{login} body }}
      }}
    }
  }
}'
```

The flat REST list of review (line) comments is handy too:

```bash
gh api "repos/$OWNER/$REPO/pulls/$PR/comments" \
  --jq '.[] | {id, user: .user.login, path, line, body}'
```

The reviewer bot's PR-level summary is an *issue* comment, not a review comment:

```bash
gh api "repos/$OWNER/$REPO/issues/$PR/comments" \
  --jq '.[] | select(.user.login|test("coderabbit";"i")) | .body'
```

## Reply to a review comment (dispute a false positive)

**Prefer `scripts/pr_reply.py "$PR" "$COMMENT_ID"`** with the body on stdin — it
avoids the shell-quoting hazards of an inline body (an apostrophe/backtick in the
reason breaks `-f body=`), is idempotent, and retries a transient EOF without
double-posting. Raw form (note `-F body=@-` reads stdin; never inline a body with
quotes):

```bash
printf '%s' "@coderabbitai This is intentional: <concise technical reason>." \
  | gh api "repos/$OWNER/$REPO/pulls/$PR/comments/$COMMENT_ID/replies" -F body=@-
```
`COMMENT_ID` = a `databaseId` from the GraphQL query above (or the `reply-to id`
that `scripts/pr_comments.py` prints).

Addressing CodeRabbit directly with `@coderabbitai` makes it respond and, if it
agrees, resolve the thread. For a general (non-thread) note use
`gh pr comment "$PR" --body "..."`.

## Do NOT resolve threads

Resolving is `resolveReviewThread` (GraphQL). This skill deliberately never calls
it — the reviewer resolves its own threads after seeing your fix or reply. Listed
here only so it's recognizable and explicitly avoided.

## Fix → push from the isolated worktree (re-triggers CI + re-review)

Never edit the user's checkout. Work in the worktree synced to the PR head:

```bash
wt=$(python3 <skill-dir>/scripts/pr_worktree.py ensure "$PR")   # detached worktree at the PR head
# edit files under "$wt", run the project's own checks THERE, then:
git -C "$wt" commit -s -am "fix(scope): address review — <what>"
git -C "$wt" push origin HEAD:<headRefName>    # same-repo PR; never --force
# fork PR — push to the fork instead (needs write access to it):
#   git -C "$wt" push https://github.com/<headOwner>/<headRepo>.git HEAD:<headRefName>
python3 <skill-dir>/scripts/pr_worktree.py remove "$PR"         # after the PR is merged
```

Resolve `<headRefName>` and whether it's a fork:

```bash
gh pr view "$PR" --json headRefName,isCrossRepository,headRepositoryOwner,headRepository
```

## Re-run a failing job (suspected flake)

```bash
gh run list --branch "$(gh pr view "$PR" --json headRefName -q .headRefName)" --limit 5
gh run rerun <run-id> --failed
```

## Merge

**Prefer `scripts/pr_merge.py "$PR"`** — it re-confirms the ready gate, preserves
the DCO sign-off, retries a transient EOF, runs remote-only (`-R`, so gh never
fast-forwards your local checkout), and verifies the result (idempotent: a no-op
if already merged, never a double-merge). Raw form:

```bash
gh pr merge "$PR" --squash --delete-branch \
  --subject "<PR title> (#$PR)" \
  --body "<short rationale; keep a Signed-off-by line if the repo uses DCO>"
```
Use `--merge` or `--rebase` instead of `--squash` only if that matches the repo's
history convention. Confirm the strategy is enabled if a merge call is rejected
(`gh repo view --json squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed`).
Verify with `gh pr view "$PR" --json state,mergedAt,mergeCommit` (not `merged` —
that field name varies by gh version).
