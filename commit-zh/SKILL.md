---
name: commit-zh
description: Generate a Git commit message following Conventional Commits, with an English subject line and a Chinese (中文) body, then commit it. Manual-invocation only — run it explicitly with /commit-zh (Claude Code) or $commit-zh (Codex). Stages everything for you when nothing is staged yet, and when you're on a protected branch (main/master) it carves off a new branch before committing so your work never lands directly on main. Commits directly by default; pass -i / --interactive (or say "让我确认" / "let me confirm") to review and choose before it commits.
disable-model-invocation: true
argument-hint: "[-i | --interactive]"
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), Bash(*commit-helper.sh*), AskUserQuestion, request_user_input
---

# Commit ZH

Write a [Conventional Commits](https://www.conventionalcommits.org/) message — an **English subject** and a **Chinese (中文) body** — for the change, then commit it through the bundled helper. The helper owns the fiddly git plumbing so you don't chain it by hand: if nothing is staged yet it stages everything (`git add -A`), and if you're sitting on a protected branch (main/master) it creates and switches to a new branch first, so your work never lands directly on main. **Two modes:** by default, show the message and commit it directly — running the skill is your go-ahead; if the user opts into interactive mode (`-i` / `--interactive`, or "让我确认" / "let me confirm"), show the message — and the branch it would create — and ask before committing.

> Throughout, `<skill-dir>` is this skill's base directory (the path shown to you when the skill loaded, e.g. `…/commit-zh`). Substitute the real absolute path when you run a command.

## Git Context

**Repository:** !`git rev-parse --is-inside-work-tree 2>/dev/null || echo "NOT_A_GIT_REPO"`
**Branch:** !`git branch --show-current 2>/dev/null`
**Status (before staging):**
!`git status --short 2>/dev/null`

**Recent commits (style reference):**
!`git log --oneline -5 2>/dev/null`

## What keeps this safe

Running `/commit-zh` is itself the decision to commit, so the skill stages, branches, and commits without stopping to rubber-stamp each step. Two of those are new powers — staging on your behalf and creating a branch — so it's worth being precise about why they're safe:

- **Auto-staging is reversible and local.** When nothing is staged, the helper runs `git add -A` — exactly the `git add .` you'd otherwise type. It only ever *adds*; it never discards or overwrites working-tree content, and nothing leaves your machine. A staging you didn't want is one `git reset` away.
- **Auto-branching protects main instead of risking it.** On a protected branch (main/master) the helper creates a new branch from the current commit *before* committing, so the protected branch is never moved. Carving a branch off the current commit keeps your staged and unstaged changes byte-for-byte intact — there's no stash, no merge, nothing to lose. (This is the one people worry about, but `git switch -c` touches neither the index nor the working tree; it only points a new ref at the commit you're already on.)
- **Exactly one new commit, nothing destructive.** The skill never pushes, never rewrites or amends existing commits, never force-anything. It makes one new commit — and at most one new branch — from your changes. A message you don't like is one `git commit --amend` away from fixed.
- **The full message is always shown before the commit lands** — even on the default direct path. Nothing is hidden behind a summary, and there's always a record to amend from.

Want to review or pick before it lands? Add `-i` / `--interactive` (or just say "让我确认" / "let me confirm"), and the skill shows the message — and the branch it would create — and asks first. `-y` / `--yes` is also accepted and simply affirms the default.

## 1. Prepare the working tree

Run the bundled helper to stage (when needed) and read back a compact, token-lean snapshot. It does the staging so you can describe a complete diff even when the user staged nothing:

```bash
bash <skill-dir>/scripts/commit-helper.sh prepare
```

It prints one `STATE` line, then the staged stat and the staged diff. Read the `STATE` line and stop early when there's nothing to do:

- `STATE repo=no` → say "Not a git repository." and stop.
- `NO_CHANGES` (working tree clean) → say "工作区没有任何改动，无需提交。" and stop.

Otherwise note these fields — they drive the rest of the run:

- `auto_staged=yes` → the helper just staged everything for you; mention it when you report the result.
- `protected=yes` together with `unborn=no` → you're on main/master with real history, so the commit step will carve off a **new branch**; you'll name it in step 7.
- `protected=no` → you're already on a feature branch (or protection is disabled); no new branch will be created.

The `STATE` line reflects the tree *after* staging, so `staged_files` is the real count you're about to commit.

## 2. Understand the change

