**Title:** `feat: search and listing results carry the RFC822 Message-ID`

**Branch:** `iret77:feat/expose-message-id-in-reads` · **Depends on:** **C1**
(contained in this PR)

---

**Useful on its own, with no write tools involved.** A client that holds on to a
message between calls currently has only `id`, and `id` is a per-mailbox ROWID. It
dies the moment any device files the message elsewhere — routine when a phone and
a tablet share the account. The caller then re-resolves it and either gets nothing
or, worse, whatever message inherited that number.

### What changed

`message_id` (the RFC822 header) travels with every result of the three tools
that return message *rows* — `search()`, `get_emails()` and `get_email()`.
(`get_email_links()` and `get_email_attachment()` return links and file content,
not messages, so there is nothing to carry it on. And `get_email()` already
returned the header before this change: what is new is that the two *listing*
paths do, which is where a caller actually collects references.)

- **FTS search** and **attachment search** read it from the `rfc822_message_id`
  column added in C1.
- **The Envelope Index fast path** takes it from our own index in **one** batched
  `get_rfc822_ids()` statement for the whole page. Apple's Envelope Index has no
  header column, so this is the only source; a per-row query would undo the reason
  that path exists. A row the index does not know yields `None` — never a wrong
  header — and a failed lookup degrades the listing instead of failing it.
- **The JXA paths** get it from `PROPERTY_SETS["standard"]`: one more bulk IPC call
  for an entire listing, not one per message.

The docstrings say which one to keep, because a model handed two identifiers will
otherwise use the shorter one.

**`MailCore.batchFetch` is hardened in the same change, not separately.** Carrying
one more property means one more bulk call that Mail could refuse on some build,
and a single refusal used to take the whole listing down. A refused property is now
padded with nulls so the caller's per-index arithmetic still lines up — while a
fetch where *nothing* could be read still raises, because an unreadable mailbox
must never read as "0 messages".

### Worth pushing back on

- The extra `messageId` property costs one additional Apple Event per JXA listing.
  Measured against the existing per-listing cost it is noise, but it is not zero.
- `message_id` is `null` for rows the index has not seen yet (a message that
  arrived after the last sync). Reporting `null` rather than falling back to a
  live JXA lookup keeps the listing fast; the alternative would make every listing
  potentially slow.

### Changelog

```markdown
### Added

- **Search and listing results carry `message_id`, the RFC822 header.** `id` is a
  per-mailbox ROWID and stops resolving as soon as any device files the message
  elsewhere, so a client holding a reference between calls had nothing durable to
  hold. `search()` (full-text and attachment scopes), `get_emails()` and
  `get_email()` now all return the header; the Envelope Index fast path resolves
  it for a whole page in one batched index lookup, and the JXA paths get it from
  the standard property set (one extra bulk call per listing, not per message).

### Fixed

- **A property Mail refuses no longer takes a whole listing down.**
  `MailCore.batchFetch()` issues one bulk call per property; a single failure
  aborted the entire fetch. Refused properties are now padded with nulls so
  per-index arithmetic still lines up, while a fetch where *no* property could be
  read still raises — an unreadable mailbox must not read as "0 messages".
```

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 514 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/expose-message-id-in-reads \
  --title "feat: search and listing results carry the RFC822 Message-ID" \
  --body-file upstream-prs/C2-expose-message-id-in-reads.md
```
