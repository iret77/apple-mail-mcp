**Title:** `feat: get_emails(before=, after=, offset=) — reach the backlog`

**Branch:** `iret77:feat/mailbox-pagination` · **Depends on:** nothing (⊘)

---

`get_emails` returned the newest N per mailbox and nothing else, so anything older
than one page was **unreachable**. Not slow — unreachable. A run that stored a
cursor deep in a mailbox could not get back to it, and `search()` is no substitute:
it needs keywords, and a gapless reverse scan has none.

### What changed

The Envelope Index can answer this with two more `WHERE` clauses and an `OFFSET`,
so `fetch_recent_messages()` gains `before`, `after` and `offset`, and the tool
exposes them as ISO dates.

- **`before` is the cursor to prefer**: hand it the oldest `date_received` you have
  seen and the walk continues from there, unaffected by mail arriving mid-walk.
- **`offset` shifts under new mail**, which is why the docstring names `before` for
  long walks and keeps `offset` for one snapshot.
- A naive date is read as **local** time, because that is what a caller typing a
  date means; the column stores Unix epoch.

**The JXA fallback refuses these three rather than dropping them.** That matters
more than it sounds: a silently ignored window returns the same newest N on every
page, so the caller pages forever and concludes the backlog is empty. An error is
the only honest answer when the window cannot be applied.

### Worth pushing back on

- `before`/`after` are exclusive (`<` / `>`), so handing back the oldest
  `date_received` you saw cannot re-deliver that same row. With second resolution,
  two messages sharing a timestamp at a page boundary could be skipped — a
  `(date, rowid)` keyset cursor would be exact, at the cost of a compound
  parameter.
- Three new parameters on an already wide tool. They are all optional and default
  to today's behaviour.

### Changelog

```markdown
### Added

- **`get_emails(before=, after=, offset=)` makes a mailbox walkable backwards.**
  Only the newest N messages per mailbox were reachable, so a backlog could not be
  approached at all — and `search()`, which requires keywords, is no substitute for
  a gapless reverse scan. `before` is the stable cursor (pass the oldest
  `date_received` you have seen; unaffected by new mail arriving mid-walk), while
  `offset` suits a single snapshot. Naive dates are read as local time. The JXA
  fallback refuses these parameters rather than ignoring them: a dropped window
  would return the same newest N on every page and read as an empty backlog.
```

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 500 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/mailbox-pagination \
  --title "feat: get_emails(before=, after=, offset=) — reach the backlog" \
  --body-file upstream-prs/D3-mailbox-pagination.md
```
