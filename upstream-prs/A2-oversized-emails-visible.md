**Title:** `fix: oversized messages vanish from the index without a trace`

**Branch:** `iret77:fix/oversized-emails-visible` · **Depends on:** nothing (⊘)

---

`parse_emlx()` refuses a file above a size limit — correct, since an unbounded
read is a memory DoS on a mailbox holding a 400 MB attachment. But it refused
*quietly*: the message never appeared in the index, `disk_email_count` sat
permanently above `email_count`, and nothing explained the difference.

### What changed

- The limit becomes configurable: `APPLE_MAIL_INDEX_MAX_EMAIL_MB` /
  `[index] max_email_mb`, default 25.
- A skipped file is recorded in the dead-letter queue (`error_type = "Skipped"`,
  `error_message = "too_large"`), so the **existing** `failed_jobs_count` in
  `index://status` and `apple-mail-mcp status` counts it. No new tool, no new
  field — the gap becomes explainable through what is already there.
- A later re-parse that succeeds clears the row.
- Registers `max_email_mb` in `CONFIG_SCHEMA` and the `config.toml` template.
  Without the schema entry the loader rejects the documented key as unknown, so
  setting it would refuse to start the server. Tests cover that the key
  validates and that a nonsense value (`0`, `"not-a-number"`) falls back to the
  default rather than skipping every message.

### Worth pushing back on

Whether "skipped on purpose" belongs in the same table as "failed to parse". They
are different events, and a future `skipped_count` split would be cleaner — but
reusing `failed_index_jobs` keeps this PR to zero schema changes and zero new
status fields, and `error_message` already distinguishes them.

### Changelog

```markdown
### Fixed

- **Oversized messages are no longer dropped silently.** `parse_emlx()` refuses
  files above a size ceiling (an unbounded read is a memory DoS on a mailbox with
  a 400 MB attachment), but the refusal left no trace: the message was simply
  absent from the index and `disk_email_count` stayed above `email_count` with
  nothing to explain it. Skipped files now land in the dead-letter queue as
  `too_large`, so the existing `failed_jobs_count` accounts for them, and the
  ceiling is configurable via `APPLE_MAIL_INDEX_MAX_EMAIL_MB` / `[index]
  max_email_mb` (default 25). The TOML key is also registered in `CONFIG_SCHEMA`
  — without it the loader would reject the documented key as unknown.
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
  --head iret77:fix/oversized-emails-visible \
  --title "fix: oversized messages vanish from the index without a trace" \
  --body-file upstream-prs/A2-oversized-emails-visible.md
```
