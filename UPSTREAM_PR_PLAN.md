# Upstream PR plan — iret77/apple-mail-mcp → imdinu/apple-mail-mcp

Measured, not assumed: `git merge-base upstream/main feat/write-ops-flag-read`
== `upstream/main` HEAD (`ee655d4`). **Upstream is zero commits ahead of us**;
we are ahead by the work listed below. No rebase needed.

## Guiding idea

The maintainer must not face an all-or-nothing package. Every unit below is cut
so it can be merged, reviewed at leisure, reworked or declined on its own,
without dragging the others down. Where a dependency is unavoidable it is named
— together with the commitment to rebuild the rest if one piece is rejected.

## What upstream's CONTRIBUTING.md asks for

- "Keep the diff focused — avoid unrelated changes in the same PR."
- "PRs are typically squash-merged into `main`." → our commit history does not
  travel; **what counts is the diff per PR**, not our sequence.
- Required: `ruff check src/`, `ruff format --check src/`, `pytest`.
- GPL-3.0.

## What upstream already has (verified, not assumed)

- `_ensure_writable()` and `APPLE_MAIL_READ_ONLY` (#80) — **the gate exists but
  guards nothing.** That is the hook for track C.
- The `index://status` resource — counters can attach there without a new tool.
- 8 tools.

## What must not travel upstream

These are **marked in the source**, not listed by line number — a list
goes stale on the next edit:

```
# fork-only:start … # fork-only:end     a block
something()                # fork-only  a single line
x = "plain"  # fork-only-replace: …     what the fork version did
```

`scripts/fork-only.py list` shows every marked region, `check` verifies the
markers are balanced, and `strip <dir>` writes a copy with them removed — so an
upstream branch is cut mechanically rather than from memory.

| Item | Where | Why |
|---|---|---|
| `mcpb/`, `scripts/build-mcpb.sh`, `dist/`, this file | fork branches | fork distribution; `strip` drops them |
| `install_mode`, `source_ref`, `APPLE_MAIL_MCP_LAUNCHER`, `APPLE_MAIL_MCP_REF` | server.py | describes our bundle launcher |
| `SERVER_REVISION` | server.py | our build stamp; upstream ships via PyPI, where the package version answers it |
| The `.mcpb` paragraph in the README | README.md | fork-specific |

---

# The units

`⊘` = can be declined with no effect on any other unit.
`↑` = builds on the named unit (usually textually — same file; if it is
rejected we rework the rest).

## Track A — bug fixes, no API change

Eight of them, each small and reviewable on its own. None widens the tool
surface; all repair demonstrably broken behaviour.

| # | Branch | Diff | Dep. |
|---|---|---|---|
| A1 | `fix/emlx-header-decoding` | `disk.py`, `sync.py` | ⊘ |
| A2 | `fix/oversized-emails-visible` | `disk.py`, `config.py`, `schema.py`, `server.py` (resource only) | ⊘ |
| A3 | `fix/stale-emlx-flags` | `server.py`, `envelope_direct.py` | ⊘ |
| A4 | `fix/local-timestamps` | `server.py` | ⊘ |
| A5 | `fix/sync-transaction-rollback` | `manager.py` | ⊘ |
| A6 | `fix/per-thread-connections` | `manager.py` | ↑ A5 |
| A7 | `perf/rebuild-fts-delete-all` | `manager.py` | ↑ A6 |
| A8 | `fix/cross-process-write-lock` | `manager.py`, `watcher.py` | ↑ A6 |
| A9 | `fix/mailbox-roles-not-names` | `jxa/mail_core.js` | ⊘ |
| A10 | `fix/incomplete-search-is-not-absence` | `server.py`, `builders.py` | ⊘ |

**A1 — an undecodable header aborts the entire sync.** A non-ASCII `Subject`,
`Received`, `Content-ID` or attachment filename makes Python's `email` module
hand back a `Header` object instead of a `str`; the first `.strip()` raises
`AttributeError` and **the whole sync dies**. It ran unnoticed for a day here,
because the error only ever reached stderr. `header_text()` and
`_filename_text()` guarantee a decoded `str`, and a guard test forbids raw
header access returning to the parser. The per-message guard in `sync.py` now
catches `Exception` rather than three specific types: one bad message must
never kill the run. *The single most valuable PR of the set.*

**A2 — oversized messages vanished silently.** Now recorded in the DLQ as
`too_large`, with the limit configurable via `APPLE_MAIL_INDEX_MAX_EMAIL_MB`
(default 25) and the counts surfaced through the **existing** `index://status`
resource — no new tool, hence independent of track B.

**A3 — stale read/flagged state.** `get_email()` read both from the `.emlx`
plist footer, which Mail does not reliably rewrite. Overlaid from Apple's
Envelope Index (one read-only SELECT).

**A4 — time zone.** All timestamps went out as UTC while Mail.app shows local
time (12:54 versus 14:54 here). `to_local_iso()` converts at every output
boundary using the **system** zone (DST-correct), never a hardcoded one.
Storage stays UTC.

**A5–A8 — four causes of "database is locked."** Found one after another, hence
separable; they all live in `manager.py` and therefore stack textually:

- **A5 rollback** — an aborted transaction stayed open and blocked *every*
  later write.
- **A6 per-thread connections** (`threading.local`) — a background rebuild
  otherwise froze the server.
- **A7 FTS triggers** dropped before the `DELETE`, the index emptied with a
  single `INSERT INTO emails_fts(emails_fts) VALUES('delete-all')`, triggers
  restored as the **first** action in `finally`. DDL commits implicitly in
  SQLite, so a rollback does not bring them back: without the restore, a
  failure mid-rebuild leaves an index that silently stops matching its FTS
  table.
- **A8 cross-process lock** (`fcntl.flock`). A `threading.Lock` is not enough:
  Claude Desktop starts the server **twice** — your open issue **#106**. Falls
  back to thread-only locking when the lock file cannot be created.

**A9 — a mailbox name is the weakest handle there is.** It changes with the
system language (`Posteingang`), with the macOS version (Apple's own German docs
still say `Eingang`), and with the provider (`Deleted Items`,
`[Gmail]/Sent Mail`, `INBOX.Trash`). Defaulting to the string `INBOX` fails
outright on a localized install. `getMailbox()` resolves by role instead: exact
name → Mail's own special-mailbox property → a normalized match that ignores
case and provider hierarchy → a table of localized and legacy names → and if
nothing matches, an error naming the role **and listing the mailboxes that do
exist**. The table covers seventeen languages, every entry taken from Apple's
localized Mail user guide or a documented provider/legacy name. Verified against
a matrix of account shapes (Exchange, Gmail, dovecot, older macOS wording).

**A10 — an incomplete search is not a verdict.** A tool may report a message
absent only when the search actually covered everywhere it could be. A mailbox
cap, a mailbox Mail refuses to read, a timeout, a denied Apple Events
permission, an account that could not be enumerated — all leave the question
open and produce the *identical* empty answer, which is what makes the defect so
expensive. An external review found twelve instances in one pass. Each scan
branch now counts what it left out, the read paths raise instead of returning
None, and a stale `.emlx` path continues into the live strategies rather than
claiming "deleted or moved" on an assumption that was never checked. A guard
test allows the word "deleted" exactly once, in the branch where a complete
search establishes it.

*A10 is partly independent of tracks B and C: `get_email`'s Strategy 3 scan and
the stale-path shortcut exist in upstream today.*

## Track B — diagnostics (additive, the tool surface grows)

Motivation: **an MCP server's stderr reaches nobody.** Under a desktop client
the server is a black box — the user sees "it doesn't work" and the agent has
no channel at all.

| # | Branch | Diff | Dep. |
|---|---|---|---|
| B1 | `feat/build-progress-and-phases` | `manager.py` | ↑ A6 |
| B2 | `feat/index-status-tool` | `server.py`, `manager.py` | ↑ B1 |
| B3 | `feat/refresh-index-tool` | `server.py` | ⊘ |
| B4 | `feat/server-log-file` | `cli.py`, `config.py` | ⊘ |
| B5 | `feat/optional-auto-build` | `cli.py`, `config.py` | ⊘ |

**B1** adds the internals without any new tool: build phase, progress, seconds
since the last progress, stall detection, a ring of the last 50 events. The
**existing** `index://status` resource can serve all of it, so B1 is usable
even if B2 is declined.

**B2** `get_index_status()` — state, progress, whether `~/Library/Mail` is
readable (the Full Disk Access test), DLQ counts, actionable `next_steps`.
*Strip before the PR: `install_mode`, `source_ref`, `SERVER_REVISION`.*

**B3** `refresh_index(full=False)` — sync on demand, `full=True` rebuilds in the
background. Two details earned the hard way: it reports `already_running` /
`failed` instead of a blind "started", and its docstring deliberately claims
the vocabulary "rebuild / re-index" **and** states this is not Apple's own
envelope index — without that, a model sends the user to "Mailbox > Rebuild" in
Mail.app. That happened live.

**B4** rotating file log at `~/.apple-mail-mcp/server.log`, created `0600` even
after rotation (a naive handler loses the mode on the rotated file), and an
empty config value really disables it (`Path("")` normalises to `"."`, which is
truthy).

**B5 — auto-build, offered as opt-in with default `false`.** Builds the index in
the background on first `serve` when none exists. We propose the conservative
default because this is a product decision, not a bug fix: with `true` the
server would walk `~/Library/Mail` unasked on first start — minutes on a large
mailbox. Flipping it is one line, and we deliberately left it off.

## Track C — writing, and stable identity

| # | Branch | Diff | Dep. |
|---|---|---|---|
| C1 | `feat/stable-identity-schema` | `schema.py`, `manager.py`, `sync.py`, `disk.py`, `watcher.py` | ⊘ |
| C2 | `feat/expose-message-id-in-reads` | `search.py`, `server.py`, `builders.py`, `mail_core.js` | ↑ C1 |
| C3 | `feat/write-tools` | `builders.py`, `server.py` | ⊘ |
| C4 | `feat/message-id-as-write-reference` | `server.py`, `builders.py` | ↑ C2, C3 |

**C1** schema v6: an `rfc822_message_id` column plus index, migration v5→v6 as
an in-place `ALTER`. Pure storage, no behaviour change — but the prerequisite
for the rest.

**C2** every read path hands out the header: `search()` (full text and
attachments), `get_emails()` (Envelope fast path via one batched
`get_rfc822_ids()` lookup, JXA paths via `messageId` in the standard property
set), `get_email()`. **Useful on its own, with no write tools at all:** a client
holding on to a message between calls currently has only a ROWID, which dies on
the next move. Includes hardening `MailCore.batchFetch`: a property Mail refuses
is padded with nulls rather than taking the whole listing down, while a fetch
where *nothing* was readable still raises — an unreadable mailbox must never
read as "0 messages".

**C3** `set_flag(ids, color?)` with all seven Apple colours (`msg.flagIndex`:
red 0 … gray 6) and `set_read_status(ids, read?)`. Single or batch (max 500),
returning per-reference buckets `{updated, unchanged, not_found,
skipped_hidden}` — **a batch never fails as a whole**. `applyToMessage()`
verifies `msg.id() === targetId` before writing; no-ops are skipped; located
and scanned groups run in separate `osascript` calls so a slow scan cannot
discard the fast writes; excluded accounts (#90) never reach JXA. A regression
test enforces the read-only gate for any future `set_`/`flag_`/`mark_` tool.
*Hook: your `APPLE_MAIL_READ_ONLY` from #80 finally has something to guard.*

**C4** the tools accept the header as a reference. **A header is never
translated back into a ROWID and then trusted** — writing matches
`msg.messageId()` in JXA, reading fetches each candidate and verifies the
header on what came back, moving to the next on a mismatch and raising rather
than returning a stranger's mail.

## Track D — fewer round-trips

The cost of reading mail here is the round-trip, not the read: a single message
comes off disk in 1-5 ms. These three remove call counts that scale with the
number of messages.

| # | Branch | Diff | Dep. |
|---|---|---|---|
| D1 | `feat/batch-reads` | `server.py` | ⊘ |
| D2 | `feat/cross-account-listing` | `server.py` | ⊘ |
| D3 | `feat/mailbox-pagination` | `server.py`, `envelope_direct.py` | ⊘ |

**D1** `get_email` accepts a list of up to 50 references and returns one entry
per reference, in order, each either `{"ref", "email"}` or `{"ref", "error"}` —
one unreadable message never sinks the batch. A single reference keeps its old
single-object shape. The cap is about how much text fits in a model's context,
not about speed, and the error says so.

**D2** `get_emails(account="all")` lists across every visible account in one
call. The Envelope Index query already means "every account" when given no
UUID; only the tool's defaulting stood in the way — and with it an `INBOX`
default that narrows the result to whichever account happens to have a mailbox
by that name (on a localized Mail, none). Exclusions still hold, by UUID. The
JXA fallback refuses rather than quietly answering a one-account question.

**D3** `get_emails(before=, after=, offset=)` makes a mailbox walkable
backwards. Without it only the newest N per mailbox are reachable, so a backlog
cannot be approached at all and `search()` — which requires keywords — is no
substitute for a gapless reverse scan. `before` is the stable cursor: hand it
the oldest `date_received` seen. The JXA fallback refuses these parameters
rather than dropping them; ignoring a window would return the same newest N on
every page and let the caller conclude the backlog is empty.

---

# Sequencing

Do not open all 17 at once — for a single maintainer that is an avalanche, and
it achieves the opposite of "decide in detail".

1. **First wave: A1, A3, A4.** Three small, low-risk fixes, reviewable in
   minutes.
2. **Second wave after the first feedback: A2, A5–A8** — the `manager.py` stack,
   pointing at their open #106.
3. **A9 and A10 fit the first or second wave** — both are bug fixes against
   code upstream already has, and A9 in particular makes the server usable at
   all on a non-English Mail.
4. **Track D can follow at any point**; none of it depends on B or C.
5. **Then an umbrella issue** carrying the table above. The maintainer says what
   they want to see; tracks B and C become PRs only after that.

# Preparation before the first diff

1. **Redistribute the tests.** Everything new sits in `tests/test_write_ops.py`,
   named after our branch. Upstream expects tests next to their code:
   `test_disk.py`, `test_manager.py`, `test_server.py`, `test_sync.py`,
   `test_watcher.py`, `test_config.py`.
2. **Cut branches from the end state, do not cherry-pick.** Our history
   interleaves topics (several "fix: N defects found by review" commits each
   correct earlier ones).
3. **Strip the fork-specific pieces** (table above).
4. **Split CLAUDE.md proportionally** — each PR carries its own share of the
   documentation instead of one lump at the end.

# Release gate

Nothing goes towards `imdinu` before Christian explicitly approves it — no PR,
no issue, no push.
