# Body language: English

Subject and body are both English.

## Examples

A change that explains itself needs no body at all.

```text
chore: bump version to 2.3.1
```

A typical fix. One paragraph, written as one continuous line, with the path in underscores and only real code in backticks.

```text
fix(api): guard null user in lookup handler

Unauthenticated requests crashed because the handler assumed `user` was always set. It now checks for null and returns the standard 401 response, matching the shape the rest of _src/api/_ already uses.
```

A multi-part why, split into cause, effect, and fix.

```text
fix(pool): release connection on context cancel

When a caller cancelled the request context mid-query, `Pool.Acquire()` returned but the deferred `conn.Release()` never ran, because the cancellation unwound the stack before the defer was registered.

Over a sustained burst of cancelled requests the pool leaked one connection each time and eventually blocked every caller inside `Acquire()`, since all `MaxConns` connections had been handed out and none came back. The symptom looked like a deadlock but was really exhaustion.

Register the release with `defer` immediately after a successful acquire, before any work that can observe cancellation, so the connection returns to the pool on every path.
```

A breaking change, flagged twice: `!` in the subject and a `BREAKING CHANGE` footer.

```text
feat(api)!: replace positional args in createUser with options object

The positional signature had grown to five arguments and was easy to misorder. An options object makes each field explicit at the call site, and every caller under _src/_ is updated.

BREAKING CHANGE: `createUser(name, email, role)` is now `createUser({ name, email, role })`.
```
