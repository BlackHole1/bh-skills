# bh-skills

Personal agent [skills](https://github.com/vercel-labs/skills) for Claude Code, Codex, and other agents.

**English** | [简体中文](./README.zh-CN.md)

## Skills

### `commit-zh` — Chinese commit messages

Writes a [Conventional Commits](https://www.conventionalcommits.org/) message
for your **staged** changes: an English subject line plus a Chinese body that
explains *why* the change was made. It reads the staged diff, infers the type
and scope from the changes and your recent history, shows you the complete
message, and waits for your confirmation before committing — it never runs
`git add` and never commits on its own. Manual-invocation only: run
`/commit-zh`.

### `commit-en` — English commit messages

The same workflow as `commit-zh`, but the entire message — subject and body —
is in English. Manual-invocation only: run `/commit-en`.

### `ship-pr` — babysit a PR until it merges

Drives an open pull request from *open* to *merged* without supervision: waits
for CI to go green, works through reviewer-bot findings (e.g. CodeRabbit) —
fixing the real ones in an isolated worktree and pushing, replying to the false
positives — and squash-merges once every check passes and all review threads
resolve. It inspects and edits PR code only in a throwaway worktree, never your
checkout, and surfaces anything risky (merge conflicts, unexplained failures,
required gates) instead of guessing. Auto-triggers when you ask to land / merge
/ ship / babysit a PR once CI is green, or run it explicitly with `/ship-pr`.

## Installation

Install with [`skills`](https://github.com/vercel-labs/skills) — one command,
works across every supported agent (Claude Code, Codex, Cursor, and more):

```bash
# Interactive: choose the scope, agents, and skills to install
npx skills add BlackHole1/bh-skills
```

Useful variants:

```bash
# Preview what's in the repo without installing anything
npx skills add BlackHole1/bh-skills --list

# Install everything, globally, to Claude Code and Codex, no prompts
npx skills add BlackHole1/bh-skills -g -a claude-code -a codex --skill '*' -y
```

Manage them later:

```bash
npx skills list      # list installed skills
npx skills update    # update to the latest version
npx skills remove    # remove from your agents
```
