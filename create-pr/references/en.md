# Body language: English

The body is English. The title stays English in both languages.

## Examples

A small fix. One sentence, the lines that carry it, and the issue it closes.

**Title:** `fix(api): guard null user in lookup handler`

````markdown
Unauthenticated requests crashed `getUser()` because it assumed `user` was always set. It now returns the standard 401 instead.

https://github.com/acme/api/blob/9f2c1ab5d0e4c7b3a86f10d2e5c9b47a3f8e6d10/src/api/user.ts#L42-L48

Closes #271
````

A feature. Two short paragraphs, a permalink at the interesting part, and one line for the migration that came with it.

**Title:** `feat(auth): rotate refresh tokens on every use`

````markdown
Refresh tokens were static, so a token lifted from storage stayed valid for its full 30 days. Each call to `AuthService.refresh()` now issues a new token and revokes the previous one in the same transaction, cutting the exposure window to a single request.

Reusing a revoked token invalidates the whole token family and forces re-login, which doubles as a theft signal.

https://github.com/acme/api/blob/9f2c1ab5d0e4c7b3a86f10d2e5c9b47a3f8e6d10/src/auth/service.ts#L88-L104

Adds a `token_rotations` table in _db/migrations/0042_token_rotation.sql_.

Closes #214
````

A breaking change. The contrast is the point, so a `diff` block carries it better than a permalink, and `BREAKING CHANGE` is never translated.

**Title:** `feat(api)!: replace positional args in createUser with options object`

````markdown
`createUser` took five positional arguments and callers kept misordering them. It now takes a single options object, and every call site under _src/_ is updated.

```diff
-createUser(name, email, role)
+createUser({ name, email, role })
```

BREAKING CHANGE: external callers must migrate to the object form.
````
