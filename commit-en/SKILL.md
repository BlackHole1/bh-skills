---
name: commit-en
description: Generate a Git commit message following Conventional Commits, entirely in English (subject and body). Manual-invocation only — run it explicitly with /commit-en (Claude Code) or $commit-en (Codex). Use it for committing staged changes when you want an all-English commit message.
disable-model-invocation: true
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), AskUserQuestion, request_user_input
---

# Commit EN

Generate a [Conventional Commits](https://www.conventionalcommits.org/) message in **English**.

## Git Context

**Repository:** !`git rev-parse --is-inside-work-tree 2>/dev/null || echo "NOT_A_GIT_REPO"`
**Branch:** !`git branch --show-current 2>/dev/null`
**Status:**
!`git status --short 2>/dev/null`

**Diff stat:**
!`git diff --staged --stat 2>/dev/null`

**Recent commits (style reference):**
!`git log --oneline -5 2>/dev/null`

**Staged diff:**
!`git diff --staged 2>/dev/null | head -500`

## Critical Constraints

Staging and committing are the user's decisions, and a commit that appears
without review erodes their trust in the tool. Treat the rules below as
non-negotiable for that reason:

1. **Subject ≤ 68 characters** — the `<type>(<scope>): <description>` line
2. **NEVER run `git add`** — only prompt the user to stage files manually
3. **NEVER auto-commit** — always wait for explicit user confirmation
4. **Always show the full commit message first** — render the complete text in a fenced code block before any confirmation prompt

## Execution Flow

### 0. Validate Environment

- If `NOT_A_GIT_REPO` appears in Repository → tell the user "Not a git repository.", stop
- If Status is empty → tell the user "No changes in the working tree or staging area.", stop
- If Diff stat is empty (nothing staged) → tell the user "No staged changes. Run `git add` to stage files first.", stop

### 1. Analyze Changes

Use the injected context above:

- **Diff stat** → file-level overview; identify which modules/areas changed
- **Staged diff** → understand the actual code changes, intent, and impact
- **If the diff is exactly 500 lines, assume it may be truncated** → generate from available context and tell the user the diff was large

### 2. Determine Type

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
| `revert` | Reverting a previous commit (body must cite the reverted commit SHA or original subject) |

### 3. Infer Scope

In priority order:

1. **Branch name pattern** — `feature/auth-login` → `auth`
2. **Single directory changed** — all files under `src/auth/` → `auth`
3. **Single module/component** — only `Button.tsx` changed → `button`
4. **Multiple related areas** — comma-separated: `auth,api` (non-standard extension; some CI tooling may reject it)
5. **Widespread changes** — omit the scope

Match the scope naming style from the **Recent commits** section to stay consistent with the project.

### 4. Write the Subject (≤ 68 characters)

- Use the imperative mood: "add", "fix", "update" — NOT "added", "fixes", "updated"
- Be specific: "add JWT refresh endpoint", NOT "update auth"
- No trailing period
- Lowercase the first letter after the colon

### 5. Write the Body

**Always include a body.** Write prose sentences explaining **why** the change was made:

- Separate it from the subject with a blank line
- Explain the reason, context, or trade-offs — not a list of what changed
- Wrap each line at ≤ 72 characters
- **Write the body in English**

### 6. Confirm With the User — MANDATORY

Follow this sequence exactly. Do not skip or reorder the steps:

1. In **the current reply**, print the complete commit message verbatim inside a fenced code block:

   ````markdown
   ```text
   <complete commit message>
   ```
   ````

2. Only after the code block is present, ask for confirmation using the tool available in your environment:
   - **Claude Code** → `AskUserQuestion`
   - **Codex** → `request_user_input`

   Use this structure:
   - `header`: `Commit`
   - `question`: `Please review the commit message above. Should I commit it?`
   - `options`:
     1. `Commit (Recommended)` — Run `git commit` with the message above.
     2. `No` — Stop without creating a commit.
     3. `Edit` — Revise the commit message, then confirm again.

Forbidden behaviors:

- Saying "I generated a commit message" without printing the full message
- Replacing the full message with a summary, explanation, or rationale
- Calling the confirmation tool before the code block appears in the current reply

If the code block was not shown in the current reply, print the full commit message again first, then ask.

**Wait for an explicit choice. NEVER run `git commit` before the user selects "Commit".**

- User selects **Commit** → go to Step 7
- User selects **No** → stop, do not commit
- User selects **Edit** → incorporate the feedback, return to Step 6

### 7. Execute the Commit

Only after the user has explicitly confirmed in Step 6:

```bash
git commit -s -m "$(cat <<'EOF'
<commit message>
EOF
)"
```

## Language Rules

| Part | Language |
|------|----------|
| Type / Scope / Footer keywords | Always English |
| Subject description | English |
| Body | English |

## Examples

```
feat(auth): add user login feature

Implement JWT-based authentication flow. Refresh token is stored
in httpOnly cookie to mitigate XSS attack surface.
```

```
fix(api): fix null pointer on user lookup

The code assumed user always exists, but unauthenticated requests
would crash. Added null check and unified error response format.
```

```
refactor(db): extract query builder into separate module

The query construction logic was duplicated across 4 repository
files. Centralizing it reduces maintenance burden and makes it
easier to add query caching later.
```
