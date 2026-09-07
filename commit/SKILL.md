---
name: commit
description: Write a Conventional Commits message for the current change and commit it. Use when the user asks to commit, wants a commit message written, or says 提交. Also what create-pr reaches for when a branch has no commits to open a PR from.
argument-hint: "[--zh | --en] [-i | --interactive]"
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), Bash(*commit_helper.py*), Read, AskUserQuestion
---

# Commit

Write a [Conventional Commits](https://www.conventionalcommits.org/) message for the change and commit it through the bundled helper, which owns the git plumbing: it stages everything when nothing is staged, and carves a new branch off main or master before committing so a protected branch never moves.

Running this skill is the go-ahead, so it commits directly. `-i` / `--interactive` (or "让我确认" / "let me confirm") shows the message and asks first.

Guardrails that always hold: exactly one new commit, at most one new branch, no push, no amend, no history rewrite, and the full message printed before it lands.

> `<skill-dir>` below is this skill's base directory, the folder holding this SKILL.md, which your harness names when it loads a skill. Substitute the real absolute path: your working directory is the user's repo, not the skill. The helper is stdlib-only Python 3; run it with `python3`, or `python` where that alias is missing (typical on Windows).

## 1. Prepare

Pass the language the user asked for, or nothing at all.

```bash
python3 <skill-dir>/scripts/commit_helper.py prepare       # this repo's remembered language
python3 <skill-dir>/scripts/commit_helper.py prepare zh    # --zh or "用中文", also records it
python3 <skill-dir>/scripts/commit_helper.py prepare en    # --en or "in English", also records it
```

The helper prints one `STATE` line, then recent commits, the staged stat, and the staged diff. Stop early when `repo=no` ("Not a git repository.") or when the output says `NO_CHANGES` (nothing to commit). Otherwise read these fields off `STATE`:

- `lang=` is `en` or `zh`, already resolved from the argument, this repo's record, or the `en` default. The record lives in `.git/config` and is shared with the create-pr skill.
- `auto_staged=yes` means the helper staged everything for you with `git add -A`. Say so when you report the result.
- `protected=yes` means the commit lands on a new branch that you name in step 5, including when `unborn=yes` (fresh repo, no commits yet).

## 2. Load the language

Read `<skill-dir>/references/<lang>.md`, using the resolved `lang`. It holds the body language and worked examples.

## 3. Understand the change

The staged stat maps the surface area and stays complete even when the diff below it is truncated. The staged diff shows the actual edits. When it was cut off, pull what you still need with `git diff --staged -- <path>`.

Work out *why* the change was made. The body depends on it.

## 4. Write the message

**Subject**, always English whatever the body language: `type(scope): description`, at most 68 characters so it survives `git log` and GitHub truncation. Imperative mood, lowercase first letter, no trailing period, specific over vague ("add JWT refresh endpoint", not "update auth"). Take the narrowest scope that is honestly true and match the scope style in the recent commits above. Use `!` before the colon when the change breaks an existing API.

**Body**, in the resolved language: a blank line after the subject, then the *why* that the diff cannot show, meaning the reason, the context, or the trade-off. Keep it proportional. A version bump or a typo fix earns no body at all, and a padded "why" paragraph is worse than none.

**Footers**, after a blank line: `BREAKING CHANGE: <what breaks and how to migrate>` whenever the subject carries `!`, and `Closes #123` or `Refs #123` only when the diff or the branch clearly ties to that issue.

## House style

Governs the body in both languages.

**Backticks are for code you could paste into an editor or a shell.** Function and method calls, variables, types, literal values, commands, flags: `AuthService.refresh()`, `MaxConns`, `--force`, `npm ci`. Ordinary words stay bare even when they are technical, so cache, middleware, race condition, and connection pool are written plain.

**Paths and directories go in underscores**, not backticks: _src/auth/_, _docs/adr/0004.md_, _package.json_.

**Spec tokens are never translated.** `BREAKING CHANGE`, `Closes #123`, `Refs #123`, `Co-authored-by`, and the type names such as `feat` and `fix` keep their exact characters in a Chinese body.

**Punctuation stays plain.** These characters never appear in a body: `;` `；` `—` `——` `·` and emoji. A period, a comma, or parentheses covers every place they would have gone.

**One paragraph is one line.** The subject has a character budget, the body has none. Write each paragraph as a single continuous line and let git and GitHub wrap it, never folding a sentence across several short lines. Blank lines separate paragraphs, and a multi-part why reads best as cause, then effect, then fix, in three of them.

## 5. Name the branch

Only when `protected=yes` (including `unborn=yes`). The commit lands on a new branch carved from the current tip, so name it after the commit you just wrote: `<type>/<short-kebab-summary>`, lowercase, around 40 characters, for example `feat/jwt-token-refresh` or `fix/user-null-pointer`. The helper appends `-2`, `-3` and so on if the name is taken, so there is nothing to check first. On a feature branch pass `""`.

## 6. Show it, then commit

Print the complete message first, verbatim, in a fenced block, in both modes. When a branch will be created, add one line under the block naming it.

**Default:** go straight to step 7. Running the skill was the go-ahead.

**Interactive** (`-i`, `--interactive`, "让我确认", "let me confirm"): after the block is visible, ask with whatever structured-question tool you have — Claude Code `AskUserQuestion`, Grok `ask_user_question`, Codex `request_user_input` where it is enabled — and simply ask in plain text if you have none. Header `Commit`, question "Commit this message?" plus the branch name when one will be created, options `Commit (Recommended)`, `No`, `Edit`. Commit goes to step 7, No stops, Edit folds in the feedback and returns to step 6.

## 7. Commit

The single-quoted heredoc keeps backticks, `$`, `!`, and backslashes literal, so write them as-is and never escape them.

```bash
python3 <skill-dir>/scripts/commit_helper.py commit "feat/jwt-token-refresh" <<'EOF'
<commit message>
EOF
```

The helper reads the message from stdin, so where no heredoc is available (a Windows shell, for instance) write the message to a temp file and pipe it in instead: `... | python3 <skill-dir>/scripts/commit_helper.py commit "feat/jwt-token-refresh"`.

The helper creates the branch when needed, commits with `-s`, and prints a line such as `COMMITTED branch=feat/jwt-token-refresh created_branch=feat/jwt-token-refresh sha=1a2b3c4`. Report, in the resolved language: the subject and the short SHA, the new branch when `created_branch=` is present, and the auto-staging when step 1 showed `auto_staged=yes`.