- The **Staged stat** from the prepare output maps *what* changed at the file level — it stays complete even when the diff below is truncated.
- The **Staged diff** shows the actual edits and their intent. If it was truncated (the header says "first N of M lines"), rely on the stat for coverage and run `git diff --staged -- <path>` to read any file you still need in full. Pull only what you need to describe the change accurately.

Work out *why* the change was made, not just which lines moved — the body depends on it.

## 3. Choose the type

| Type | When to use |
|------|-------------|
| `feat` | New functionality visible to users |
| `fix` | Correcting incorrect behavior |
| `docs` | Only comments/docs changed |
| `style` | Whitespace/formatting only, no logic change |
| `refactor` | Code restructure, no behavior change |
| `perf` | Measurable performance improvement |
| `test` | Adding or modifying tests |
| `build` | Build config or dependencies |
| `ci` | CI pipeline changes |
| `chore` | Miscellaneous maintenance tasks |
| `revert` | Reverting a previous commit (cite the reverted SHA or subject in the body) |

## 4. Infer the scope

Pick the narrowest scope that honestly describes the change, in priority order:

1. **Branch name** — `feature/auth-login` → `auth`
2. **Single directory** — everything under `src/auth/` → `auth`
3. **Single module/component** — only `Button.tsx` → `button`
4. **Several related areas** — comma-separated `auth,api` (a non-standard extension; some CI linters reject it)
5. **Widespread** — omit the scope

Match the scope naming style in **Recent commits** so the history stays consistent.

## 5. Write the subject (≤ 68 characters)

`type(scope): description` — one space after the colon.

- **The subject description is always English**, regardless of the language the user writes in
- Imperative mood: "add", "fix", "refactor" — not "added", "fixes", "refactored"
- Specific over vague: "add JWT token refresh endpoint", not "update auth"
- Lowercase the first letter of the description; no trailing period
- Keep the whole line ≤ 68 characters so it survives `git log` and GitHub truncation

## 6. Write the body (Chinese 中文)

The diff already records *what* changed; the body's lasting value is *why*. Include one whenever there's a reason, context, or trade-off worth recording — which is almost every change:

- Blank line after the subject, then prose **in Chinese (中文)** explaining the *why* — not a restated changelog
- Wrap lines at ≤ 72 characters
- Keep it proportional to the change: a subtle fix earns a short paragraph; a one-line refactor earns one sentence

**Wrap code in backticks.** Set off function and method calls, macros, and expressions — `isolate->SetWasmStreamingCallback()`, `WebAssembly.compileStreaming()`, `CHECK(!impl.IsEmpty())`. It pays off most for tokens carrying punctuation like `()`, `->`, or `!`, which read awkwardly bare and leave the reader guessing where the symbol ends; plain filenames, flags, and ordinary identifiers are a judgment call — backtick them only when it genuinely aids reading, not by reflex. Backticks read especially well in a Chinese body, where a bare `foo()` would otherwise blur into the surrounding 中文. The commit is written through a single-quoted heredoc (step 9), so backticks, `$`, `!`, and backslashes reach the message literally — write them as-is and never escape them.

**Break a multi-part why into paragraphs.** When the reasoning has distinct beats, separate them with a blank line instead of packing everything into one block. A reliable shape is 起因 → 影响 → 修复 (cause → effect → fix): what changed upstream, what broke as a result, and what this commit does about it. The blank lines are what let a reader follow the thread.

**When a body would add nothing, omit it.** Some changes are their own explanation — a pure typo fix, a version bump, a lockfile refresh. Forcing a "why" paragraph onto them produces filler that future readers have to wade through. Prefer no body over a padded one. But if you can name any non-obvious reason, context, or risk, include it — when in doubt, write the body.

