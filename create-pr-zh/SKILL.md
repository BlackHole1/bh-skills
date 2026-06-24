---
name: create-pr-zh
description: Open a GitHub Pull Request for the current branch — an English Conventional-Commits title and a Markdown body written entirely in Chinese (中文). If a PR already exists for the branch, update its title/body instead of opening a new one. Manual-invocation only — run it explicitly with /create-pr-zh (Claude Code) or $create-pr-zh (Codex). It reads the branch's commits (and the diff when the commits are too thin to explain the change) to draft the body. Creates/updates directly by default; pass -i / --interactive (or "让我确认") to review first, --draft to open as a draft.
disable-model-invocation: true
argument-hint: "[-i | --interactive] [--draft] [base-branch]"
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), Bash(git branch *), Bash(git rev-parse *), Bash(git merge-base *), Bash(git rev-list *), Bash(git push -u origin HEAD), Bash(git push origin HEAD), Bash(gh pr view *), Bash(gh pr create *), Bash(gh pr edit *), Bash(gh pr diff *), Bash(gh repo view *), AskUserQuestion, request_user_input
---

# Create PR (Chinese 中文)

Open — or, if one already exists, update — a GitHub Pull Request for the current branch. The **title** is always an English [Conventional Commits](https://www.conventionalcommits.org/) line; the **body** is **Chinese (中文)** Markdown. The diff records *what* changed; a good PR body explains *why* and orients a reviewer, so the lasting work here is reading the branch's commits (and the code when the commits are thin) and turning them into a description someone can review against.

**Two modes:** by default, draft the title and body and apply them directly — running the skill is your go-ahead; if the user opts into interactive mode (`-i` / `--interactive`, or "让我确认" / "let me confirm"), show the title and body and ask before applying.

## Branch & PR context

**Repository:** !`git rev-parse --is-inside-work-tree 2>/dev/null || echo "NOT_A_GIT_REPO"`
**Current branch:** !`git branch --show-current 2>/dev/null | grep . || echo "DETACHED_HEAD"`
**Default base branch:** !`gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main"`
**Branch pushed to remote?** !`git rev-parse --abbrev-ref --symbolic-full-name @{push} 2>/dev/null || echo "NO_UPSTREAM"`
**Commits ahead of remote** (these are the unpushed local commits — non-zero means §7 must push before the body can describe them): !`git rev-list --count @{push}..HEAD 2>/dev/null || echo "n/a"`

**Existing PR for this branch** (`NONE` means create a new one; otherwise read its `state` — see §2):
!`gh pr view --json number,state,isDraft,title,url,baseRefName,body 2>/dev/null || echo "NONE"`

**Commits on this branch** (oldest first — subjects and bodies; this is your primary source):
!`b=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null||echo main); mb=$(git merge-base HEAD "origin/$b" 2>/dev/null||git merge-base HEAD "$b" 2>/dev/null); base=${mb:-$(git rev-list --max-parents=0 HEAD 2>/dev/null|tail -1)}; rng="${base:+$base..}HEAD"; git log --reverse --format='─── %h %s%n%b' "$rng" 2>/dev/null`

**Files changed** (diffstat — stays complete even when the diff below is truncated):
!`b=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null||echo main); mb=$(git merge-base HEAD "origin/$b" 2>/dev/null||git merge-base HEAD "$b" 2>/dev/null); base=${mb:-$(git rev-list --max-parents=0 HEAD 2>/dev/null|tail -1)}; rng="${base:+$base..}HEAD"; git diff --stat "$rng" 2>/dev/null`

**Branch diff** — first 600 of !`b=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null||echo main); mb=$(git merge-base HEAD "origin/$b" 2>/dev/null||git merge-base HEAD "$b" 2>/dev/null); base=${mb:-$(git rev-list --max-parents=0 HEAD 2>/dev/null|tail -1)}; rng="${base:+$base..}HEAD"; git diff "$rng" 2>/dev/null | wc -l | tr -d ' '` lines:
!`b=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null||echo main); mb=$(git merge-base HEAD "origin/$b" 2>/dev/null||git merge-base HEAD "$b" 2>/dev/null); base=${mb:-$(git rev-list --max-parents=0 HEAD 2>/dev/null|tail -1)}; rng="${base:+$base..}HEAD"; git diff "$rng" 2>/dev/null | head -600`

**PR template** (if the repo defines one — its sections are required; honor it, see §5/§7a):
!`tmpl=$(for f in .github/PULL_REQUEST_TEMPLATE.md .github/pull_request_template.md PULL_REQUEST_TEMPLATE.md pull_request_template.md docs/PULL_REQUEST_TEMPLATE.md docs/pull_request_template.md; do [ -f "$f" ] && echo "$f" && break; done); [ -z "$tmpl" ] && tmpl=$(find .github/PULL_REQUEST_TEMPLATE -name '*.md' -type f 2>/dev/null | head -1); if [ -n "$tmpl" ]; then echo "--- $tmpl ---"; cat "$tmpl"; else echo "NONE"; fi`

## What keeps this safe

Running `/create-pr-zh` is itself the decision to publish (or refresh) the PR, so by default the skill drafts the title and body and applies them without a rubber-stamp prompt. What keeps that safe is non-negotiable:

- **Only the current branch, only its PR.** The skill touches exactly one thing — the PR for the branch you're on. It never opens or edits another PR, never switches branches, and never merges.
- **It writes only PR metadata, plus the branch push a PR requires.** A PR can't exist on GitHub without its branch on the remote, and a refreshed body shouldn't describe commits the remote doesn't have — so when the remote is behind local `HEAD`, the skill may run a plain `git push` of the **current branch only** (`git push -u origin HEAD` / `git push origin HEAD` — never `--force`, never another branch). It never rewrites history and never commits.
- **Always show the complete title and body before applying — even on the default direct path.** Nothing is hidden behind a summary, and `gh pr edit` can revise the result at any time, so a description you don't like is one re-run (or one edit) away from fixed.

Want to review or adjust before it lands? Add `-i` / `--interactive` (or just say "让我确认" / "让我看看" / "let me confirm"), and the skill shows the title and body and asks first. `-y` / `--yes` is also accepted and simply affirms the default. `--draft` opens a *new* PR as a draft (it applies only when creating; on an existing PR it has no effect — use `gh pr ready` / `gh pr ready --undo` to flip draft state).

## 1. Validate the environment

Read the injected context and stop early when there's nothing to open a PR for:

- Repository shows `NOT_A_GIT_REPO` → say "Not a git repository." and stop.
- **Current branch is `DETACHED_HEAD`** (detached HEAD, no branch) → say "You're in a detached HEAD state with no branch. Check out or create a branch before opening a PR." and stop — `git push -u origin HEAD` can't set an upstream for a detached HEAD, and there's no branch to open a PR from.
- **Current branch is the default base branch** (e.g. you're on `main`) → say "You're on the base branch (`<branch>`). Switch to a feature branch before opening a PR." and stop — a branch can't open a PR against itself.
- **No commits ahead of the base** (the commit list above is empty) → say "No commits on this branch beyond `<base>`. Commit your work first." and stop. There's nothing to describe.
- `gh` not authenticated (a `gh` call complains about auth) → say "GitHub CLI isn't authenticated. Run `gh auth login`." and stop.

If the **base looks wrong** — the commit list reaches back further than this branch's real work, the diffstat includes files you didn't touch, or the commit/diff blocks are empty even though you're on a feature branch (the base may not have resolved) — the auto-detected base is off. Recompute the merge-base against the intended base (`git merge-base HEAD origin/<base>`), or take an explicit `[base-branch]` argument from the user. Then **re-run the source commands yourself** for the corrected base — `git log --reverse <base>..HEAD`, `git diff <base>..HEAD` — and reason from *that* output, not the injected blocks, which were computed against the wrong base.

## 2. Decide the mode: create or update

Look at **Existing PR for this branch** and read its `state` — don't route on the presence of a `number` alone, because `gh pr view` returns a branch's PR even when it's closed or merged:

- `NONE` → **create mode** (§7a). Open a new PR.
- `state: "OPEN"` → **update mode** (§7b). A PR already exists, so running this skill means "refresh it." Note its current `title` and `body` — you'll show what changes.
- `state: "MERGED"` → **stop.** Say "This branch's PR #`<n>` is already merged — there's nothing to update." Editing a landed PR's description is pointless; don't.
- `state: "CLOSED"` (not merged) → **create mode** (§7a), but say first: "PR #`<n>` for this branch is closed; opening a new one." `gh pr create` opens a fresh PR and leaves the closed one untouched.

## 3. Understand the change

A PR body is only as good as your understanding of the branch, so read the branch as a whole, not commit-by-commit in isolation:

- **Commits** are the primary source — they already carry the author's intent and a `type(scope)` you can lift the title from. Read every subject *and* body. When the subjects and bodies already explain the *why*, synthesize the body from them and use the diffstat to confirm the surface area — you don't need a line-by-line diff re-read.
- **When the commits are thin** — terse subjects like `wip` / `fix` / `address review`, empty bodies, or a single squashed commit that hides a large diff — they won't explain the change on their own. Fall back to the code: the **diffstat** maps the surface area, and the **branch diff** shows the actual edits. If the diff was truncated at 600 lines, read what you still need with `git diff <base>..HEAD -- <path>` (or `gh pr diff <number>` in update mode).
- **Synthesize across commits.** A PR usually bundles several commits into one reviewable unit; describe the *net* change the branch delivers, not a replay of each commit in order.

Work out *why* the branch exists and *what a reviewer needs to know to approve it* — that, not a file-by-file recap, is what the body is for.

## 4. Write the title (English, ≤ 68 characters)

`type(scope): description` — the same [Conventional Commits](https://www.conventionalcommits.org/) shape as a commit subject, because in a squash-merge repo the PR title becomes the squash commit's subject line.

- **Always English**, regardless of the language the user writes in. Only the **body** is Chinese — the title stays English, mirroring `commit-zh`.
- **Type** — derive it from the branch's net intent, not just the first commit. When commits mix types, pick the one that captures the headline change (a `feat` among supporting `fix`/`refactor`/`test` commits → `feat`; a pure bug-fix branch → `fix`). Use `!` before the colon (`feat(api)!:`) when the branch breaks an existing API — a single-line title can't carry a `BREAKING CHANGE:` footer, so the `!` is how a break is flagged in the title.
- **Scope** — the narrowest honest area: a branch name like `feature/auth-login` → `auth`; everything under `src/auth/` → `auth`; several areas → omit it rather than cram. Match the scope style in the branch's own commits.
- **Description** — imperative mood, specific over vague ("add JWT refresh endpoint", not "update auth"), lowercase first letter, no trailing period. Keep the whole line ≤ 68 characters so it survives the GitHub PR list and `git log` after squash (the same budget `commit-zh` uses for the subject).
- **Single-commit branch:** GitHub would default the title to that commit's subject — reuse it (refined) rather than inventing a different one.
- **Update mode:** keep the existing title unless the branch's scope has genuinely shifted since it was set; the title is the squash subject and may have been chosen deliberately. If you do change it, call that out when you show the result.

## 5. Write the body (Chinese 中文 Markdown)

Write the body the way a human engineer describes a change to a teammate — not as a filled-in form — and write the prose **in Chinese (中文)**. A PR body is GitHub-flavored Markdown — headings, lists, code, tables, `<details>` are all available — but reach for that structure only when the change is big enough to need it.

**Don't hard-wrap the prose.** Unlike a commit body (which wraps at ~72 columns), a PR body has **no line-length limit** and GitHub soft-wraps it in the browser. Write each paragraph (中文 included) as one continuous line; use real line breaks only between paragraphs and list items. Folding a paragraph into many short lines mid-sentence is a commit-message habit that reads as machine-generated here — the examples below are written as single-line paragraphs on purpose.

**Default to prose, and lead with the point.** Most PRs are a short paragraph or two **in Chinese**: what the change is and — where the diff doesn't make it obvious — the problem or cause behind it and the approach you took. A reliable, human shape is *起因 → 解决 (cause → fix)*. Open with a clear standalone sentence (some repos fold the PR body into the squash-merge commit message, so an opening that reads well as a commit body keeps history readable). Add a short bullet list of the notable changes only if there are several worth separating out. For many PRs, two or three sentences is the entire body.

**Avoid the auto-generated look — this is the tell reviewers notice most.** A fixed `## Summary` / `## Why` / `## What changed` / `## Test plan` scaffold stamped onto every PR is the unmistakable mark of a machine-written description; humans almost never write their PRs that way. Don't put section headings on a body that fits on one screen, and don't label the reasoning with a `## Why` heading — weave the *why* into the sentences. Describe the cause and the solution in plain Chinese prose.

**Don't add a "Test plan" / "Testing"（测试）section by default.** Real PR descriptions rarely carry one. Mention testing only when there's something genuinely worth saying — a regression test you added for the exact bug, a risky change you verified against real data — and then say it in a sentence, inline, not under a heading. If there's nothing notable about how you tested, write nothing about it. (The default section template was never a verified best practice; a natural cause→fix narrative is.)

**Follow the repo's own conventions.** If a **PR template** was injected above (not `NONE`), use it as the skeleton — it's the team's chosen structure, checkboxes and required links included (see §7a); `gh pr create --body` does *not* auto-apply it, so honoring it is on you. Otherwise, if you can tell how the repo's recent merged PRs read, match that shape rather than imposing your own.

**When a PR is genuinely large** — many commits, cross-cutting, or risky — *then* a little structure earns its place: a one-line overview, a grouped list of the notable changes, and where they apply, the sections humans actually do write — a **screenshot**（截图）for a user-visible UI change, or a short **migration note**（迁移说明）for a breaking change (pair that with `!` in the title). If you do use headings, they may stay English (`## Summary`) or be Chinese (`## 概述`) — match the repo; the prose underneath is always Chinese. Leave a "test plan" out unless the template asks for it.

### Issue links (footer)

Add a closing keyword only when the branch or commits clearly tie to an issue — don't invent one. On merge, these **auto-close** the issue: `Closes #123`、`Fixes #123`、`Resolves #123`（GitHub 也接受 `close`/`closed`/`fix`/`fixed`/`resolve`/`resolved` 等变体）. To reference without closing, write `Refs #123` or `Related to #123`. Cross-repo: `owner/repo#123`. Keep these keywords in English — they're GitHub tokens — even in a Chinese body.

**Auto-close fires only when the PR's base is the repository's default branch.** If you're opening this PR against a non-default `[base-branch]`, a closing keyword still *links* the issue but won't close it on merge — so don't rely on `Closes #` there; use `Refs #` and note that the issue closes once the base eventually lands on the default branch.

### Keep it proportional

A one-line fix, a version bump, or a typo correction needs only a sentence — plus a `Closes #` if it resolves an issue. Don't pad it with headings; match the body's weight to the change's weight. Bigger, riskier changes earn more context (and, past a point, light structure); small ones don't.

### What to avoid

The failure modes are forms of making the reviewer do your work: an **empty body**; **"see commits"** / **"自解释"**; **restating the diff** file-by-file (they can read the diff — tell them what they can't see); a **wall of unstructured text**; and the **boilerplate `## Summary` / `## Test plan` scaffold** on a change that doesn't warrant it — it reads as auto-generated. If a line would only restate what the diff already shows, cut it.

### Markdown mechanics worth using

- **Code in backticks**, code blocks in fenced ``` blocks with a language tag. Backticks read especially well in a Chinese body, where a bare `foo()` would otherwise blur into the surrounding 中文. A single-quoted heredoc (§7) passes backticks, `$`, `!`, and `#` through literally — write them as-is, never escape them.
- **`<details><summary>…</summary>`** to fold long logs, generated output, or a big optional diff so the body stays scannable.
- **Task lists** `- [ ]` for follow-ups or a reviewer checklist; GitHub renders and tracks them.

## 6. Show the title and body, then apply (or ask, if requested)

**Always print the complete title and body first** — in the current reply, verbatim, in a fenced block, so what's about to be published is fully visible. This applies in *both* modes:

````markdown
**Title:** `<title>`

```markdown
<complete PR body>
```
````

In **update mode**, also show — briefly — how this differs from the PR's current title/body (e.g. "标题不变；正文按最新提交重写为起因→解决的叙述"), so the change isn't silent.

Then branch on the mode:

### Default — apply directly
Running the skill is the go-ahead, so don't ask. After the block is printed, go to §7, then report what happened **in Chinese**: the PR URL and number, and whether you created or updated it. The default path skips *only* the prompt — every safeguard in §1 still holds.

### Interactive mode — `-i` / `--interactive`, or "让我确认" / "让我看看" / "let me confirm"
After the block is visible, ask with the tool your environment provides — `AskUserQuestion` (Claude Code) or `request_user_input` (Codex):
- `header`: `创建 PR`（更新场景用 `更新 PR`）
- `question`: `请确认上方的 PR 标题与正文，是否执行<创建 / 更新>？`
- `options`:
  1. `执行 (Recommended)` — apply the title and body above
  2. `否` — stop without touching the PR
  3. `修改` — revise, then confirm again

Never call the confirmation tool before the block appears, and never swap the full title/body for a summary. Then wait for the explicit choice: **执行** → §7; **否** → stop; **修改** → fold in the feedback and return to §6.

## 7. Apply

**First, make the remote branch match local `HEAD`** — in *both* modes, so the diff a reviewer sees matches the body you're about to write. This assumes the branch's remote is named `origin` (the default from `git clone` / `gh repo fork`); if the repo's sole remote has another name, push the current branch manually first (`git push -u <remote> HEAD`) and skip this step.

- **Branch pushed to remote?** is `NO_UPSTREAM` → `git push -u origin HEAD` (publishes the branch and sets upstream).
- Otherwise, if **Commits ahead of remote** is non-zero (local is ahead) → `git push origin HEAD` (a plain push of the current branch; never `--force`).
- Otherwise (remote already matches) → push nothing.

Then write the body through a **single-quoted heredoc** so Markdown — backticks, `#`, `$`, `!`, `[]()` — reaches GitHub literally, exactly as shown. Don't escape anything inside it.

### 7a. Create mode

Open the PR (add `--draft` if requested; `--base <base>` only when overriding the auto-detected default branch):

```bash
gh pr create --title "type(scope): description" --body "$(cat <<'EOF'
<complete PR body>
EOF
)"
```

### 7b. Update mode

`gh pr edit` **replaces** the body wholesale, so don't overwrite blind. The body in the injected context was captured at skill-load time and may be stale if a human or bot edited the PR on GitHub since. **Re-fetch the live body just before editing** and merge into *that*:

```bash
gh pr view <number> --json body -q .body   # the current live body — preserve hand-curated parts of it
gh pr edit <number> --body "$(cat <<'EOF'
<complete PR body>
EOF
)"
# add --title "type(scope): description" only if §4 decided the title genuinely changed
```

Refresh the narrative sections from the branch's current state, but **preserve hand-curated content** in the live body — a filled-in PR-template checklist, reviewer notes, a manually added `Closes #`. Regenerate what's derivable; keep what a human clearly added. When in doubt, prefer preserving over discarding, and say what you kept.

## Examples

A feature branch with several commits — written the way a person would: the problem, then the fix, then the notable changes. No `Summary`/`Test plan` scaffold. (Subject English, body 中文.)

```markdown
**Title:** `feat(auth): add JWT refresh-token rotation`
```

```markdown
之前 refresh token 是静态的，一旦从存储中被窃取，在 30 天过期前都能继续使用。现在每次使用都会轮换：`AuthService.refresh()` 每次调用都签发新 token 并在同一事务中作废旧的，把暴露窗口压缩到单次请求。重复使用已作废的 token 会使整个 token 家族失效、强制重新登录，同时也成了盗用信号——见 `detectReuse()`。

新增 `token_rotations` 表（迁移脚本 `0042_token_rotation.sql`）。

Closes #214
```

A tiny change — one sentence is the whole description:

```markdown
**Title:** `fix(api): guard null user in lookup handler`
```

```markdown
未登录请求会让 `getUser()` 崩溃，因为它假设 `user` 始终存在；补上判空和标准的 401 响应即可。

Closes #271
```

A breaking change — `!` in the title; the break and its migration in prose, no headings:

```markdown
**Title:** `feat(api)!: replace positional args in createUser with options object`
```

```markdown
`createUser` 此前的五个位置参数很容易传错顺序，现在改为接收单个选项对象。本仓库内所有调用点已更新；外部调用方需要迁移：

`createUser(name, email, role)` 改为 `createUser({ name, email, role })`
```
