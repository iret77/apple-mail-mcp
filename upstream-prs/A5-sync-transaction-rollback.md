**Title:** `fix: a failed sync leaves its transaction open and wedges every write`

**Branch:** `iret77:fix/sync-transaction-rollback` · **Depends on:** nothing (⊘)

---

Python's `sqlite3` holds an implicit transaction open until commit or rollback. A
sync that raises mid-run therefore leaves one open on a connection the manager
keeps — and **every later write fails with "database is locked" until the process
restarts.** From outside the index looks dead while nothing is actually wrong
with it.

### What changed

`sync_updates()` rolls back before re-raising. The rollback is best-effort (it can
fail too, on a connection that is already broken) and the original exception is
what propagates, so the caller still sees the real cause.

### Worth pushing back on

Nothing in the approach — but note this is the first of a small `manager.py`
stack: A6 (per-thread connections) builds on it, and A7/A8 on that. They are
separate PRs so each can be judged on its own; if this one is declined the rest
rebase off it easily.

### Changelog

```markdown
### Fixed

- **A failed sync no longer wedges every later write.** `sqlite3` keeps an
  implicit transaction open until commit or rollback, so a sync that raised
  mid-run left one open on the manager's connection and every subsequent write
  failed with "database is locked" until the process restarted — the index looked
  dead while nothing was wrong with it. The sync path now rolls back on failure
  before re-raising.
```

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 495 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/sync-transaction-rollback \
  --title "fix: a failed sync leaves its transaction open and wedges every write" \
  --body-file upstream-prs/A5-sync-transaction-rollback.md
```
