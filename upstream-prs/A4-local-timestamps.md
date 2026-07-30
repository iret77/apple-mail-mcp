**Title:** `fix: present timestamps in local time, not UTC`

**Branch:** `iret77:fix/local-timestamps` · **Depends on:** nothing (⊘)

---

Every timestamp went out as UTC while Mail.app shows local time. On this machine
that is 12:54 for a message the user sees at 14:54, so any question of the form
"is this from this morning?" gets the wrong answer, and an assistant summarising
a day's mail is off by the offset throughout.

### What changed

- `to_local_iso()` converts at every output boundary — `date_received` and
  `date_sent` in `get_email`, `get_emails` and the search paths.
- Conversion uses `astimezone()` with the **system** zone, so DST is correct for
  the date of the message rather than for today. No hardcoded offset.
- Storage stays UTC. Converting on the way in would make a stored value
  un-reinterpretable later.

### Worth pushing back on

This is a visible output change for existing clients: anyone parsing the ISO
string and assuming UTC will now be off by the offset in the other direction. The
strings carry their offset (`+02:00`), so a correct parser is unaffected — but a
naive one is. Worth a changelog note under **Changed** rather than **Fixed** if
you prefer to treat it that way.

### Changelog

```markdown
### Fixed

- **Timestamps are reported in local time.** All `date_received` / `date_sent`
  values went out as UTC while Mail.app displays local time — 12:54 for a message
  the user sees at 14:54 — so every "is this from this morning?" question was
  answered against the wrong clock. Conversion now happens at the output boundary
  using the system time zone (DST-correct for the date of the message, not for
  today); storage stays UTC.
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
uv run pytest -q                # 499 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/local-timestamps \
  --title "fix: present timestamps in local time, not UTC" \
  --body-file upstream-prs/A4-local-timestamps.md
```
