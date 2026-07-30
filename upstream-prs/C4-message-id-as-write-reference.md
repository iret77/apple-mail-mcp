**Title:** `feat: the Message-ID is a reference everywhere, not just an output`

**Branch:** `iret77:feat/message-id-as-write-reference` · **Depends on:** **C2**
(and through it C1), **C3**, **A9** — all contained in this PR

---

With the header exposed in read results and the write tools in place, this closes
the loop: every tool that takes a message accepts the RFC822 `Message-ID` as well
as the numeric id — `get_email`, `get_email_links`, `get_email_attachment`,
`get_attachment`, `set_flag`, `set_read_status`.

### The rule that makes it worth having

> **A header is never translated back into a ROWID and then trusted.**

An index row can be stale, and by then its ROWID may belong to a *different*
message.

- **Writes** match `msg.messageId()` in JXA (`applyByHeader`). The index is
  consulted only to pick the account and to order the mailbox scan
  (`prefer_mailboxes`), so a stale row can misdirect the search but can never write
  the wrong message. Header groups run in their own `osascript` call and are echoed
  back as headers.
- **Reads** fetch each candidate the index offers and **verify** the header on what
  came back, moving to the next candidate on a mismatch and raising rather than
  returning a stranger's mail.
- **The index orders the search; it never limits it.** A header is looked for in
  the account the index points at FIRST, then in every other visible account. Group
  order is insertion order — sorting the groups by name throws that priority away.
- **A header the index cannot place searches EVERY visible account.** Not exotic:
  it is every message that arrived after the last sync. The JXA script retires a
  header globally (`settled`) the moment it lands, so fanning out never writes two
  copies and never reports a miss another account already settled.
- **A recovered write never lands in a discard mailbox** unless the index expects
  the message there. The same mail often still sits in Trash after being re-filed,
  and flagging that copy would leave the visible one untouched — while a message
  that genuinely lives in Junk is still a legitimate target. Trash and junk are
  decided by role (**this is why A9 is a dependency**), and skipped mailboxes are
  counted, because "deliberately not searched" is still not searched.

**Angle brackets are not part of the identity.** The `.emlx` header keeps them
(`<a@b>`), Apple Mail's `messageId` property drops them (`a@b`). Every comparison
goes through `_header_key()` in Python or `normHeader()` /
`MailCore.normHeaderValue()` in JS, and `find_by_rfc822()` matches either stored
form plus a normalized fallback for folded headers. A strict comparison here fails
*silently*: nothing throws, so nothing is logged, and every Message-ID lookup
reports a missing message that is sitting in the mailbox. That cost a debugging
session.

Two smaller pieces of the same story: `get_email` reports the current `account`
and `mailbox` (addressing by header makes the location the one thing the caller
cannot derive), and a stringified list or HTML-escaped brackets are unwrapped —
`'["<a@b>"]'` taken literally is a Message-ID nothing will ever match.

### Worth pushing back on

- **`&amp;` is deliberately NOT decoded** while `&lt;`, `&gt;` and `&quot;` are.
  `&` is legal in a Message-ID local part, so `<a&amp;b@x>` and `<a&b@x>` can both
  exist as distinct messages, and decoding would aim the write at the other one
  *while reporting success*. A reference that fails to match is recoverable; a
  write to the wrong message is not.
- **The header path walks mailboxes**, so it costs a scan rather than a direct
  address. It is bounded by the Strategy 3 mailbox cap and timeout, and the index
  orders the walk so the first mailbox is usually the right one — but a header for
  an unindexed message in a many-mailbox account is genuinely slow.
- This is the largest PR of the set. If you would rather see it split, the natural
  seam is reads (verify-on-fetch) versus writes (match-in-JXA).

### Changelog

```markdown
### Added

- **The RFC822 `Message-ID` is accepted as a message reference** by `get_email`,
  `get_email_links`, `get_email_attachment`, `get_attachment`, `set_flag` and
  `set_read_status` — the identifier that survives another device filing the
  message elsewhere, which a numeric ROWID does not. A header is never translated
  back into a ROWID and then trusted: writes match `msg.messageId()` in JXA, reads
  verify the header on the message that comes back and move to the next candidate
  on a mismatch, and a header the index cannot place is searched for in every
  visible account rather than only the default one. Angle brackets are normalized
  on both sides (the `.emlx` header keeps them, Apple's `messageId` drops them — a
  strict comparison silently matched nothing). `get_email` also reports the
  message's current `account` and `mailbox`.
```

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 584 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/message-id-as-write-reference \
  --title "feat: the Message-ID is a reference everywhere, not just an output" \
  --body-file upstream-prs/C4-message-id-as-write-reference.md
```
