**Title:** `feat: schema v6 — store the RFC822 Message-ID`

**Branch:** `iret77:feat/stable-identity-schema` · **Depends on:** nothing (⊘)

---

**Pure storage. No behaviour change, no new lookup, no tool surface change** — and
the prerequisite for everything else in this track.

`emails.message_id` is Mail.app's id, and Mail.app's id is a **per-mailbox
ROWID**. It is exact while the message stays put and dead the moment the message
is filed elsewhere — which happens routinely, since most accounts are also open on
a phone and a tablet. After such a move the index still holds a row, the ROWID in
it now belongs to a *different* message, and nothing in the schema can tell the
difference.

### What changed

- `emails.rfc822_message_id` plus an index on it.
- Populated by every write path: the bulk build, the disk sync, the file watcher.
  `parse_emlx()` already extracted the header — it was simply dropped on the floor.
- Migration v5→v6 as an in-place `ALTER TABLE`.

### Worth pushing back on

- **In-place ALTER, not a rebuild.** A 70k index must not have to be re-read just
  to open. The cost is that existing rows keep `NULL`, which every future lookup
  has to treat as "unknown" and fall back to the ROWID path — so a partially
  migrated index stays correct, just less able to recover moved messages. A full
  rebuild backfills them.
- **A missing header is stored as `NULL`, never `""`** — an empty string would
  match a query for the empty header and hand back a stranger's message.

### Changelog

```markdown
### Added

- **Schema v6 stores the RFC822 `Message-ID`.** `emails.message_id` holds
  Mail.app's id, which is a per-mailbox ROWID: it stops identifying the message
  the moment any device files it elsewhere, and the row then points at a different
  message. The new `rfc822_message_id` column (indexed, populated by the bulk
  build, the disk sync and the watcher) is stable across moves. The v5→v6
  migration is an in-place `ALTER TABLE` so a large index still opens instantly;
  pre-existing rows keep `NULL` until re-indexed and fall back to the ROWID path.
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
  --head iret77:feat/stable-identity-schema \
  --title "feat: schema v6 — store the RFC822 Message-ID" \
  --body-file upstream-prs/C1-stable-identity-schema.md
```
