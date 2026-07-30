**Title:** `fix: serialize index writes across processes, not just threads`

**Branch:** `iret77:fix/cross-process-write-lock` · **Depends on:** **A6** (and
through it A5) — same file, both contained in this PR

**Addresses:** #106

---

A `threading.Lock` is not enough. Claude Desktop starts a **second instance** of
every MCP server (#106), so two processes hold connections to the same SQLite
file. SQLite allows one writer, and a rebuild holds its transaction for minutes —
the other process then dies on "database is locked" once `busy_timeout` expires.
That is how a rebuild failed here in practice, and it is the same root cause as
the report in #106.

### What changed

`WriteLock` pairs a thread lock with an advisory `flock` on `<index>.lock`:

- The **file lock** is process-wide, and the OS releases it if a process dies, so
  a crash cannot wedge the index permanently.
- The **thread lock** still guards threads inside one process, where `flock` would
  not — they share the same file description.

It is acquired **non-blocking**, so a second caller is told "already running"
(`IndexBusyError`) instead of queueing behind a multi-minute rebuild, and it is
released **last**, after the final flush and the FTS rebuild — the heaviest
writes in the program. Releasing early let a waiting sync grab the lock, whose
open transaction then made those writes fail from inside the `finally`.

### Worth pushing back on

`flock` is advisory and Unix-only. That is fine here (the project is macOS-only),
but it means a third-party process writing the same file is not stopped. The
alternative — a lock table inside SQLite — cannot be taken before the connection
that needs it, so it does not solve the same problem.

### Changelog

```markdown
### Fixed

- **Two server instances no longer fight over the index.** Claude Desktop starts
  a second copy of every MCP server, so two processes wrote the same SQLite file;
  SQLite allows one writer, and a rebuild holds its transaction for minutes, so
  the other process died on "database is locked" after `busy_timeout`. Index
  writes are now serialized by a cross-process advisory lock (`flock` on
  `<index>.lock`) paired with the existing thread lock — acquired non-blocking, so
  a second caller is told the build is already running instead of queueing, and
  released only after the final flush and FTS rebuild. (#106)
```

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 502 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/cross-process-write-lock \
  --title "fix: serialize index writes across processes, not just threads" \
  --body-file upstream-prs/A8-cross-process-write-lock.md
```
