---
name: commit-cn
description: 生成中文 Git commit message，遵循 Conventional Commits 规范。当用户请求提交代码、生成 commit 信息时使用。
allowed-tools: Bash(git diff *), Bash(git log *), Bash(git status *), AskUserQuestion
---

# Commit CN

生成遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范的中文 commit message。

## Git 上下文

**仓库：** !`git rev-parse --is-inside-work-tree 2>/dev/null || echo "NOT_A_GIT_REPO"`
**分支：** !`git branch --show-current 2>/dev/null`
**状态：**
!`git status --short 2>/dev/null`

**Staged 文件概览：**
!`git diff --staged --stat 2>/dev/null`

**最近提交（风格参考）：**
!`git log --oneline -5 2>/dev/null`

**Staged diff：**
!`git diff --staged 2>/dev/null | head -500`

## 关键约束

1. **标题 ≤ 68 字符** — `<type>(<scope>): <描述>` 这一行
2. **绝对不执行 `git add`** — 只提示用户手动运行
3. **绝对不自动提交** — 始终等待用户明确确认
4. **必须先展示完整 commit message** — 在调用确认工具前，必须把完整文本原样放进代码块

## 执行流程

### 0. 验证环境

- 若 Repository 显示 `NOT_A_GIT_REPO` → 告知用户"不是 git 仓库"，停止
- 若 Status 为空 → 告知用户"工作区和暂存区均无变更"，停止
- 若 Diff stat 为空（无 staged 内容）→ 告知用户"没有已暂存的变更，请先运行 `git add` 暂存文件"，停止

### 1. 分析变更

利用上方注入的上下文：

- **Diff stat** → 文件级概览，识别涉及的模块/区域
- **Staged diff** → 理解实际代码变更、意图和影响
- **若 diff 输出恰好为 500 行，假定可能被截断** → 基于已有上下文生成，并告知用户 diff 较大

### 2. 确定 type

| Type | 使用场景 |
|------|----------|
| `feat` | 新增用户可见的功能 |
| `fix` | 修复错误行为 |
| `docs` | 仅修改文档/注释 |
| `style` | 仅修改格式，无逻辑变更 |
| `refactor` | 重构代码，不改变行为 |
| `perf` | 可量化的性能提升 |
| `test` | 新增或修改测试 |
| `build` | 构建系统或依赖变更 |
| `ci` | CI 配置变更 |
| `chore` | 杂项维护任务 |
| `revert` | 回滚之前的提交（body 中需注明被回滚的提交 SHA 或原始标题） |

### 3. 推断 scope

按优先顺序：

1. **分支名模式** — `feature/auth-login` → `auth`
2. **单一目录变更** — 所有文件在 `src/auth/` → `auth`
3. **单一模块/组件** — 仅变更 `Button.tsx` → `button`
4. **多个相关区域** — 逗号分隔：`auth,api`（非标准扩展，部分 CI 工具可能不支持）
5. **大范围变更** — 省略 scope

参考**最近提交**中的 scope 命名风格，与项目保持一致。

### 4. 撰写标题

- **标题描述必须使用英文**，无论用户用何种语言交流
- 使用英文动词短语（祈使句）：如 "add"、"fix"、"refactor"
- 具体明确："add JWT token refresh endpoint"，而非 "update auth"
- 结尾无句号
- `type(scope):` 后接英文描述（冒号后有空格）

### 5. 撰写 body

**Body 始终生成**，使用散文句子解释**为什么**做这个改动：

- 与标题之间空一行
- 解释改动的原因、背景或权衡，而非罗列做了什么
- 每行 ≤ 72 字符
- 使用中文

### 6. 展示给用户确认 — 必须等待明确回复

必须严格按以下顺序执行，不能省略或调换：

1. 在**当前这条回复里**先输出完整 commit message，且必须使用 fenced code block 原样展示
2. 只有在代码块已经输出后，才能**使用 `AskUserQuestion` 工具**询问

输出模板如下：

````markdown
```text
<complete commit message>
```
````

禁止行为：

- 只说“我已经生成了一版 commit message”但不贴出完整正文
- 用摘要、说明、理由替代完整 commit message
- 在当前回复没有出现代码块时就直接调用 `AskUserQuestion`

如果因为上下文或工具调用导致这条回复里没有成功展示代码块，必须先重新输出完整 commit message，再进入确认步骤。

然后询问：

> "请确认上方的 commit message，是否执行提交？（是 / 否 / 修改）"

**必须等待用户明确回复，在收到回复前绝对不能调用 `git commit`。**

- 用户回答**是** → 进入步骤 7
- 用户回答**否**或**取消** → 停止，不执行提交
- 用户要求**修改** → 根据反馈调整后返回本步骤

### 7. 执行提交

仅在步骤 6 用户明确确认后执行：

```bash
git commit -s -m "$(cat <<'EOF'
<commit message>
EOF
)"
```

## 语言规则

| 部分 | 语言 |
|------|------|
| Type / Scope / Footer 关键词 | 始终英文 |
| 标题描述 | **始终英文** |
| Body | 中文 |

## 示例

```
feat(auth): add user login

实现了基于 JWT 的用户认证流程。选择 httpOnly cookie 存储
refresh token 以避免 XSS 攻击风险。
```

```
fix(api): fix null pointer exception in user query

之前代码假设 user 对象始终存在，但在未登录状态下会导致
崩溃。通过提前判空并返回统一错误格式修复此问题。
```

```
refactor(db): extract query builder into standalone module

查询构建代码分散在 4 个 repository 文件中导致重复维护。
集中管理后更易于后续添加查询缓存机制。
```
