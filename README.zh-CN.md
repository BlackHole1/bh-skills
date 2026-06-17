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
