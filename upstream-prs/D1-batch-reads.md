**Title:** `feat: get_email accepts a list of ids`

**Branch:** `iret77:feat/batch-reads` · **Depends on:** nothing (⊘)

---

The cost of reading mail here is the round-trip, not the read: a single message
comes off disk in 1-5 ms. So any task that touches a page of messages — triage, a
survey of what is flagged, summarising a thread — spends nearly all its time in
call overhead. Fifty-seven messages meant fifty-seven calls to move a few hundred
kilobytes.

### What changed

`get_email` also takes a list of up to 50 ids and returns one entry per
**distinct** id, in the order given, each either `{"ref", "email"}` or
`{"ref", "error"}`. A repeated id is read once — the same id twice is the same
message — so the result can be shorter than the input; if you would rather have
positional alignment with the request, say so and I will keep the duplicates.

- **One unreadable message never sinks the batch** — the caller asked about 50
  messages, and 49 answers are worth more than one exception.
- **Duplicates are read once**, which is why the answer is one entry per
  distinct id rather than per submitted element.
- **A single id keeps its old single-object shape**, so existing callers and their
  parsers are untouched.
- The cascade moves into `_get_email_by_id()` unchanged; `get_email` becomes the
  front end that drives it.

### Worth pushing back on

- **The cap is about context, not speed** — 50 full message bodies is already a
  lot of text for a model — and the error says so, otherwise the caller cannot
  tell whether retrying differently would help.
- The batch runs the strategy cascade per id, concurrently via `asyncio.gather`.
  For ids the index can place that is 50 cheap disk reads; for ids that fall
  through to Strategy 3 it is 50 mailbox scans, which is slow. A shared-scan
  optimisation is possible but was deliberately left out to keep the diff small.

### Changelog

```markdown
### Added

- **`get_email()` accepts a list of up to 50 ids.** A single message comes off disk
  in 1-5 ms, so any task touching a page of messages spent nearly all its time in
  round-trips — 57 messages meant 57 calls. A list returns one `{"ref", "email"}`
  or `{"ref", "error"}` entry per id in the order given, so one unreadable message
  never sinks the batch; duplicates are read once, and a single id keeps its
  previous single-object shape.
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
  --head iret77:feat/batch-reads \
  --title "feat: get_email accepts a list of ids" \
  --body-file upstream-prs/D1-batch-reads.md
```
