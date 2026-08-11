# bh-skills

Personal agent [skills](https://github.com/vercel-labs/skills) for Claude Code, Codex, Grok, and other agents.

**English** | [简体中文](./README.zh-CN.md)

## Skills

### `commit` — Conventional Commits messages, in English or Chinese

Writes a [Conventional Commits](https://www.conventionalcommits.org/) message
for your change and commits it. The subject is always English; the body is
whichever language you picked, and explains *why* the change was made. It
stages everything when nothing is staged, and on a protected branch it carves
off a new branch first so your work never lands on `main`. It shows you the
complete message and then commits: running the skill is your go-ahead, so it
won't make you click through a prompt you'd just approve anyway. It never
pushes, never amends, and never rewrites history. Add `-i` / `--interactive`
(or say "让我确认") to review and choose before it commits.

```bash
/commit          # this repo's remembered language, English by default
/commit --zh     # Chinese body, and remembered for this repo from now on
/commit --en     # back to English, likewise remembered
```

### `create-pr` — PR descriptions, in English or Chinese

Opens a Pull Request for your current branch, with an English Conventional
Commits title and a Markdown body in your chosen language. It reads the
branch's commits and falls back to the diff when the commits are too thin to
explain the change. The body is written to be read in under thirty seconds:
usually one short paragraph, with a GitHub permalink only where prose alone
would leave a reviewer hunting, at most two, and pinned to the commit that
actually makes the point, the pre-branch one for a cause and the branch tip for
an intricate implementation. If the branch has uncommitted
work or no commits at all, it runs the `commit` skill first. If a PR already
exists, it *updates* the title and body instead of opening a duplicate,
preserving any hand-curated template content. It only ever touches the current
branch's PR, never merges, and never rewrites history. Add `-i` /
`--interactive` to review first, or `--draft` to open a draft.
Triggers on its own when you ask for a PR, or run `/create-pr` directly.

```bash
/create-pr              # this repo's remembered language, English by default
/create-pr --zh         # Chinese body, and remembered for this repo from now on
/create-pr --draft main # draft PR against an explicit base branch
```

Both skills share one language record, stored as `skills.lang` in the repo's
`.git/config`. It is per-repo and never committed, so setting it once in a repo
covers every later `/commit` and `/create-pr` there.

### `ship-pr` — babysit a PR until it is ready (merge only with `-y`)

Watches an open pull request until CI is green and every review thread is
resolved: works through reviewer-bot findings (e.g. CodeRabbit) — fixing the
real ones in an isolated worktree and pushing, replying to the false positives.
By default it stops at "ready to merge", notifies you, and leaves the merge
click to you. Pass `-y` (or say so in your own words, "land it, no review
needed") and it squash-merges the moment that gate holds — never by a mid-run
question. Running the skill is your go-ahead for its writes — pushing fix
commits to the PR head and, with `-y`, the merge — so those are pre-approved
and won't stall on permission prompts. Commits are written through the
`commit` skill, and if the fixes grow to change what the PR means it refreshes
the title and body through `create-pr`. It inspects and edits PR code only in a
throwaway worktree, never your checkout, and surfaces anything risky (merge
conflicts, unexplained failures, required gates) instead of guessing. On Claude
Code it can auto-trigger when you ask to land / merge / ship / babysit a PR;
Codex requires an explicit `/ship-pr` (or `$ship-pr`).

```bash
/ship-pr           # babysit the current branch's PR; stops at "ready to merge"
/ship-pr 42 -y     # babysit PR #42 and squash-merge it the moment it's ready
```

## Agent support

Written to run unchanged on Claude Code, Codex, and Grok. Where one harness has
an affordance another lacks, the skill names the alternative instead of assuming
it: the interactive prompt uses whichever structured-question tool is available
and falls back to asking in plain text, and `ship-pr` waits on a streaming
monitor where one exists (Claude Code, Grok) or on a blocking
`pr-watch.sh --once` where none does (Codex). All the git and GitHub work lives
in the bundled shell scripts, which need only bash 3.2 with `git`, `gh`, and
`jq` — nothing agent-specific.

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

Grok is not one of the CLI's `-a` targets, but it scans `~/.claude/skills/` and
`.agents/skills/` by default, so a Claude Code install covers Grok as well.
