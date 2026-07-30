**Title:** `fix: one SQLite connection per thread, not one per manager`

**Branch:** `iret77:fix/per-thread-connections` · **Depends on:** **A5**
(`fix/sync-transaction-rollback`) — same file, and this PR contains it

---

`IndexManager` held a single connection shared by every thread. `sqlite3`
connections are not safe to share that way, and here the sharing was not merely
unsafe in theory: a background rebuild holds its transaction for minutes, and
every request that used the index during that window ran on the same connection
— so it either serialized behind the rebuild's statements or read the rebuild's
uncommitted, half-emptied state. From outside, the server looked frozen or
inexplicably empty while it was working exactly as designed.

(To be precise about what the lock did and did not do: `_conn_lock` guarded
creation of the connection, not the queries running on it.)

### What changed

- `self._local = threading.local()`; `_get_conn()` opens one connection per
  thread.
- Every opened connection is remembered in `_open_conns` so `close()` can shut all
  of them — a `threading.local` alone would leak the others.
- The connection lock now guards only that bookkeeping, not the queries.

### Worth pushing back on

More connections mean more file descriptors and more SQLite page caches. For an
MCP server with a handful of threads that is a good trade against a
minutes-long stall, but if you expect many short-lived threads a pool would be
the better shape.

### Changelog

```markdown
### Fixed

- **A background rebuild no longer blocks every index read.** `IndexManager` used
  one SQLite connection guarded by a single lock, so a rebuild — which holds its
  transaction for minutes — stalled every request that needed the index, and the
  server looked frozen while working as designed. Connections are now per thread
  (`threading.local`), with every opened connection tracked so `close()` still
  shuts all of them.
```


### Changelog and version

Deliberately not in the diff. Upstream's changelog entries carry issue numbers
under a release heading the maintainer owns, and twenty-two prepared branches
each editing the same `[Unreleased]` block would conflict twenty-two ways and
force a rebase after every merge. The prose is above, ready to paste; the
release number is yours to choose.

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 501 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/per-thread-connections \
  --title "fix: one SQLite connection per thread, not one per manager" \
  --body-file upstream-prs/A6-per-thread-connections.md
```
