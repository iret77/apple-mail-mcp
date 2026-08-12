# Health Check

A protocol for verifying a release against a real Mail.app on macOS.

The test suite runs on any platform and never touches Apple Events: the
JXA scripts are executed in node against stubs, and `osascript` is
mocked. That covers the logic and misses everything about the actual
mail. **This check is the part that cannot be automated here** — it is
run by a person, against their own mailbox, on the machine the bundle
was installed on.

## How to run it

Paste the protocol below into a **fresh** Claude Desktop session with
the MCP server installed. It drives the tools in a safe order and
reports back. It touches exactly one message, reversibly.

Every phase below states *what it guards*. That is not decoration: each
one exists because the thing it checks broke once, shipped, and was
found by somebody running this protocol.

| Phase | Guards against |
|-------|----------------|
| 0 | Testing a build that is not the one you installed |
| 1 | A single-reference tool that rejects a form its siblings accept |
| 2 | A `mailbox` value that a read hands out and a write cannot use |
| 3 | Listings losing the stable `message_id` |
| 4 | Diagnostics that misreport the search that happened |
| 5 | Mailbox roles failing on a non-English Mail |
| 6 | Pagination that silently restarts; a listing wider than asked |
| 7 | A write that reports the wrong outcome |

## Rules given to the session

1. **Raw output, not a summary.** Where the protocol says "raw", the
   tool's answer is wanted verbatim — **including exact error text,
   character for character**. One blocker was found only because an
   error contained `&lt;` instead of `<`.
2. **Change nothing permanently.** Writes happen on one message, whose
   state is recorded first and restored at the end.
3. **Never guess.** `not_found`, `failed`, `ambiguous`, `unconfirmed`
   or a `hint` are *results*, not errors — quote them and move on. Do
   not repair anything, do not work around, do not "try it differently".
4. **No `refresh_index(full=True)`** without asking first.
5. **Never delete, move, send or reply to a message.**

---

## The protocol

> Copy everything from here down into a fresh session.

You are running a health check of the Apple Mail MCP server. Work
through the phases in order. Phases 0–4 are regression tests — **if one
of them fails, stop and report it**; the rest is not worth running.

Follow these rules: give raw tool output where asked, including exact
error text. Change nothing permanently — writes happen only in phases 2
and 7, on one message, restored at the end. Never guess: `not_found`,
`failed`, `ambiguous`, `unconfirmed` and `hint` are results, not
errors — quote them and continue. Do not repair anything. No
`refresh_index(full=True)` without asking. Never delete, move, send or
reply to a message. For each step report: tool, parameters, answer,
PASS / FAIL / UNCLEAR.

### Phase 0 — Build and index

- `get_index_status()` — raw.
  - The `server_version` must be **the version you just installed**. If
    it is not, **stop here**: Claude Desktop is still talking to an
    older build and everything below measures the wrong thing.
  - Note `indexed_emails`, `disk_emails`, `staleness_hours`,
    `without_stable_id`, `failed_parse_jobs`. If `failed_parse_jobs`
    is above 0, `failed_parse_examples` must be present.
- `refresh_index()` — raw. Report how long it took.
  - Expect `completed`. A large `changes` value right after an upgrade
    is normal *if the release notes say index rows are being replaced*;
    otherwise it is worth explaining.

### Phase 1 — Every reference form reaches the same message

- `search("pdf", scope="attachments", limit=5)` — raw. Take one hit as
  **A**; note its `id`, `message_id` and attachment `filename`.
- Call `get_email_attachment` **four times** with the same filename and
  a different reference form. **All four must return the attachment:**
  1. the numeric `id`
  2. the same id as a **string**
  3. the `message_id` **with** angle brackets
  4. the `message_id` **without** angle brackets
- Quote the exact error text of any failure. If it contains `&lt;` or
  `&gt;` instead of `<` and `>`, say so explicitly.
- Then `get_email_links()` with forms 1 and 3 — both must answer. An
  empty link list is a valid answer, not a failure.
- If no message with an attachment exists, say so and skip that part.

### Phase 2 — The read-then-write loop

- `get_emails(limit=5)` — raw. Take the newest message as **T**; note
  `id`, `message_id`, `subject`, `read`, `flagged`.
- `get_email(<id of T>, account="<its account>")` — raw. It must report
  `account` and `mailbox`.
- `list_mailboxes(account="<account of T>")` — raw. **The `mailbox`
  value from `get_email` must appear in this list verbatim.** A GUID in
  it is a failure.
- **The test:** pass that exact `mailbox` value into a write —
  `set_flag(<id of T>, account=…, mailbox="<the reported value>",
  color="orange")`. Expect `updated`; "No mailbox matching" is a
  failure.
