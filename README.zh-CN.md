# bh-skills

为 Claude Code、Codex 及其他 agent 准备的个人 [skills](https://github.com/vercel-labs/skills)。

[English](./README.md) | **简体中文**

## Skills

### `commit-zh` — 中文 commit message

为你**已暂存（staged）**的改动生成一条 [Conventional Commits](https://www.conventionalcommits.org/)
规范的提交信息：标题用英文，正文用中文解释这次改动**为什么**要做。它会读取
staged diff，结合改动内容与最近的提交历史推断 type 和 scope，把完整信息展示给你，
然后**直接提交**——运行这个 skill 本身就是你的提交意图，不再让你点一个反正都会同意的
确认框。它**绝不执行 `git add`、绝不 push**，对结果不满意一条 `git commit --amend`
即可改。想先复核或挑选？加上 `-i` / `--interactive`（或说“让我确认”），它会展示
信息并先征求你的选择再提交。仅显式调用：运行 `/commit-zh`。

### `commit-en` — 英文 commit message

与 `commit-zh` 流程相同，但整条信息（标题与正文）都使用英文，同样支持 `-i` /
`--interactive` 复核选项。仅显式调用：运行 `/commit-en`。

### `create-pr-zh` — 中文 PR 描述

为当前分支开启一个 Pull Request：标题用英文 Conventional Commits，正文用中文
Markdown 编写。它读取分支上的 commit 来起草正文，当 commit 信息太少、不足以说明
改动时再回退去看 diff。标题会成为 squash 合并后的提交标题，因此保持英文；正文则是
完整的 GitHub Markdown，可用标题、列表、代码块，且不受 commit 正文那种 72 列换行的
约束。若该分支已存在 PR，运行这个 skill 会改为**更新**其标题/正文，而非重复开 PR，
并保留模板里人工填写的内容。应用前会先展示完整的标题与正文——运行这个 skill 本身
就是你的意图，不再让你点确认框——加 `-i` / `--interactive`（或说“让我确认”）则会
先征求你的选择。它只触碰当前分支的 PR，绝不合并、绝不改写历史。仅显式调用：运行
`/create-pr-zh`。

### `create-pr-en` — 英文 PR 描述

与 `create-pr-zh` 流程相同，但 PR 正文使用英文，同样支持“已存在则更新”的行为与
`-i` / `--interactive` 复核选项。仅显式调用：运行 `/create-pr-en`。

### `ship-pr` — 全程看护 PR 直到合并

无人值守地把一个开启状态的 Pull Request 从 *open* 推进到 *merged*：等待 CI 变绿，
逐条处理审查机器人（如 CodeRabbit）的意见——真实问题在独立 worktree 中修复并推送，
误报则回复说明——并在所有检查通过、所有审查讨论串关闭后执行 squash 合并。它只在
临时 worktree 中查看和修改 PR 代码，**绝不触碰你的工作区**；遇到有风险的情况
（合并冲突、无法解释的失败、必需的门禁）会交回给你而非贸然猜测。当你要求在 CI 通过后
land / merge / ship / 看护某个 PR 时会自动触发，也可显式运行 `/ship-pr`。

## 安装

使用 [`skills`](https://github.com/vercel-labs/skills) 安装——一条命令即可，
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
