---
name: create-pr
description: Open or update the GitHub Pull Request for the current branch, with an English Conventional-Commits title and a short, reviewable body. Run it explicitly with /create-pr (Claude Code, Grok) or $create-pr (Codex). Pass --zh for a Chinese body or --en for English, and the choice is remembered per repo.
disable-model-invocation: true
argument-hint: "[--zh | --en] [-i | --interactive] [--draft] [base-branch]"
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), Bash(git rev-parse *), Bash(git push -u origin HEAD), Bash(git push origin HEAD), Bash(*pr-helper.sh*), Bash(gh pr view *), Bash(gh pr create *), Bash(gh pr edit *), Bash(gh pr diff *), Bash(gh repo view *), Read, Skill, AskUserQuestion
---

# Create PR

Open, or refresh if one already exists, the GitHub Pull Request for the current branch. The title is always an English [Conventional Commits](https://www.conventionalcommits.org/) line, because in a squash-merge repo it becomes the squash commit's subject. The body is Markdown in the resolved language.

Running this skill is the go-ahead, so it applies directly. `-i` / `--interactive` (or "让我确认" / "let me confirm") shows the title and body and asks first. `--draft` opens a new PR as a draft.

Guardrails that always hold: only the current branch and only its PR, no merge, no history rewrite, no force push, and the full title and body printed before they are applied. The only writes outside PR metadata are a plain push of the current branch, which a PR cannot exist without, and whatever the commit skill does when step 2 hands off to it.

> `<skill-dir>` below is this skill's base directory, the folder holding this SKILL.md, which your harness names when it loads a skill. Substitute the real absolute path: your working directory is the user's repo, not the skill.

## 1. Prepare

Pass the language the user asked for and the base branch if they named one.

```bash
bash <skill-dir>/scripts/pr-helper.sh prepare            # this repo's remembered language
bash <skill-dir>/scripts/pr-helper.sh prepare zh         # --zh or "用中文", also records it
bash <skill-dir>/scripts/pr-helper.sh prepare en develop # --en against an explicit base
```

It prints one `STATE` line, then the existing PR, the branch's commits, the diffstat, a capped branch diff, and the repo's PR template. The `lang=` field is already resolved from the argument, this repo's record, or the `en` default, and the record lives in `.git/config` shared with the commit skill.

## 2. Route on STATE

In this order:

- `repo=no` gives "Not a git repository." and stops.
- `gh=no` means the GitHub CLI could not read this repo, usually because it is unauthenticated or the remote is not on GitHub. Say so, suggest `gh auth login`, and stop.
- `detached=yes` gives "You are on a detached HEAD. Check out a branch first." and stops, since there is no branch to open a PR from.
- `dirty=yes` means work the PR would silently leave out, whether the branch has commits already or none at all. Invoke the **commit** skill to land it — Claude Code: the `Skill` tool, name `commit`; Codex: `$commit`; Grok and others: the `commit` skill, or read `<skill-dir>/../commit/SKILL.md` and follow it yourself if your harness cannot invoke another skill. Then run `prepare` again and read the fresh `STATE`. The commit skill stages the whole working tree, so say which files it swept in when you report the result. On a protected branch it also carves off a feature branch, which clears `on_base` below.
- `commits=0` at this point means the branch holds nothing beyond `base` and there was nothing to commit either. Say so and stop.
- `on_base=yes` gives "You are on the base branch `<base>`. Switch to a feature branch." and stops, since a branch cannot open a PR against itself.

If the base looks wrong, meaning the commit list reaches further back than this branch's real work or the diffstat names files you never touched, re-run `prepare` with an explicit base branch and reason from that output instead.

## 3. Create or update

Read the `state` field of the existing PR block, not just the presence of a number, because `gh pr view` returns a branch's PR even when it is closed or merged.

- `NONE` or `state: "CLOSED"` puts you in **create mode**, step 9a. For a closed PR, say "PR #`<n>` for this branch is closed, opening a new one." first.
- `state: "OPEN"` puts you in **update mode**, step 9b. Note the current title and body, since you will show what changed.
- `state: "MERGED"` stops. Say "PR #`<n>` is already merged, there is nothing to update."

## 4. Load the language

Read `<skill-dir>/references/<lang>.md`, using the resolved `lang`. It holds the body language and worked examples.

## 5. Understand the change

Commits are the primary source. They already carry the author's intent and a `type(scope)` you can lift the title from, so read every subject and body and use the diffstat to confirm the surface area.

When the commits are thin, meaning terse subjects like `wip`, empty bodies, or one squashed commit hiding a large diff, fall back to the code: the diffstat maps the surface area and the branch diff shows the edits. When the diff was capped, read what you still need with `git diff <merge_base>..HEAD -- <path>`, or `gh pr diff <number>` in update mode.

Describe the net change the branch delivers, not a replay of each commit in order. Work out what a reviewer needs in order to approve it.

## 6. Write the title

`type(scope): description`, always English, at most 68 characters so it survives the PR list and `git log` after squash. Imperative mood, lowercase first letter, no trailing period, specific over vague.

Derive the type from the branch's net intent rather than the first commit, so a `feat` among supporting `fix` and `test` commits is still a `feat`. Use `!` before the colon when the branch breaks an existing API, since a single line cannot carry a `BREAKING CHANGE` footer. Take the narrowest honest scope and match the scope style in the branch's own commits.

On a single-commit branch, reuse that commit's subject refined rather than inventing a different one. In update mode, keep the existing title unless the branch's scope has genuinely shifted, and call it out if you change it.

## 7. Write the body

**A reviewer should get the point in under thirty seconds.** That budget is the whole design constraint. Most PRs are one short paragraph plus the lines of code that carry the change. Three sentences is a full paragraph, so start a new one rather than letting it grow, and cut any sentence that only restates what the diff already shows.

**Lead with the point, in prose.** A reliable shape is cause then fix: what was wrong, and what this branch does about it. Open with a sentence that stands on its own, because some repos fold the body into the squash commit message.

**Show the code, not only prose.** A GitHub permalink alone on its line renders inline as an expanded, syntax-highlighted snippet, so the reviewer reads the change instead of your paraphrase of it. Build it from `repo_slug` and `head_sha` on the `STATE` line:

```text
https://github.com/<repo_slug>/blob/<head_sha>/<path>#L42-L48
```

It expands only when the URL sits alone on its own line with a blank line above and below, and only after the branch is pushed, which step 9 does first. Point it at the two or three lines that carry the change, never a whole file. When the point is a before and after contrast, or the code does not live in the repo, a fenced block with a language tag reads better.

**Write it the way a person would, not as a filled-in form.** A fixed `## Summary` / `## Why` / `## What changed` / `## Test plan` scaffold is the clearest tell of a machine-written description, and a body that fits on one screen needs no headings at all. Weave the why into the sentences. Leave testing out unless there is something genuinely worth saying, such as a regression test written for this exact bug, and then say it inline in one sentence.

**Follow the repo's own conventions.** When `prepare` printed a PR template, use it as the skeleton, checkboxes and required links included, since `gh pr create --body` does not apply it for you.

**Structure earns its place only on a genuinely large PR**, meaning many commits, cross-cutting, or risky. Then a one-line overview, a grouped list of the notable changes, a screenshot for a visible UI change, and a short migration note for a breaking change are all worth it. Even there, keep it to what a reader needs.

**Issue footers** go last, and only when the branch or commits clearly tie to an issue. `Closes #123`, `Fixes #123`, and `Resolves #123` auto-close on merge, `Refs #123` links without closing, and `owner/repo#123` crosses repos. Auto-close fires only when the base is the repository's default branch, so against any other base use `Refs #123` and note that the issue closes once the base lands.

## House style

Governs the body in both languages.

**Backticks are for code you could paste into an editor or a shell.** Function and method calls, variables, types, literal values, commands, flags: `AuthService.refresh()`, `MaxConns`, `--force`, `npm ci`. Ordinary words stay bare even when they are technical, so cache, middleware, race condition, and connection pool are written plain.

**Paths and directories go in underscores**, not backticks: _src/auth/_, _docs/adr/0004.md_, _package.json_. In a Chinese body, keep a space on either side so GitHub still renders the italics.

**Spec tokens are never translated.** `BREAKING CHANGE`, `Closes #123`, `Refs #123`, `Co-authored-by`, and the type names such as `feat` and `fix` keep their exact characters in a Chinese body.

**Punctuation stays plain.** These characters never appear in a body: `;` `；` `—` `——` `·` and emoji. A period, a comma, or parentheses covers every place they would have gone.

**One paragraph is one line.** A PR body has no line-length limit and GitHub soft-wraps it, so write each paragraph as a single continuous line. Folding a sentence across several short lines is a commit-message habit that reads as machine-generated here.

## 8. Show it, then apply

Print the complete title and body first, verbatim, in a fenced block, in both modes.

````markdown
**Title:** `<title>`

```markdown
<complete PR body>
```
````

In update mode, add one line on how this differs from the live title and body, so the change is not silent.

**Default:** go straight to step 9. Running the skill was the go-ahead.

**Interactive** (`-i`, `--interactive`, "让我确认", "let me confirm"): after the block is visible, ask with whatever structured-question tool you have — Claude Code `AskUserQuestion`, Grok `ask_user_question`, Codex `request_user_input` where it is enabled — and simply ask in plain text if you have none. Header `Create PR` or `Update PR`, question "Apply this title and body?", options `Apply (Recommended)`, `No`, `Edit`. Apply goes to step 9, No stops, Edit folds in the feedback and returns to step 8.

## 9. Apply

**Push first**, in both modes, so the diff a reviewer sees matches the body and so permalinks resolve. This assumes the remote is named `origin`. If the repo's only remote has another name, push manually with `git push -u <remote> HEAD` and skip ahead.

- `upstream=none` calls for `git push -u origin HEAD`.
- Otherwise a non-zero `ahead` calls for `git push origin HEAD`, a plain push of the current branch and never `--force`.
- Otherwise push nothing.

Then write the body through a single-quoted heredoc so backticks, `#`, `$`, `!`, and `[]()` reach GitHub literally. Never escape anything inside it.

### 9a. Create

Add `--draft` when requested, and `--base <base>` only when overriding the repo default.

```bash
gh pr create --title "type(scope): description" --body "$(cat <<'EOF'
<complete PR body>
EOF
)"
```

### 9b. Update

`gh pr edit` replaces the body wholesale, and the body from `prepare` may already be stale if a human or a bot edited the PR since. Re-fetch the live body and merge into that.

```bash
gh pr view <number> --json body -q .body
gh pr edit <number> --body "$(cat <<'EOF'
<complete PR body>
EOF
)"
# add --title only when step 6 decided the title genuinely changed
```

Regenerate the narrative from the branch's current state, and preserve what a human clearly added: a filled-in template checklist, reviewer notes, a hand-written `Closes #`. When in doubt keep it, and say what you kept.

Finally report, in the resolved language: the PR URL and number, and whether you created or updated it.
