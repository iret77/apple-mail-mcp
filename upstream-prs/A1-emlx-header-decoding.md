**Title:** `fix: one undecodable header aborts the entire index sync`

**Branch:** `iret77:fix/emlx-header-decoding` · **Depends on:** nothing (⊘)

---

A non-ASCII `Subject`, `Received`, `Content-ID` or attachment filename makes
Python's `email` module return a `Header` object instead of a `str`. The first
`.strip()` on it raises `AttributeError`, and because that happens inside the
per-message loop, **the whole sync dies** — every message after the bad one stays
unindexed.

It is silent, which is what makes it expensive: the traceback only reaches
stderr, so under a desktop client nobody sees it. On my mailbox it ran for a full
day before I noticed that new mail had stopped appearing in `search()`.

### What changed

- `header_text()` and `_filename_text()` in `disk.py` guarantee a decoded `str`
  for every header the parser touches, via `make_header(decode_header(...))` with
  a raw-value fallback.
- The per-message guard in `sync.py` catches `Exception` rather than the three
  specific types it used to list. Narrowing it is what let this one through; a
  message that cannot be parsed now lands in the DLQ and the run continues.
- A guard test forbids raw `msg["..."]` access returning to the parser, because
  the failure mode is invisible from outside — it looks like "the sync stopped
  finding new mail".

### Worth pushing back on

The broadened `except Exception` is deliberate but debatable: it can mask a
genuine bug in the parser as a per-message skip. The DLQ row (with the exception
type and message) is what keeps that visible, and `failed_jobs_count` in
`index://status` surfaces the count.

### Changelog

```markdown
### Fixed

- **One undecodable header no longer aborts the whole index sync.** A non-ASCII
  `Subject`, `Received`, `Content-ID` or attachment filename makes Python's
  `email` module hand back a `Header` object instead of a `str`; the first
  `.strip()` raised `AttributeError` inside the per-message loop and every later
  message went unindexed. Since the traceback only reached stderr, the symptom
  was "search stopped finding new mail" with nothing to point at. Header access
  now goes through decoding helpers that guarantee a `str`, the per-message guard
  in `sync.py` catches `Exception` rather than three specific types, and a guard
  test forbids raw header access returning to the parser.
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
  --head iret77:fix/emlx-header-decoding \
  --title "fix: one undecodable header aborts the entire index sync" \
  --body-file upstream-prs/A1-emlx-header-decoding.md
```
