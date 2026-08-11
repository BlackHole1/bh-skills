# bh-skills

为 Claude Code、Codex、Grok 及其他 agent 准备的个人 [skills](https://github.com/vercel-labs/skills)。

[English](./README.md) | **简体中文**

## Skills

### `commit` — Conventional Commits 提交信息，中英文可选

为当前改动生成一条 [Conventional Commits](https://www.conventionalcommits.org/)
规范的提交信息并提交。标题恒为英文，正文使用你选定的语言，解释这次改动
**为什么**要做。没有暂存任何内容时它会自动暂存全部改动；处在受保护分支上时会先
切出一个新分支，你的工作永远不会直接落到 `main`。它把完整信息展示给你后
**直接提交**：运行这个 skill 本身就是你的提交意图，不再让你点一个反正都会同意的
确认框。它绝不 push、绝不 amend、绝不改写历史。想先复核？加上 `-i` /
`--interactive`（或说“让我确认”），它会展示信息并先征求你的选择。

```bash
/commit          # 沿用本仓库记住的语言，顶层默认英文
/commit --zh     # 中文正文，并记住，此后本仓库默认中文
/commit --en     # 切回英文，同样记住
```

### `create-pr` — PR 描述，中英文可选

为当前分支开启 Pull Request：标题用英文 Conventional Commits，正文用你选定的语言
编写。它读取分支上的 commit 来起草正文，当 commit 信息太少、不足以说明改动时再回退
去看 diff。正文以“三十秒内读完”为目标：通常只有一小段；只在散文说不清、审查者需要自己
去翻代码时才放 GitHub permalink，最多两个，并且指向真正说明问题的那个 commit：讲原因就
指分支之前的代码，讲复杂实现就指分支上的代码。若分支上还有未提交的改动、或根本没有
commit，它会先调用 `commit` skill。若该分支已存在 PR，则改为**更新**标题与正文而非
重复开 PR，并保留模板里人工填写的内容。它只触碰当前分支的 PR，绝不合并、绝不改写
历史。加 `-i` / `--interactive` 可先复核，`--draft` 则开草稿 PR。当你提出开 PR 时会
自动触发，也可直接运行 `/create-pr`。

```bash
/create-pr              # 沿用本仓库记住的语言，顶层默认英文
/create-pr --zh         # 中文正文，并记住，此后本仓库默认中文
/create-pr --draft main # 针对指定 base 分支开草稿 PR
```

两个 skill 共用同一份语言记录，存放在仓库 `.git/config` 的 `skills.lang`。它按仓库
生效且不会进入版本库，因此在一个仓库里设定一次，之后所有 `/commit` 与 `/create-pr`
都会沿用。

### `ship-pr` — 看护 PR 直到就绪（仅 `-y` 时合并）

看护一个开启状态的 Pull Request，直到 CI 变绿且所有审查讨论串关闭：逐条处理审查
机器人（如 CodeRabbit）的意见，真实问题在独立 worktree 中修复并推送，误报则回复
说明。默认停在“可以合并”并通知你，把合并这一下留给你自己。传入 `-y`（或用你自己的
话明确授权，如“绿了就直接合并”）时，才在就绪的那一刻执行 squash 合并，绝不会中途
再问一次。运行这个 skill 本身就是对其写操作的授权，包括向 PR head 推送修复 commit
以及 `-y` 时的合并，这些命令已预先放行，不会再被权限确认卡住。修复的 commit 统一
交给 `commit` skill 生成；若修复累积到改变了 PR 的原有语义，则通过 `create-pr`
刷新 PR 的标题与正文。它只在临时 worktree 中查看和修改 PR 代码，**绝不触碰你的
工作区**；遇到有风险的情况（合并冲突、无法解释的失败、必需的门禁）会交回给你而非
贸然猜测。在 Claude Code 上，当你要求 land / merge / ship / 看护某个 PR 时可以自动
触发；Codex 需要显式运行 `/ship-pr`（或 `$ship-pr`）。

```bash
/ship-pr           # 看护当前分支的 PR，停在“可以合并”状态
/ship-pr 42 -y     # 看护 PR #42，就绪的那一刻执行 squash 合并
```

## Agent 支持

这些 skill 无需改动即可在 Claude Code、Codex、Grok 上运行。当某个 harness 缺少
另一个才有的能力时，skill 会写明替代方案而不是想当然：交互确认使用当前可用的结构化
提问工具，没有则直接用纯文本发问；`ship-pr` 的等待在有流式监听工具的 harness 上走
monitor（Claude Code、Grok），没有的则走阻塞式 `pr-watch.sh --once`（Codex）。所有
git 与 GitHub 操作都封装在随附的 shell 脚本里，只依赖 bash 3.2 加 `git`、`gh`、`jq`，
不含任何 agent 专有的东西。

## 安装

使用 [`skills`](https://github.com/vercel-labs/skills) 安装，一条命令即可，
适配所有受支持的 agent（Claude Code、Codex、Cursor 等）：

```bash
# 交互式：自行选择安装范围、目标 agent 和要装的 skill
npx skills add BlackHole1/bh-skills
```

常用变体：

```bash
# 仅预览仓库里有哪些 skill，不实际安装
npx skills add BlackHole1/bh-skills --list

# 全局安装全部 skill 到 Claude Code 和 Codex，跳过所有确认
npx skills add BlackHole1/bh-skills -g -a claude-code -a codex --skill '*' -y
```

后续管理：

```bash
npx skills list      # 列出已安装的 skill
npx skills update    # 更新到最新版本
npx skills remove    # 从 agent 中移除
```

Grok 不在该 CLI 的 `-a` 目标列表里，但它默认会扫描 `~/.claude/skills/` 与
`.agents/skills/`，因此装到 Claude Code 就等于同时装给了 Grok。
