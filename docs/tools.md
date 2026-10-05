# Tools

Apple Mail MCP provides **11 MCP tools** — a consolidated API designed for AI assistants.

## Overview

| Tool | Purpose | Parameters |
|------|---------|------------|
| `list_accounts()` | List email accounts | — |
| `list_mailboxes()` | List mailboxes | `account?` |
| `get_emails()` | Get emails with filtering | `account?`, `mailbox?`, `filter?`, `limit?`, `before?`, `before_id?`, `after?`, `offset?` |
| `get_email()` | Get single email with content + attachments | `message_id`, `account?`, `mailbox?` |
| `search()` | Search emails | `query`, `account?`, `mailbox?`, `scope?`, `limit?`, `exclude_mailboxes?`, `before?`, `after?`, `highlight?` |
| `get_email_links()` | Extract links from an email | `message_id`, `account?`, `mailbox?` |
| `get_email_attachment()` | Extract attachment content | `message_id`, `filename`, `account?`, `mailbox?` |
| `set_flag()` | **Write** — flag/unflag one email or a batch (max 500), optionally by color | `message_ids`, `color?`, `account?`, `mailbox?` |
| `set_read_status()` | **Write** — mark one email or a batch read (seen) or unread (unseen) | `message_ids`, `read?`, `account?`, `mailbox?` |
| `get_index_status()` | Index health and setup diagnostics — build state, progress, and whether Full Disk Access is missing | — |
| `refresh_index()` | Update the index on demand — the index otherwise syncs only at server start | `full?` |

---

## `list_accounts()`

List all configured email accounts in Apple Mail.

**Parameters:** None

**Returns:** List of accounts with `name` and `id` fields.

```python
list_accounts()
# → [{"name": "Work", "id": "abc123"}, {"name": "Personal", "id": "def456"}]
```

---

## `list_mailboxes()`

List all mailboxes for an email account.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `account` | `string?` | env default | Account name |

**Returns:** List of mailboxes with `name` and `unreadCount` fields.

```python
list_mailboxes()
# → [{"name": "INBOX", "unreadCount": 5}, {"name": "Sent", "unreadCount": 0}]

list_mailboxes("Work")
# → [{"name": "INBOX", "unreadCount": 12}, ...]
```

---

## `get_emails()`

Get emails from a mailbox with optional filtering. This is the primary tool for listing emails.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `account` | `string?` | env default | Account name |
| `mailbox` | `string?` | `INBOX` | Mailbox name |
| `filter` | `string?` | `all` | Filter type (see below) |
| `limit` | `int?` | `50` | Max emails to return |
| `before` | `string?` | — | Only messages older than this ISO date/datetime. Naive input is read as local time |
| `before_id` | `int?` | — | The `id` of the oldest row you have already seen. The second half of a cursor — see the note below |
| `after` | `string?` | — | Only messages newer than this ISO date/datetime |
| `offset` | `int?` | `0` | Skip this many rows. Fine for a second page, but see the note |

!!! note "Walking a mailbox backwards"
    `before` + `before_id` together are a **keyset cursor**, and that pair is
    the reliable way to page into a backlog. Pass the `date_received` and the
    `id` of the last row you received; the next call resumes exactly after it.

    A timestamp alone is not a position — Mail stores whole seconds, so several
    messages can share one. Paging on `before` by itself makes every message
    that shares the oldest second of a page permanently unreachable.

    `before_id` without `before` is rejected rather than ignored: silently
    dropping it returns the newest page, which a backwards walk reads as "start
    again" and loops forever. An empty or blank `before` counts as no `before`.

    `offset` re-reads the rows it skips and shifts when new mail arrives
    mid-walk. Prefer the cursor for anything longer than one extra page.

**Filters:**

| Filter | Description |
|--------|-------------|
| `all` | All emails (default) |
| `unread` | Only unread emails |
| `flagged` | Only flagged emails |
| `today` | Emails received today |
| `last_7_days` | Emails from the last 7 days |
| `this_week` | Alias for `last_7_days` |

**Returns:** List of email summaries sorted by date (newest first), each with: `id`, `subject`, `sender`, `date_received`, `read`, `flagged`.

```python
get_emails()
# All emails from default mailbox

get_emails(filter="unread", limit=10)
# 10 most recent unread emails

get_emails("Work", "INBOX", filter="today")
# Today's work emails
```

---

## `get_email()`

Get a single email with full content. Uses a 3-strategy cascade to find the message:

