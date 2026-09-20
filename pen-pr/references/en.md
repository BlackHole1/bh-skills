# Body language: English

The body is English. The title stays English in both languages.

## Examples

The common case: a small fix, one sentence, no citation. The diff is short enough to read, so a link would add nothing.

**Title:** `fix(api): guard null user in lookup handler`

````markdown
Unauthenticated requests crashed `getUser()`, which assumed `user` was always set. It now returns the standard 401 instead.

Closes #271
````

A cause the reviewer would not spot. The one citation points at `merge_base`, so the block on screen is the broken one, and the prose says why.

**Title:** `fix(pool): release connections when a request is cancelled`

````markdown
A cancelled request returns before `release()` is reached, so its connection stays checked out and the pool starves under load.

https://github.com/acme/api/blob/3b1d7c40a95e2f8b6d0c4a17e93f5b28c6a0d114/src/pool/handler.ts#L88-L94

The release now runs in a `finally`, and a regression test drives 200 cancellations through a pool of 10.

Closes #402
````

An intricate implementation. The citation points at `head_sha`, the branch's own code, and names what to look at.

**Title:** `feat(auth): rotate refresh tokens on every use`

````markdown
Refresh tokens were static, so one lifted from storage stayed valid for its full 30 days. Each call to `AuthService.refresh()` now issues a new token and revokes the previous one, and reusing a revoked token invalidates the whole family.

Rotation, revocation, and the reuse check share a single transaction, which is the part worth a close read:

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
