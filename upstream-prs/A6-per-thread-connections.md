**Title:** `fix: one SQLite connection per thread, not one per manager`

**Branch:** `iret77:fix/per-thread-connections` · **Depends on:** **A5**
(`fix/sync-transaction-rollback`) — same file, and this PR contains it

---

`IndexManager` held a single connection guarded by one lock. `sqlite3` connections
are not safe to share across threads, and the lock made that safety expensive: a
background rebuild holds its transaction for minutes, so every request that
needed the index blocked behind it. From outside the server looked frozen while
it was working exactly as designed.

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

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 498 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/per-thread-connections \
  --title "fix: one SQLite connection per thread, not one per manager" \
  --body-file upstream-prs/A6-per-thread-connections.md
```