1. Try the specified mailbox directly
2. Look up the email's location in the FTS5 index
3. Iterate all mailboxes with per-mailbox error handling

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message_id` | `int` | *required* | Email ID (from list/search results) |
| `account` | `string?` | env default | Helps find the message faster |
| `mailbox` | `string?` | `INBOX` | Helps find the message faster |

**Returns:** Full email with: `id`, `subject`, `sender`, `content` (full body text), `date_received`, `date_sent`, `read`, `flagged`, `reply_to`, `message_id` (RFC 822 Message-ID header, always in its bracketed form `<a@b>` whichever strategy answered), `account` and `mailbox` (where the message is *now*), `attachments` (list of `{filename, mime_type, size}`).

```python
get_email(12345)
# → {"id": 12345, "subject": "Meeting notes", "content": "Hi team,...",
#    "attachments": [{"filename": "notes.pdf", "mime_type": "application/pdf", "size": 52340}], ...}
```

!!! tip
    If `account` and `mailbox` are not provided, the server searches all mailboxes in the default account to find the message.

!!! note
    The `attachments` list comes from JXA and only reports file attachments visible in Mail.app's UI. For reliable extraction (including inline images), use `get_email_attachment()`.

---

## `search()`

Search emails with automatic FTS5 optimization. Uses the FTS5 index for fast search (~2ms) when available, falls back to JXA-based search otherwise.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `string` | *required* | Search term or phrase |
| `account` | `string?` | `None` | Account filter. `None` = search all (FTS) or default (JXA) |
| `mailbox` | `string?` | `None` | Mailbox filter. `None` = search all (FTS) or default (JXA) |
| `scope` | `string?` | `all` | Search scope (see below) |
| `limit` | `int?` | `20` | Max results |
| `offset` | `int?` | `0` | Skip first N results (for pagination) |
| `exclude_mailboxes` | `list?` | `["Drafts"]` | Mailboxes to exclude (FTS/attachment scopes only) |
| `before` | `string?` | `None` | Only return emails before this date (YYYY-MM-DD) |
| `after` | `string?` | `None` | Only return emails after this date (YYYY-MM-DD) |
| `highlight` | `bool?` | `False` | Highlight matching terms in results |

**Scopes:**

| Scope | Searches | Engine |
|-------|----------|--------|
| `all` | Subject + sender + body | FTS5 (if indexed) |
| `subject` | Subject line only | FTS5 column filter (if indexed) |
| `sender` | Sender field only | FTS5 column filter (if indexed) |
| `body` | Body content only | FTS5 (if indexed) |
| `attachments` | Attachment filenames | SQL (requires index) |

**Returns:** List of results sorted by relevance (FTS5) or date (JXA fallback), each with: `id`, `subject`, `sender`, `date_received`, `score`, `matched_in`, and optionally `content_snippet`, `account`, `mailbox`.

```python
search("invoice")
# Search everywhere — uses FTS5 for instant results

search("john@example.com", scope="sender")
# Find emails from a specific sender

search("meeting notes", scope="body")
# Search body content only

search("pdf", scope="attachments")
# Find emails with PDF attachments

search("deadline", limit=5)
# Top 5 results

search("invoice", after="2025-01-01", before="2025-12-31")
# Emails from 2025 only

search("meeting", limit=20, offset=20)
# Page 2 of results (skip first 20)

search("meeting", highlight=True)
# Results with matching terms highlighted
```

---

## `get_email_links()`

Extract all links (URLs) from an email's body content.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message_id` | `int` | *required* | Email ID |
| `account` | `string?` | `None` | Account (helps disambiguate) |
| `mailbox` | `string?` | `None` | Mailbox (helps disambiguate) |

**Returns:** List of links found in the email body.

```python
get_email_links(12345)
# → [{"url": "https://example.com/invoice", "text": "View Invoice"}, ...]
```

---

## `get_email_attachment()`

Extract attachment content from an email. Parses the raw `.emlx` MIME structure, so it works for all attachment types including inline images.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message_id` | `int` | *required* | Email ID |
| `filename` | `string` | *required* | Attachment filename to extract |
| `account` | `string?` | `None` | Account (helps disambiguate) |
| `mailbox` | `string?` | `None` | Mailbox (helps disambiguate) |

**Returns:** Dictionary with `filename`, `mime_type`, `size`, and `content_base64`. If the attachment exceeds 10 MB, returns metadata only with `truncated: true`.

```python
get_email_attachment(12345, "invoice.pdf")
# → {"filename": "invoice.pdf", "mime_type": "application/pdf",
#    "size": 52340, "content_base64": "JVBERi0x..."}
```

!!! note
    Requires the FTS5 search index. If upgrading from v0.1.x, run `apple-mail-mcp rebuild` to populate attachment metadata.

---

## `set_flag()`

Flag or unflag one or more emails, optionally with a color.

!!! warning "Write operation"
    Refused when the server runs read-only (`APPLE_MAIL_READ_ONLY=true`, `[server] read_only = true`, or `apple-mail-mcp serve -r`): the call raises `PermissionError` and nothing is written.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message_ids` | `ref` or `list[ref]` | *required* | One reference or a list of them (max 500 per call). A reference is the RFC 822 `message_id` header (preferred, e.g. `"<a1b2@example.com>"`) or the numeric `id` |