- Restore the original flag state and confirm with `get_email`.

### Phase 3 — The stable identity in listings

- `get_emails(account="all", limit=10)` — raw.
  - Every row should carry `message_id` in bracketed form. Say how many
    of 10 do. A `null` on mail that is minutes old is expected; several
    on mail from yesterday is not.
- Fetch the same header for T three ways and print all three raw, one
  under the other: from `get_emails`, from `search()`, and from
  `get_email`. They must be **character-identical**.
  - If `search()` does not return T at all, note it separately and
    compare only the other two. To tell ranking from a missing index
    row, also try `search("<subject of T>", scope="subject")`.

### Phase 4 — Do the diagnostics tell the truth?

- `set_flag(999999999, color="red")` — raw, complete. Then the same
  with the id as a **string**.
- Check: `diagnostics.accounts_searched` is **not empty** and holds no
  `null`. If `diagnostics.mailboxes_not_searched` is above 0, the
  `hint` must **not** blame the Automation permission — it should say
  that mailboxes could not be read and that nothing about the
  message's existence has been established.
- No `ReferenceError`, no "None" or "null" in any user-facing text.

### Phase 5 — Accounts, mailboxes, roles

- `list_accounts()`.
- `get_emails(mailbox="Sent", limit=3)` and
  `get_emails(mailbox="Trash", limit=3)` — results or an *empty* list,
  never an error, even when Mail is not in English. And **not** the
  contents of the inbox.
- `get_emails(mailbox="Definitely-Does-Not-Exist-XYZ", limit=1)` — an
  error listing the mailboxes that *do* exist, with no "None"/"null".

### Phase 6 — Pagination, account scope, search

- **Cursor:** `get_emails(limit=5)`, then
  `get_emails(limit=5, before="<date_received of the last row>",
  before_id=<its id>)` — five **older** messages, no overlap.
- **Half a cursor:** `get_emails(limit=5, before_id=123)` — an error.
- **Empty cursor:** `get_emails(limit=5, before="", before_id=123)` —
  the same error.
- **Scope:** `get_emails(limit=10)` against
  `get_emails(account="all", limit=10)` — the first from exactly one
  account; under `"all"` every row carries `account` as a plain name,
  never a UUID.
- **Search:** one query each for the default scope, `body`, `sender`
  and `attachments` — every result carries `message_id`.

### Phase 7 — Write paths and deliberate failures

Continue on **T**, restoring after each step.

- The id as a **string**, with `account` and `mailbox` → `updated`.
- The `message_id` alone, no account or mailbox → `updated`.
- The id with `mailbox` but **no** `account` → either `updated`, or
  `failed` with a hint about "more than one mailbox". Both are correct;
  afterwards confirm with `get_email` that **T** was the message meant.
- `set_read_status(<message_id of T>, read=<the original value>)` — a
  deliberate no-op, expect `unchanged`. This is the proof that the
  server reads live state instead of writing blindly.
- **Restore and prove it:** `get_email(<message_id of T>)` — `read` and
  `flagged` exactly as at the start. **The most important check here.**

These are expected to fail:

- `get_email(999999999)` — an error that also says whether the search
  was complete.
- `set_flag(999999999, color="red")` — `not_found` or `failed` with a
  hint. The hint must not claim the message was deleted unless the
  search actually covered everywhere.
- `get_email_attachment(<message_id of T>, "does-not-exist.pdf")` — a
  clear error **naming the file**. "not found in index" here means the
  reference was not resolved at all, which is a different bug.

### Report

1. Three lines first: the build, whether phase 1 passed, whether
   phase 2 passed.
2. A table: phase | check | PASS/FAIL/UNCLEAR | note.
3. Every FAIL with the exact call and the **full** answer.
4. A reference matrix for phase 1: one row per form.
5. The three header strings from phase 3, raw, one under the other.
6. The diagnostics excerpt from phase 4, raw.
7. A write matrix for phases 2 and 7.
8. Confirmation that T is back in its original state, with before and
   after values.
9. Anything noticeably slow (> 3 s), and how long `refresh_index()`
   took.
10. One sentence: would you release this server for daily use?

---

## Reading the result

A finding in the **raw output** is worth more than a finding in the
conclusions. Both blockers in 0.20.4 were visible only in unfiltered
answers — a missing field and an escaped angle bracket — while the
summary of the same run said everything passed.

When something fails, the useful question is not "is it broken?" but
**"which of the two answers is this?"**:

- a *statement about the mail* ("the message is not there"), or
- a *statement about the search* ("we did not get to look everywhere").

The server is written to keep those apart, and every hint it produces
says which one it means. A report that preserves that distinction is
directly actionable; one that flattens it into "not found" costs a
debugging round.
