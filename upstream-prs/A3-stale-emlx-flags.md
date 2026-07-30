**Title:** `fix: get_email reports stale read/flagged state`

**Branch:** `iret77:fix/stale-emlx-flags` · **Depends on:** nothing (⊘)

---

`get_email()` Strategy 0 reads `read` and `flagged` from the `.emlx` plist
footer's flags bitmask. Mail does not reliably rewrite that file when the state
changes — a message read on a phone, or read in Mail without the file being
touched, keeps its old bits for as long as the file lives. So the fast path
returns state that can be arbitrarily old, while Strategies 1–3 (JXA) return the
truth. Same tool, same arguments, two different answers depending on which
strategy happened to win.

### What changed

- `fetch_message_flags()` in `envelope_direct.py`: one read-only `SELECT` against
  Apple's Envelope Index for `(read, flagged)`.
- Strategy 0 overlays both fields from it, keeping the footer parse as the
  fallback for when that database cannot be read (no Full Disk Access, or an
  unsupported layout) — so the fast path degrades to the old behaviour rather
  than failing.
- CLAUDE.md's cascade description said the footer was the source; it now records
  why it is not.

### Worth pushing back on

The overlay costs one extra SQLite open per `get_email` on the disk path. It is
read-only, hits an indexed lookup, and the disk path is already the ~1-5 ms one,
so the added latency is small — but it is not free, and a batch read multiplies
it.

### Changelog

```markdown
### Fixed

- **`get_email()` no longer returns stale read/flagged state.** The disk fast
  path read both fields from the `.emlx` plist footer, which Mail does not
  reliably rewrite when the state changes — a message read on another device kept
  its old bits, so the same call answered differently depending on which strategy
  served it. Both fields are now overlaid from Apple's Envelope Index (one
  read-only SELECT), with the footer as the fallback when that database is
  unreadable.
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
uv run pytest -q                # 498 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/stale-emlx-flags \
  --title "fix: get_email reports stale read/flagged state" \
  --body-file upstream-prs/A3-stale-emlx-flags.md
```