| `color` | `string?` | `default` | What to set (see below) |
| `account` | `string?` | `None` | Optional hint. Speeds id resolution; required (with `mailbox`) to place numeric ids when no search index is built |
| `mailbox` | `string?` | `None` | Optional hint (see `account`) |

**Colors:**

| Color | Effect |
|-------|--------|
| `default` | Flag without forcing a color (default) |
| `none` | Remove the flag |
| `red`, `orange`, `yellow`, `green`, `blue`, `purple`, `gray` | Flag with that color |

These are Apple Mail's seven colors and nothing more. The server attaches **no** meaning to any of them — what a color stands for is the user's own convention.

**Returns:** A dict of per-reference outcome buckets. A batch never fails as a whole: every reference lands in exactly one bucket, echoed exactly as it was passed (an int id as an int, a Message-ID header as that header).

| Field | Present | Description |
|-------|---------|-------------|
| `updated` | always | References actually changed |
| `unchanged` | always | Already in the requested state, so no write was sent — still a success |
| `not_found` | always | Mail was reachable and the message was not there |
| `skipped_hidden` | always | Resolved into an excluded account (`APPLE_MAIL_INDEX_EXCLUDE_ACCOUNTS`); never sent to Mail |
| `failed` | when non-empty | Mail refused the write or was unreachable — **not** a verdict that the message is gone |
| `error` | with `failed` | What Apple Mail actually said |
| `diagnostics` | when something did not land | What the write actually did: `accounts_searched`, `mailboxes_preferred`, `located_by_index`, `references_as_received`, `mailboxes_not_searched` |
| `hint` | when actionable | Guidance, e.g. that a numeric id had moved and was re-found by its Message-ID |

```python
set_flag("<a1b2@example.com>", color="red")
# → {"updated": ["<a1b2@example.com>"], "unchanged": [],
#    "not_found": [], "skipped_hidden": []}

set_flag(["<a@x.com>", "<b@x.com>"], color="orange")
# Batch — each reference lands in exactly one bucket

set_flag("<a1b2@example.com>", color="none")
# Unflag

set_flag(12345, color="red")
# Numeric id, if that's all there is
```

!!! tip "Prefer the Message-ID"
    A numeric `id` is a per-mailbox ROWID: exact while the message stays put, dead as soon as any device files it elsewhere. The `message_id` header survives the move and is searched in every visible account, starting with the one the index points at. A numeric id that no longer resolves is re-found by its Message-ID while the index still knows it, and the move is reported in `hint`.

!!! note
    A `not_found` with a non-zero `diagnostics.mailboxes_not_searched` means the search did not cover every mailbox, so the message's absence is not established.

---

## `set_read_status()`

Mark one or more emails read (seen) or unread (unseen).

!!! warning "Write operation"
    Refused when the server runs read-only (`APPLE_MAIL_READ_ONLY=true`, `[server] read_only = true`, or `apple-mail-mcp serve -r`): the call raises `PermissionError` and nothing is written.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message_ids` | `ref` or `list[ref]` | *required* | One reference or a list of them (max 500 per call). A reference is the RFC 822 `message_id` header (preferred, e.g. `"<a1b2@example.com>"`) or the numeric `id` |
| `read` | `bool?` | `True` | `True` marks read (seen); `False` marks unread (unseen) |
| `account` | `string?` | `None` | Optional hint. Speeds id resolution; required (with `mailbox`) to place numeric ids when no search index is built |
| `mailbox` | `string?` | `None` | Optional hint (see `account`) |

**Returns:** The same per-reference buckets as `set_flag()`: `updated`, `unchanged`, `not_found`, `skipped_hidden`, plus `failed` (with `error`), `diagnostics` and `hint` when they apply.

```python
set_read_status("<a1b2@example.com>")
# Mark read

set_read_status(["<a@x.com>", "<b@x.com>"], read=False)
# Mark a batch unread

