**Title:** `feat: set_flag() and set_read_status() — the first write tools`

**Branch:** `iret77:feat/write-tools` · **Depends on:** nothing (⊘)

**Tool count:** 8 → 10 · **Hook:** `APPLE_MAIL_READ_ONLY` from #80 finally has
something to guard

---

Both tools take one id or a list (max 500) and return per-id buckets — `updated`,
`unchanged`, `not_found`, `skipped_hidden`, and on trouble `failed` + `error` +
`hint`. **A batch never fails as a whole**: every id the caller passed comes back
in exactly one bucket, because a partial success is information and an exception is
not.

### Design points worth reviewing

- **A Mail.app id is a per-mailbox ROWID**, so `byId()` needs to be told which
  mailbox. `_resolve_write_targets()` places each id via the index's location
  resolver (the machinery `get_email` Strategy 2 already uses), then an explicit
  `account` + `mailbox` hint, then a bounded all-mailbox scan of a visible account
  — mirroring Strategy 3, so writes work with no index at all.
- **Located and scan groups run in separate `osascript` calls.** A slow or
  timed-out scan must not discard the fast, precise writes that already succeeded.
- **`applyToMessage()` re-verifies `msg.id()` before writing.** The id array is a
  snapshot; if mail arrives or is filed between fetching it and writing, positions
  shift and the same index points at a different message. On a mismatch it
  re-resolves by id and skips rather than writing to the wrong mail.
- **No-ops are skipped, from live state.** Every write is a server round-trip on
  IMAP/Exchange (and rotates the Exchange ItemId), so `unchanged` is a deliberate
  outcome — and the current state is read from Mail, never from an index that could
  be stale.
- **`not_found` stays a statement about the MESSAGE.** Anything that went wrong on
  the way — no such account, an unreadable mailbox, Apple Events refused — goes to
  `failed` with its reason and a cause-specific hint. Merging the two made a broken
  account lookup look like a batch of deleted mail. The scan also counts what it
  skipped, so a miss after a capped scan is not reported as a deletion.
- **Excluded accounts (#90) never reach JXA.** An id resolving into a hidden
  account goes to `skipped_hidden`; naming a hidden `account` skips the whole batch
  at the entry gate.
- **Nothing untrusted is interpolated into JS.** The mutating statement comes from
  the operation and a validated int/bool; ids and names cross over only through
  `json.dumps`.
- **Flag colours** are Apple's seven (`msg.flagIndex` 0-6), with `color="none"` to
  unflag and `"default"` to flag without forcing a colour. The server attaches **no
  meaning** to any colour — what a colour stands for is the user's own convention,
  and the docstring tells the model to ask rather than assume.

Your existing regression test for write-implying tool names already covers both
new tools, since they start with `set_`.

### Worth pushing back on

- **The batch ceiling is 500.** That is a lot of Apple Events in one call; the cost
  is per-group rather than per-id (one `osascript` per `(account, mailbox)`), but a
  500-id batch spanning many mailboxes is still slow. Lower it if you prefer.
- **`unchanged` versus `updated` requires reading current state before writing**,
  which costs one property read per message. Skipping the check would make every
  call a write, which on IMAP means a server round-trip per message.

### Changelog

```markdown
### Added

- **Write tools: `set_flag()` and `set_read_status()`.** The first tools that
  change mail state, and what `APPLE_MAIL_READ_ONLY` (#80) now guards. Both take a
  single id or a batch (max 500) and return per-id buckets — `updated`,
  `unchanged`, `not_found`, `skipped_hidden`, plus `failed` with a cause-specific
  hint — so a batch never fails as a whole. Flag colours cover Apple's seven, with
  `none` to unflag and `default` to flag without forcing a colour; the server
  attaches no meaning to any colour. Ids are placed via the index, an explicit
  `account`/`mailbox` hint, or a bounded mailbox scan, so writes work without an
  index. `not_found` means only "Mail was reachable and the message was not
  there": an unreachable account, an unreadable mailbox or a refused Apple Events
  permission goes to `failed` instead, and a scan that hit its cap says so rather
  than implying deletion. Ids resolving into an excluded account (#90) are never
  dispatched to Mail.
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
uv run pytest -q                # 517 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/write-tools \
  --title "feat: set_flag() and set_read_status() — the first write tools" \
  --body-file upstream-prs/C3-write-tools.md
```
