# Body language: Chinese 中文

The subject stays English. The body is Chinese, apart from the spec tokens and the code identifiers, which keep their exact characters.

## Examples

A change that explains itself needs no body at all.

```text
chore: bump version to 2.3.1
```

A typical fix. One paragraph, written as one continuous line, with the path in underscores and only real code in backticks.

```text
fix(api): guard null user in lookup handler

未登录请求会导致崩溃，因为处理函数假设 `user` 一定存在。现在补上判空并返回标准的 401 响应，与 _src/api/_ 下其余接口保持一致的格式。
```

A multi-part why, split into 起因、影响、修复.

```text
fix(pool): release connection on context cancel

当调用方在查询中途取消请求 context 时，`Pool.Acquire()` 已经返回，但随后的 `conn.Release()` 从未执行，因为取消在 `defer` 注册之前就展开了调用栈。

在持续的取消请求冲击下，连接池每次都会泄漏一个连接，最终 `MaxConns` 个连接全部借出且无一归还，所有调用方都阻塞在 `Acquire()` 上。表面看像死锁，实际是连接耗尽。

在成功获取连接之后、任何可能感知取消的逻辑之前立即用 `defer` 注册释放，使连接在每条路径上都能回到池中。
```

A breaking change, flagged twice: `!` in the subject and a `BREAKING CHANGE` footer whose keyword is never translated.

```text
feat(api)!: replace positional args in createUser with options object

createUser 的位置参数已经增长到五个，调用方很容易传错顺序。改用选项对象后，每个字段在调用处都显式可读，_src/_ 下的所有调用点已同步更新。

BREAKING CHANGE: `createUser(name, email, role)` 现在改为 `createUser({ name, email, role })`，旧的位置参数调用需要迁移。
```
