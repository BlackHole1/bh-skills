---
name: commit-zh
description: Generate a Git commit message following Conventional Commits, with an English subject line and a Chinese (中文) body. Manual-invocation only — run it explicitly with /commit-zh (Claude Code) or $commit-zh (Codex). Use it for committing staged changes when you want a Chinese commit body. Commits directly by default; pass -i / --interactive (or say "让我确认" / "let me confirm") to review and choose before it commits.
disable-model-invocation: true
argument-hint: "[-i | --interactive]"
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), Bash(git commit *), AskUserQuestion, request_user_input
---

# Commit ZH

Write a [Conventional Commits](https://www.conventionalcommits.org/) message — an **English subject** and a **Chinese (中文) body** — for the staged changes, then commit it. **Two modes:** by default, show the message and commit it directly — running the skill is your go-ahead; if the user opts into interactive mode (`-i` / `--interactive`, or "让我确认" / "let me confirm"), show the message and ask before committing.

## Git Context

**Repository:** !`git rev-parse --is-inside-work-tree 2>/dev/null || echo "NOT_A_GIT_REPO"`
**Branch:** !`git branch --show-current 2>/dev/null`
**Status:**
!`git status --short 2>/dev/null`

**Staged files (complete list):**
!`git diff --staged --stat 2>/dev/null`

**Recent commits (style reference):**
!`git log --oneline -5 2>/dev/null`

**Staged diff** — first 500 of !`git diff --staged 2>/dev/null | wc -l | tr -d ' '` lines:
!`git diff --staged 2>/dev/null | head -500`

## What keeps this safe

Running `/commit-zh` is itself the decision to commit, so by default the skill writes the message and commits it — no prompt to rubber-stamp. What keeps that safe is non-negotiable:

- **Never run `git add`.** Commit only what the user already staged; if nothing is staged, stop and say so — don't stage on their behalf.
- **Never push, and never rewrite existing commits.** The skill makes exactly one new commit from the staged changes and nothing else. A message you don't like is one `git commit --amend` away from fixed.
- **Always show the complete message before committing — even in the default direct path.** Nothing is hidden behind a summary, and there's always a record to amend from.

Want to review or pick before it lands? Add `-i` / `--interactive` (or just say "让我确认" / "let me confirm"), and the skill shows the message and asks first instead of committing straight away. `-y` / `--yes` is also accepted and simply affirms the default.

## 1. Validate the environment

Read the injected context and stop early when the repo isn't ready:

- Repository shows `NOT_A_GIT_REPO` → say "Not a git repository." and stop.
- Status is empty → say "No changes in the working tree or staging area." and stop.
- Staged files is empty (nothing staged) → say "No staged changes. Run `git add` to stage files first." and stop.

## 2. Understand the change

- **Staged files / diff stat** maps *what* changed at the file level — it stays complete even when the diff below is truncated.
- **Staged diff** shows the actual edits and their intent. If the line count above exceeds 500, the diff is truncated: rely on the diff stat for coverage and run `git diff --staged -- <path>` to read any file you still need in full. Pull only what you need to describe the change accurately.

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

**Wrap code in backticks.** Set off function and method calls, macros, and expressions — `isolate->SetWasmStreamingCallback()`, `WebAssembly.compileStreaming()`, `CHECK(!impl.IsEmpty())`. It pays off most for tokens carrying punctuation like `()`, `->`, or `!`, which read awkwardly bare and leave the reader guessing where the symbol ends; plain filenames, flags, and ordinary identifiers are a judgment call — backtick them only when it genuinely aids reading, not by reflex. Backticks read especially well in a Chinese body, where a bare `foo()` would otherwise blur into the surrounding 中文. The commit is written through a single-quoted heredoc (step 8), so backticks, `$`, `!`, and backslashes reach the message literally — write them as-is and never escape them.

**Break a multi-part why into paragraphs.** When the reasoning has distinct beats, separate them with a blank line instead of packing everything into one block. A reliable shape is 起因 → 影响 → 修复 (cause → effect → fix): what changed upstream, what broke as a result, and what this commit does about it. The blank lines are what let a reader follow the thread.

**When a body would add nothing, omit it.** Some changes are their own explanation — a pure typo fix, a version bump, a lockfile refresh. Forcing a "why" paragraph onto them produces filler that future readers have to wade through. Prefer no body over a padded one. But if you can name any non-obvious reason, context, or risk, include it — when in doubt, write the body.

**Breaking changes:** when the change breaks an existing API or behavior, signal it both ways the spec allows — a `!` before the colon (`feat(api)!: …`) *and* a `BREAKING CHANGE: <what breaks + how to migrate>` footer after a blank line. Keep the `BREAKING CHANGE:` keyword in English (it's a spec token); the description after it may be Chinese. This is the major-version signal; omitting it makes the break silent.

**Issue references:** add a `Closes #123` / `Refs #123` footer only when the diff or branch clearly ties to that issue — don't invent one.

## 7. Show the message, then commit (or ask, if requested)

**Always print the complete message first** — in the current reply, verbatim, in a fenced block. This applies in *both* modes:

````markdown
```text
<complete commit message>
```
````

Then branch on the mode:

### Default — commit directly
Running the skill is the go-ahead, so don't ask. After the block is printed, go straight to step 8, then report what landed **in Chinese**: the subject line and the new short SHA (`git rev-parse --short HEAD`). The default path skips *only* the prompt — every other safeguard holds: validation in step 1 still stops on a no-repo or nothing-staged state instead of committing, and you still never run `git add`.

### Interactive mode — `-i` / `--interactive`, or "让我确认" / "让我看看" / "let me confirm"
The user wants to decide, so after the block is visible, ask with the tool your environment provides — `AskUserQuestion` (Claude Code) or `request_user_input` (Codex):
- `header`: `提交确认`
- `question`: `请确认上方的 commit message，是否执行提交？`
- `options`:
  1. `执行提交 (Recommended)` — run `git commit` with the message above
  2. `否` — stop without committing
  3. `修改` — revise, then confirm again

Never call the confirmation tool before the block appears, and never swap the full message for a summary or rationale. Then wait for the explicit choice:

- **执行提交** → step 8
- **否** → stop, don't commit
- **修改** → fold in the feedback and return to step 7

## 8. Commit

Once you reach this step — directly in the default path, or after the user confirms in interactive mode:

```bash
git commit -s -m "$(cat <<'EOF'
<commit message>
EOF
)"
```

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
