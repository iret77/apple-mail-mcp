**Title:** `feat: refresh the index without restarting the server`

**Branch:** `iret77:feat/refresh-index-tool` · **Depends on:** nothing (⊘)

**Tool count:** 8 → 9

---

The index syncs when the server starts and never again. A client that stays open
for a day drifts: a message that arrived an hour ago is not in the index,
`search` cannot find it, and the only way out was to restart the client.

### What changed

`refresh_index(full=False)` runs the normal disk sync and returns the change
count. `full=True` discards the index and rebuilds; that takes minutes on a large
mailbox, so it runs detached.

Two details are the point of the tool rather than incidental to it:

- **A detached build that never started must not report "started".** The first
  version answered "started" the moment the thread was spawned, so a build
  refused on its first line looked identical to a running one — and since nothing
  was building, the status said "ready" forever with no explanation. `on_started`
  fires once the build is really under way and the tool waits up to
  `BUILD_START_TIMEOUT` for it; no signal plus an exception in hand means
  `failed`, with the reason.
- **The docstring deliberately claims the vocabulary users actually use** —
  "rebuild", "re-index", "from scratch", "recreate" — *and* states that this is
  the server's own FTS5 index, not Apple Mail's envelope index, and nothing to do
  with Mail.app's "Mailbox > Rebuild" menu item. Without that, a model asked to
  "rebuild the mail index" sends the user into Mail.app's menus, which does
  something else entirely and takes hours. That happened live before the wording
  was added.

### Worth pushing back on

The tool is allowed in read-only mode, on the grounds that it touches only the
local index and never the mail. If you read `APPLE_MAIL_READ_ONLY` as "change
nothing at all", that is a one-line change.

### Changelog

```markdown
### Added

- **`refresh_index(full=False)` — update the index without restarting.** The index
  synced only at startup, so a long-running client drifted: a message that
  arrived during the session could not be found and the only remedy was a
  restart. `full=True` discards and rebuilds in the background. The tool reports
  `already_running` / `failed` rather than a blind "started" — it waits for
  confirmation that the detached build actually began, because a build refused on
  its first line otherwise looked identical to a running one. Touches only the
  local index, so it is permitted in read-only mode.
```

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 497 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/refresh-index-tool \
  --title "feat: refresh the index without restarting the server" \
  --body-file upstream-prs/B3-refresh-index-tool.md
```
