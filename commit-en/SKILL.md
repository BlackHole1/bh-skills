---
name: commit-en
description: Generate a Git commit message following Conventional Commits, entirely in English (subject and body), then commit it. Manual-invocation only — run it explicitly with /commit-en (Claude Code) or $commit-en (Codex). Stages everything for you when nothing is staged yet, and when you're on a protected branch (main/master) it carves off a new branch before committing so your work never lands directly on main. Commits directly by default; pass -i / --interactive (or say "let me confirm") to review and choose before it commits.
disable-model-invocation: true
argument-hint: "[-i | --interactive]"
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), Bash(*commit-helper.sh*), AskUserQuestion, request_user_input
---

# Commit EN

Write a [Conventional Commits](https://www.conventionalcommits.org/) message — subject and body both in **English** — for the change, then commit it through the bundled helper. The helper owns the fiddly git plumbing so you don't chain it by hand: if nothing is staged yet it stages everything (`git add -A`), and if you're sitting on a protected branch (main/master) it creates and switches to a new branch first, so your work never lands directly on main. **Two modes:** by default, show the message and commit it directly — running the skill is your go-ahead; if the user opts into interactive mode (`-i` / `--interactive`, or "let me confirm" / "let me review"), show the message — and the branch it would create — and ask before committing.

> Throughout, `<skill-dir>` is this skill's base directory (the path shown to you when the skill loaded, e.g. `…/commit-en`). Substitute the real absolute path when you run a command.

## Git Context

**Repository:** !`git rev-parse --is-inside-work-tree 2>/dev/null || echo "NOT_A_GIT_REPO"`
**Branch:** !`git branch --show-current 2>/dev/null`
**Status (before staging):**
!`git status --short 2>/dev/null`

**Recent commits (style reference):**
!`git log --oneline -5 2>/dev/null`

## What keeps this safe

Running `/commit-en` is itself the decision to commit, so the skill stages, branches, and commits without stopping to rubber-stamp each step. Two of those are new powers — staging on your behalf and creating a branch — so it's worth being precise about why they're safe:

- **Auto-staging is reversible and local.** When nothing is staged, the helper runs `git add -A` — exactly the `git add .` you'd otherwise type. It only ever *adds*; it never discards or overwrites working-tree content, and nothing leaves your machine. A staging you didn't want is one `git reset` away.
- **Auto-branching protects main instead of risking it.** On a protected branch (main/master) the helper creates a new branch from the current commit *before* committing, so the protected branch is never moved. Carving a branch off the current commit keeps your staged and unstaged changes byte-for-byte intact — there's no stash, no merge, nothing to lose. (This is the one people worry about, but `git switch -c` touches neither the index nor the working tree; it only points a new ref at the commit you're already on.)
- **Exactly one new commit, nothing destructive.** The skill never pushes, never rewrites or amends existing commits, never force-anything. It makes one new commit — and at most one new branch — from your changes. A message you don't like is one `git commit --amend` away from fixed.
- **The full message is always shown before the commit lands** — even on the default direct path. Nothing is hidden behind a summary, and there's always a record to amend from.

Want to review or pick before it lands? Add `-i` / `--interactive` (or just say "let me confirm" / "let me review"), and the skill shows the message — and the branch it would create — and asks first. `-y` / `--yes` is also accepted and simply affirms the default.

## 1. Prepare the working tree

Run the bundled helper to stage (when needed) and read back a compact, token-lean snapshot. It does the staging so you can describe a complete diff even when the user staged nothing:

```bash
bash <skill-dir>/scripts/commit-helper.sh prepare
```

It prints one `STATE` line, then the staged stat and the staged diff. Read the `STATE` line and stop early when there's nothing to do:

- `STATE repo=no` → say "Not a git repository." and stop.
- `NO_CHANGES` (working tree clean) → say "No changes in the working tree — nothing to commit." and stop.

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

- Imperative mood: "add", "fix", "update" — not "added", "fixes", "updated"
- Specific over vague: "add JWT refresh endpoint", not "update auth"
- Lowercase the first letter of the description; no trailing period
- Keep the whole line ≤ 68 characters so it survives `git log` and GitHub truncation

## 6. Write the body (English)

The diff already records *what* changed; the body's lasting value is *why*. Include one whenever there's a reason, context, or trade-off worth recording — which is almost every change:

- Blank line after the subject, then prose explaining the *why* — not a restated changelog
- Wrap lines at ≤ 72 characters
- Keep it proportional to the change: a subtle fix earns a short paragraph; a one-line refactor earns one sentence