**Breaking changes:** when the change breaks an existing API or behavior, signal it both ways the spec allows — a `!` before the colon (`feat(api)!: …`) *and* a `BREAKING CHANGE: <what breaks + how to migrate>` footer after a blank line. Keep the `BREAKING CHANGE:` keyword in English (it's a spec token); the description after it may be Chinese. This is the major-version signal; omitting it makes the break silent.

**Issue references:** add a `Closes #123` / `Refs #123` footer only when the diff or branch clearly ties to that issue — don't invent one.

## 7. Name the branch (only when `protected=yes` and `unborn=no`)

When prepare reported a protected branch, the commit lands on a *new* branch carved from the current commit. Name it after the commit you just wrote, so the branch reads like its contents:

`<type>/<short-kebab-summary>` — e.g. `feat/jwt-token-refresh`, `fix/user-null-pointer`. Fold the scope in when it sharpens the name (`feat/auth-jwt-refresh`). Keep it lowercase, hyphenated, and short (≈40 chars). If that name is already taken the helper appends `-2`, `-3`, … so you never have to check first.

On a feature branch (`protected=no`) no branch is created — pass any slug (it's ignored) or an empty string `""`.

## 8. Show the message, then commit (or ask, if requested)

**Always print the complete message first** — in the current reply, verbatim, in a fenced block. This applies in *both* modes:

````markdown
```text
<complete commit message>
```
````

When a branch will be created (`protected=yes`), add one line under the block naming it, e.g. `将从 main 创建并切换到分支 feat/jwt-token-refresh`. Then branch on the mode:

### Default — commit directly
Running the skill is the go-ahead, so don't ask. After the block is printed, go straight to step 9. The default path skips *only* the prompt — every other safeguard holds: prepare in step 1 still stops on a no-repo or no-changes state, and every mutation still flows through the audited helper.

### Interactive mode — `-i` / `--interactive`, or "让我确认" / "让我看看" / "let me confirm"
The user wants to decide, so after the block is visible, ask with the tool your environment provides — `AskUserQuestion` (Claude Code) or `request_user_input` (Codex):
- `header`: `提交确认`
- `question`: `请确认上方的 commit message`（若会创建分支，追加 `，并将提交到新分支 <name>`）`，是否执行提交？`
- `options`:
  1. `执行提交 (Recommended)` — run the commit helper with the message above
  2. `否` — stop without committing
  3. `修改` — revise, then confirm again

Never call the confirmation tool before the block appears, and never swap the full message for a summary or rationale. Then wait for the explicit choice:

- **执行提交** → step 9
- **否** → stop, don't commit
- **修改** → fold in the feedback and return to step 8

## 9. Commit

Pipe the message into the helper's `commit` subcommand, passing the branch name from step 7 (or `""` on a feature branch). The single-quoted heredoc keeps backticks, `$`, `!`, and backslashes literal:

```bash
bash <skill-dir>/scripts/commit-helper.sh commit "feat/jwt-token-refresh" <<'EOF'
<commit message>
EOF
```

The helper creates the branch when on a protected branch, commits with `-s` (sign-off), and prints a `COMMITTED` line such as `COMMITTED branch=feat/jwt-token-refresh created_branch=feat/jwt-token-refresh sha=1a2b3c4`. Report what landed **in Chinese**:

- the subject line and the new short SHA (`sha=`)
- when `created_branch=` is present → 已从原分支创建并切换到分支 `<name>`
- when step 1 showed `auto_staged=yes` → 已自动暂存全部改动（相当于 `git add .`）

## Examples

```
feat(auth): add user login

实现了基于 JWT 的用户认证流程。选择 httpOnly cookie 存储
refresh token 以避免 XSS 攻击风险。
```

```
fix(api): fix null pointer exception in user query

之前代码假设 `user` 对象始终存在，但在未登录状态下会导致
崩溃。通过提前判空并返回统一错误格式修复此问题。
```

```
refactor(db): extract query builder into standalone module

查询构建代码分散在 4 个 repository 文件中导致重复维护。
集中管理后更易于后续添加查询缓存机制。
```

A multi-part *why* reads best as separate paragraphs, with code set off in
backticks (subject English, body 中文):

```
fix(pool): release connection on context cancel

当调用方在查询中途取消了请求 context 时，`Pool.Acquire()` 已经返回，
但其后的 `conn.Release()` 始终没有执行——取消在 `defer` 注册之前就
展开了调用栈。

在持续的取消请求冲击下，连接池每次都会泄漏一个连接，最终因为
`MaxConns` 个连接全部借出且无一归还，所有调用方都阻塞在
`Acquire()` 上。表面看像死锁，实际是连接耗尽。

在成功获取连接后、任何可能感知取消的逻辑之前，立即用 `defer`
注册释放，使连接在每条路径上都能回到池中。
```

A self-explanatory change needs no body (subject stays English):

```
chore: bump version to 2.3.1
```

```
feat(api)!: replace positional args in createUser with options object

createUser 的位置参数已增长到五个，调用方很容易传错顺序。
改用选项对象后，每个字段在调用处都显式可读。

BREAKING CHANGE: `createUser(name, email, role)` 现在改为
`createUser({ name, email, role })`，旧的位置参数调用需要迁移。
```
