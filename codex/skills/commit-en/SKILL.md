---
name: commit-en
description: Generate English Git commit messages following Conventional Commits. Use when the user requests to commit code, generate a commit message, or complete work that requires committing.
---

# Commit EN

Generate commit messages following [Conventional Commits](https://www.conventionalcommits.org/).

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

1. **Title ≤ 68 characters** — the `<type>(<scope>): <description>` line
2. **NEVER execute `git add`** — only prompt user to run it manually
3. **NEVER auto-commit** — always wait for explicit user confirmation

## Execution Flow

### 0. Validate Environment

- If `NOT_A_GIT_REPO` appears in Repository → tell user "Not a git repository.", stop
- If Status is empty → tell user "No changes in working tree or staging area.", stop
- If Diff stat is empty (nothing staged) → tell user "No staged changes. Please run `git add` to stage your changes first.", stop

### 1. Analyze Changes

Use the injected context above:

- **Diff stat** → file-level overview, identify which modules/areas changed
- **Staged diff** → understand the actual code changes, intent, and impact
- **If the diff output is exactly 500 lines, assume it may be truncated** → generate based on available context and note to user that the diff was large

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

Determine scope from context, in priority order:

1. **Branch name pattern** — `feature/auth-login` → `auth`
2. **Single directory changed** — all files in `src/auth/` → `auth`
3. **Single module/component** — changes only in `Button.tsx` → `button`
4. **Multiple related areas** — comma-separated: `auth,api` (non-standard extension — some CI tooling may reject it)
5. **Widespread changes** — omit scope entirely

Learn scope naming conventions from the **Recent commits** section. Match the project's existing style.

### 4. Write Description (title ≤ 68 characters)

- Use imperative mood: "add", "fix", "update" — NOT "added", "fixes", "updated"
- Be specific: "add JWT refresh endpoint" — NOT "update auth"
- No period at the end
- Lowercase first letter after the colon

### 5. Write Body

**Body is always included.** Write prose sentences explaining **why** the change was made:

- Separate from title with a blank line
- Explain the reason, context, or trade-offs — not just what changed
- Keep each line ≤ 72 characters
- Write in English

### 6. Present to User — MANDATORY CONFIRMATION REQUIRED

Display the complete commit message in a code block, then **use `request_user_input` to ask exactly 1 question** with this structure:

- `header`: `Commit`
- `id`: `commit_action`
- `question`: `Please review the commit message above. Should I commit it?`
- `options`:
  1. `Commit (Recommended)` — Use the commit message above and run `git commit`.
  2. `No` — Stop without creating a commit.
  3. `Edit` — Revise the commit message before confirming again.

**You MUST wait for the user's explicit selection before doing anything else. Do NOT call `git commit` until the user selects `Commit`.**

- If the user selects **Commit** → proceed to Step 7
- If the user selects **No** → stop, do not commit
- If the user selects **Edit** → incorporate feedback, return to Step 6

### 7. Execute Commit

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
| Description (title) | English |
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
