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

### The cursor is `(before, before_id)`

Mail stores whole seconds, so a timestamp alone is not a position. Reproduced:
three messages received in the same second with `limit=2` — page one returns
two, page two with `before=<oldest date_received>` returns **nothing**, and the
third message is unreachable forever. That is the defect this unit exists to
remove, reintroduced at the page boundary.

So `before_id` carries the ROWID tie-break, the ordering becomes
`date_received DESC, ROWID DESC` to give it a defined meaning, and both values
are fields of the last row the caller already saw — no new concept. `before`
alone still works where timestamps are distinct; `before_id` without `before` is
refused, because silently ignoring it returns the newest page and a backwards
walk reads that as "start again".

### Worth pushing back on

- Four new parameters on an already wide tool. All optional, all defaulting to
  today's behaviour — but if you would rather have one opaque `cursor` string
  than `before` + `before_id`, that is a reasonable trade and easy to change.
- The tie-break assumes ROWID order agrees with arrival order within one second,
  which holds for Apple's Envelope Index but is an assumption.

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
uv run pytest -q                # 502 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/mailbox-pagination \
  --title "feat: get_emails(before=, after=, offset=) — reach the backlog" \
  --body-file upstream-prs/D3-mailbox-pagination.md
```