**Wrap code in backticks.** Set off function and method calls, macros, and expressions — `isolate->SetWasmStreamingCallback()`, `WebAssembly.compileStreaming()`, `CHECK(!impl.IsEmpty())`. It pays off most for tokens carrying punctuation like `()`, `->`, or `!`, which read awkwardly bare and leave the reader guessing where the symbol ends; plain filenames, flags, and ordinary identifiers are a judgment call — backtick them only when it genuinely aids reading, not by reflex. The commit is written through a single-quoted heredoc (step 9), so backticks, `$`, `!`, and backslashes reach the message literally — write them as-is and never escape them.

**Break a multi-part why into paragraphs.** When the reasoning has distinct beats, separate them with a blank line instead of packing everything into one block. A reliable shape is cause → effect → fix: what changed upstream, what broke as a result, and what this commit does about it. Each paragraph stays wrapped at ≤ 72; the blank lines are what let a reader follow the thread.

**When a body would add nothing, omit it.** Some changes are their own explanation — a pure typo fix, a version bump, a lockfile refresh. Forcing a "why" paragraph onto them produces filler that future readers have to wade through. Prefer no body over a padded one. But if you can name any non-obvious reason, context, or risk, include it — when in doubt, write the body.

**Breaking changes:** when the change breaks an existing API or behavior, signal it both ways the spec allows — a `!` before the colon (`feat(api)!: …`) *and* a `BREAKING CHANGE: <what breaks + how to migrate>` footer after a blank line. This is the major-version signal; omitting it makes the break silent.

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

When a branch will be created (`protected=yes`), add one line under the block naming it, e.g. `Will create and switch to branch feat/jwt-token-refresh off main.` Then branch on the mode:

### Default — commit directly
Running the skill is the go-ahead, so don't ask. After the block is printed, go straight to step 9. The default path skips *only* the prompt — every other safeguard holds: prepare in step 1 still stops on a no-repo or no-changes state, and every mutation still flows through the audited helper.

### Interactive mode — `-i` / `--interactive`, or "let me confirm" / "let me review" / "let me choose"
The user wants to decide, so after the block is visible, ask with the tool your environment provides — `AskUserQuestion` (Claude Code) or `request_user_input` (Codex):
- `header`: `Commit`
- `question`: `Please review the commit message above` (when a branch will be created, append `; it will be committed to a new branch <name>`) `. Should I commit it?`
- `options`:
  1. `Commit (Recommended)` — run the commit helper with the message above
  2. `No` — stop without committing
  3. `Edit` — revise, then confirm again

Never call the confirmation tool before the block appears, and never swap the full message for a summary or rationale. Then wait for the explicit choice:

- **Commit** → step 9
- **No** → stop, don't commit
- **Edit** → fold in the feedback and return to step 8

## 9. Commit

Pipe the message into the helper's `commit` subcommand, passing the branch name from step 7 (or `""` on a feature branch). The single-quoted heredoc keeps backticks, `$`, `!`, and backslashes literal:

```bash
bash <skill-dir>/scripts/commit-helper.sh commit "feat/jwt-token-refresh" <<'EOF'
<commit message>
EOF
```

The helper creates the branch when on a protected branch, commits with `-s` (sign-off), and prints a `COMMITTED` line such as `COMMITTED branch=feat/jwt-token-refresh created_branch=feat/jwt-token-refresh sha=1a2b3c4`. Report what landed:

- the subject line and the new short SHA (`sha=`)
- when `created_branch=` is present → created and switched to branch `<name>` off the protected branch
- when step 1 showed `auto_staged=yes` → staged all changes for you (equivalent to `git add .`)

## Examples

```
feat(auth): add user login feature

Implement JWT-based authentication flow. Refresh token is stored
in httpOnly cookie to mitigate XSS attack surface.
```

```
fix(api): fix null pointer on user lookup

The code assumed `user` always exists, but unauthenticated requests
would crash. Added a `null` check and unified the error response
format.
```

```
refactor(db): extract query builder into separate module

The query construction logic was duplicated across 4 repository
files. Centralizing it reduces maintenance burden and makes it
easier to add query caching later.
```

A multi-part *why* reads best as separate paragraphs, with code set off in
backticks:

```
fix(pool): release connection on context cancel

When a caller cancelled the request context mid-query, `Pool.Acquire()`
returned but the deferred `conn.Release()` never ran, because the
cancellation unwound the stack before the defer was registered.

Over a sustained burst of cancelled requests the pool leaked one
connection each time and eventually blocked every caller in
`Acquire()`, since `MaxConns` had all been handed out and none came
back. The symptom looked like a deadlock but was really exhaustion.

Register the release with `defer` immediately after a successful
acquire, before any work that can observe cancellation, so the
connection returns to the pool on every path.
```

A self-explanatory change needs no body:

```
chore: bump version to 2.3.1
```

```
feat(api)!: replace positional args in createUser with options object

The positional signature had grown to five arguments and was easy to
misorder. An options object makes each field explicit at the call site.

BREAKING CHANGE: `createUser(name, email, role)` is now
`createUser({ name, email, role })`.
```