set_read_status(12345)
# Numeric id, if that's all there is
```

---

## `get_index_status()`

Diagnose the search index: readiness, build progress, and setup problems — with step-by-step instructions to fix them. Reads state only and changes nothing, so it also works in read-only mode.

Worth calling whenever email tooling behaves unexpectedly: search returns nothing, a write reports `not_found`, or the user asks whether it is working or how far along a build is.

**Parameters:** None

**Returns:** Dictionary with, among others:

| Field | Description |
|-------|-------------|
| `state` | `building`, `ready`, `empty` or `absent` |
| `user_message` | One plain sentence to relay to the user |
| `next_steps` | Ordered, non-technical instructions to fix a problem. Present only when there is something to do |
| `problem` / `note` | What is wrong, or why the setup is fine anyway. Present when relevant |
| `indexed_emails`, `disk_emails`, `progress_percent` | Build progress — counts rise continuously while a build runs |
| `mail_dir_accessible` | `false` means macOS Full Disk Access is missing for the app running the server — the most common cause of an empty index |
| `index_command` | The exact command for this install, if one is needed |
| `index_mode`, `server_version`, `read_only`, `write_tools_enabled` | Setup |
| `recent_events` | What the server actually did, newest first (build/sync started, finished, failed) |
| `log_file` | Where the server log is |
| `last_error`, `failed_parse_jobs`, `last_sync`, `staleness_hours`, `db_size_mb`, `excluded_accounts` | Health details |
| `without_stable_id` | Rows indexed before schema v6, with no stored Message-ID — `refresh_index(full=True)` backfills them |
| `skipped_too_large` | Messages over the size limit and therefore not searchable. Present only when non-zero |

```python
get_index_status()
# → {"state": "ready", "indexed_emails": 73104, "mail_dir_accessible": true,
#    "user_message": "The mail index is ready.", "staleness_hours": 2.4, ...}
```

!!! tip
    When the result carries `problem` or `next_steps`, relay `user_message` and walk the user through `next_steps` in order rather than showing the raw JSON. For a client-polled snapshot that needs no tool call, see the `index://status` resource below.

---

## `refresh_index()`

Update or completely rebuild the server's FTS5 search index on demand. The index otherwise syncs only at server start, so a long-running client drifts out of date.

This is the index at `~/.apple-mail-mcp/index.db` — not Apple Mail's own envelope index, and unrelated to Mail.app's *Mailbox > Rebuild*. It touches only the local index, never the mail itself, so it is allowed in read-only mode.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `full` | `bool?` | `False` | `False` syncs changes since the last run — fast, returns when done. `True` discards the index and rebuilds from scratch in the background, returning immediately |

**Returns:** Dictionary with `status`, a `message` to relay, and `changes` (added + deleted + moved) for a completed sync. A failure adds `error`, plus `next_steps` when the sync could not read the mail directory.

| Status | Meaning |
|--------|---------|
| `completed` | Sync finished; `changes` says how much moved |
| `started` | A background build began — follow it with `get_index_status()` |
| `already_running` | A build or sync is already in progress |
| `unconfirmed` | A rebuild was launched but had not begun reading mail in time — check `get_index_status()` in a minute |
| `failed` | Nothing was updated; `error` says why |

```python
refresh_index()
# → {"status": "completed", "changes": 12,
#    "message": "Index updated: 12 change(s)."}

refresh_index(full=True)
# → {"status": "started",
#    "message": "Building the index in the background. ..."}
```

!!! note
    When no usable index exists yet, `refresh_index()` starts a background build even with `full=False` — a first build of a large mailbox is too slow to wait for.

---

## MCP Resources

Tools are model-invoked (the LLM calls them). **Resources** are typically client-polled — read-only data the MCP client can pull as context without an LLM round-trip.

### `index://status` *(added v0.3.0)*

Read-only JSON snapshot of FTS5 search-index health. Lets clients render an "index OK" indicator or surface staleness without invoking a tool.

**MIME type:** `application/json`

**Payload (when index exists):**

| Field | Type | Description |
|-------|------|-------------|
| `has_index` | `bool` | Always `true` when index file is present |
| `email_count` | `int` | Number of indexed emails |
| `mailbox_count` | `int` | Number of distinct (account, mailbox) pairs |
| `attachment_count` | `int` | Total attachment metadata rows |
| `disk_email_count` | `int?` | Total `.emlx` files on disk (best-effort; `null` if Full Disk Access denied) |
| `db_size_mb` | `float` | Size of index DB on disk, rounded to 0.01 MB |
| `capped_mailboxes` | `int` | Number of mailboxes that hit `APPLE_MAIL_INDEX_MAX_EMAILS` cap |
| `failed_jobs_count` | `int` | Rows in the dead-letter queue (`.emlx` parses that failed) |
| `last_sync` | `string?` | ISO-8601 of last sync, or `null` if never synced |
| `staleness_hours` | `float?` | Hours since `last_sync`, rounded to 0.01 |

**Payload (when no index):**

```json
{
  "has_index": false,
  "message": "No index found. Run 'apple-mail-mcp index' to build it."
}
```
