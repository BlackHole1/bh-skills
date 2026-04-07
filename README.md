# bh-skills

Personal skills plugin for Claude Code and Codex.

## Skills

| Skill | Description |
|-------|-------------|
| `bh:commit-cn` | 生成中文 Git commit message（Conventional Commits） |
| `bh:commit-en` | Generate English Git commit messages (Conventional Commits) |

---

## Installation

### Claude Code

#### Local (repo already cloned)

```bash
# 1. Register the marketplace
claude plugins marketplace add /path/to/bh-skills

# 2. Install the plugin
claude plugins install bh@bh-skills
```

#### Remote (from GitHub)

```bash
# 1. Register the marketplace
claude plugins marketplace add BlackHole1/bh-skills

# 2. Install the plugin
claude plugins install bh@bh-skills
```

After installation, restart Claude Code. Skills are available as `/commit-cn` and `/commit-en`.

#### Updating

```bash
claude plugins uninstall bh@bh-skills
rm -rf ~/.claude/plugins/cache/bh-skills
claude plugins install bh@bh-skills
```

---

### Codex

Codex uses a symlink to load the skills directory.

#### Local (repo already cloned)

```bash
mkdir -p ~/.agents/skills
ln -s /path/to/bh-skills/codex/skills ~/.agents/skills/bh
```

#### Remote (from GitHub)

```bash
git clone git@github.com:BlackHole1/bh-skills.git ~/.local/share/bh-skills
mkdir -p ~/.agents/skills
ln -s ~/.local/share/bh-skills/codex/skills ~/.agents/skills/bh
```

Restart Codex. Skills are available as `bh:commit-cn` and `bh:commit-en`.

#### Updating

If you installed from a local clone, update that clone as usual.

```bash
git -C ~/.local/share/bh-skills pull
```

Skills update instantly through the symlink.
