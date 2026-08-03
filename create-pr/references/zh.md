# Body language: Chinese 中文

The title stays English. The body is Chinese, apart from the spec tokens and the code identifiers, which keep their exact characters.

## Examples

A small fix. One sentence, the lines that carry it, and the issue it closes.

**Title:** `fix(api): guard null user in lookup handler`

````markdown
未登录请求会让 `getUser()` 崩溃，因为它假设 `user` 一定存在。现在改为返回标准的 401 响应。

https://github.com/acme/api/blob/9f2c1ab5d0e4c7b3a86f10d2e5c9b47a3f8e6d10/src/api/user.ts#L42-L48

Closes #271
````

A feature. Two short paragraphs, a permalink at the interesting part, and one line for the migration that came with it.

**Title:** `feat(auth): rotate refresh tokens on every use`

````markdown
此前 refresh token 是静态的，一旦从存储中被窃取，在 30 天过期前都能继续使用。现在 `AuthService.refresh()` 每次调用都签发新 token 并在同一事务中作废旧的，把暴露窗口压缩到单次请求。

重复使用已作废的 token 会让整个 token 家族失效并强制重新登录，同时也是一个盗用信号。

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
