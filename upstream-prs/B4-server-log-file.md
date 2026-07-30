**Title:** `feat: a log file, because the server's stderr reaches nobody`

**Branch:** `iret77:feat/server-log-file` · **Depends on:** nothing (⊘)

---

Run under a desktop client, an MCP server is a black box. Its stderr goes wherever
the client decided, which in practice is nowhere the user can reach: they see "it
doesn't work" and there is no record of why. A build that dies during startup
leaves nothing behind at all.

### What changed

The server writes to `~/.apple-mail-mcp/server.log`, rotating at 1 MB with three
backups — diagnostics must not grow without bound. `APPLE_MAIL_LOG_PATH`
relocates it; an empty value disables it. Adds a troubleshooting entry pointing
at the file.

Two details that are easy to get wrong:

- **The mode is re-applied on every open, not chmod'ed once.** On rollover,
  `RotatingFileHandler` reopens the path with plain `open()`, i.e. 0644 under the
  usual umask. The log carries mail paths and account names, so from the first
  rotation onward it would silently have become world-readable.
  `_OwnerOnlyRotatingFileHandler` opens with 0600 itself, which covers the rotated
  file too.
- **"Disabled" must be an explicit `None`.** `Path("")` normalizes to `"."`, which
  is a truthy directory, so a falsy guard never fires and the handler tries to
  open the current directory as a file.

### Worth pushing back on

It logs unconditionally to a fixed default path. A server that writes a file
nobody asked for is a legitimate objection — the alternative is defaulting to
disabled, at the cost of the diagnostics being absent exactly when they are
needed (a first run that fails). Flipping the default is one line in
`get_log_path()`.

### Changelog

```markdown
### Added

- **The server writes its own rotating log** to `~/.apple-mail-mcp/server.log` (1
  MB × 3 backups, created `0600`). Under a desktop client the server's stderr is
  not reachable, so a failed startup or a dead build previously left no trace at
  all. `APPLE_MAIL_LOG_PATH` relocates the file; an empty value disables logging.
  The file mode is re-applied on every open rather than chmod'ed once, because
  rollover reopens the path with plain `open()` — which would have made the log
  (containing mail paths and account names) world-readable from the first rotation.
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
uv run pytest -q                # 500 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/server-log-file \
  --title "feat: a log file, because the server's stderr reaches nobody" \
  --body-file upstream-prs/B4-server-log-file.md
```
