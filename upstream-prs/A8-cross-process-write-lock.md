**Title:** `fix: serialize index writes across processes, not just threads`

**Branch:** `iret77:fix/cross-process-write-lock` · **Depends on:** **A6** (and
through it A5) — same file, both contained in this PR

**Related:** #106 — see the scope note below

---

A `threading.Lock` is not enough. Claude Desktop starts a **second instance** of
every MCP server (#106), so two processes hold connections to the same SQLite
file. SQLite allows one writer, and a rebuild holds its transaction for minutes —
the other process then dies on "database is locked" once `busy_timeout` expires.
That is how a rebuild failed here in practice.

### What changed

`WriteLock` pairs a thread lock with an advisory `flock` on `<index>.lock`:

- The **file lock** is process-wide, and the OS releases it if a process dies, so
  a crash cannot wedge the index permanently.
- The **thread lock** still guards threads inside one process, where `flock` would
  not — they share the same file description.

It is acquired **non-blocking**, so a second caller is told "already running"
(`IndexBusyError`) instead of queueing behind a multi-minute rebuild. Within
`build_from_disk()` it is released **last** — after the final flush and the FTS
rebuild, the heaviest writes in the program. Releasing early let a waiting sync
grab the lock, whose open transaction then made those writes fail from inside the
`finally`.

`rebuild()` is the one place that hands the lock over rather than holding it
throughout: it takes the lock for its own DELETE, releases it, and calls
`build_from_disk()`, which takes it again. There is a gap between the two, so
`rebuild()` is serialized against other writers in each half rather than as one
atomic operation. Closing that would mean a reentrant lock; it did not seem worth
the machinery, but say the word.

Every acquire is paired with a `finally`, including the paths that can fail
before any work starts — an unreadable mail directory, a JXA failure while
resolving exclusions. Leaking it once wedges every later build and sync with
"already running" until the process restarts.

### Scope against #106 — what this does *not* fix

#106 reports two `--watch` instances in a reconciliation ping-pong: WAL growth to
74 MB in 13 minutes and FTS queries blocked for minutes. This PR shares that
issue's root cause — Claude Desktop spawning two instances that write the same
SQLite file — and removes the "database is locked" failure and the concurrent
writers behind the WAL storm, because a second caller is told the work is already
running instead of queueing.

**But it does not cover the watcher path.** `WriteLock` is taken by
`build_from_disk()` and `sync_updates()`; `watcher.py` writes without it, so two
watchers can still duplicate each other's work. Making the watcher take the lock
needs its own decision — a non-blocking acquire there would *drop* a batch of file
events rather than defer it, so the queue would have to survive the skip — and I
did not want to improvise that into this PR. Happy to do it as a follow-up if you
want it, in whichever shape you prefer.

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
  released only after the final flush and FTS rebuild, and taken by `rebuild()`
  before its first DELETE. The watcher path is not yet covered — see #106. (#106)
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
uv run pytest -q                # 507 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/cross-process-write-lock \
  --title "fix: serialize index writes across processes, not just threads" \
  --body-file upstream-prs/A8-cross-process-write-lock.md
```
