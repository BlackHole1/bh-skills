# Body language: Chinese 中文

The title stays English. The body is Chinese, apart from the spec tokens and the code identifiers, which keep their exact characters.

## Examples

The common case: a small fix, one sentence, no citation. The diff is short enough to read, so a link would add nothing.

**Title:** `fix(api): guard null user in lookup handler`

````markdown
未登录请求会让 `getUser()` 崩溃，因为它假设 `user` 一定存在。现在改为返回标准的 401 响应。

Closes #271
````

A cause the reviewer would not spot. The one citation points at `merge_base`, so the block on screen is the broken one, and the prose says why.

**Title:** `fix(pool): release connections when a request is cancelled`

````markdown
请求被取消时会在 `release()` 之前提前返回，连接一直没有归还，高并发下连接池会被耗尽。

https://github.com/acme/api/blob/3b1d7c40a95e2f8b6d0c4a17e93f5b28c6a0d114/src/pool/handler.ts#L88-L94

现在归还放进 `finally`，并补了一个回归测试，用 10 连接的池跑 200 次取消。

Closes #402
````

An intricate implementation. The citation points at `head_sha`, the branch's own code, and names what to look at.

**Title:** `feat(auth): rotate refresh tokens on every use`

````markdown
此前 refresh token 是静态的，一旦从存储中被窃取，在 30 天过期前都能继续使用。现在 `AuthService.refresh()` 每次调用都签发新 token 并作废旧的，重复使用已作废的 token 会让整个 token 家族失效。

轮换、作废和重用检查共用同一个事务，这段值得重点看：

https://github.com/acme/api/blob/9f2c1ab5d0e4c7b3a86f10d2e5c9b47a3f8e6d10/src/auth/service.ts#L88-L104

新增 `token_rotations` 表，迁移脚本见 _db/migrations/0042_token_rotation.sql_ 。

Closes #214
````

A breaking change. The contrast is the point, so a `diff` block carries it better than a permalink, and `BREAKING CHANGE` is never translated.

**Title:** `feat(api)!: replace positional args in createUser with options object`

````markdown
`createUser` 此前的五个位置参数很容易传错顺序，现在改为接收单个选项对象，_src/_ 下的所有调用点已同步更新。

```diff
-createUser(name, email, role)
+createUser({ name, email, role })
```

BREAKING CHANGE: 外部调用方需要迁移到对象形式。
````
