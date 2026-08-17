**Title:** `Upstreaming improvements from the iret77 fork — which would you like as PRs?`

---

Hi @imdinu — thanks for apple-mail-mcp. We've been running a fork
([iret77/apple-mail-mcp](https://github.com/iret77/apple-mail-mcp)) fairly hard
(tested on a ~73K-message mailbox) and it has grown a number of fixes and
features we'd like to offer back. Rather than open a wall of PRs unprompted,
this issue is the map: **you tell us which of these you want, and we open them as
focused, individually-reviewable PRs** — each a clean diff against your current
`main`, with tests, per your CONTRIBUTING. We can send any subset in any order.

A few ground rules we're holding ourselves to:

- **One topic per PR**, no unrelated changes, based on your current `main`.
- **No PR touches `CHANGELOG.md` / `pyproject.toml` / `server.json`** — the
  release number and changelog placement are yours. Changelog prose ships in each
  PR body, ready to paste.
- Fork-specific bits (build/revision stamps, our auto-update launcher wiring) are
  stripped out and never appear in a PR. The `.mcpb` install bundle *itself* we'd
  love to contribute though — see **Track E** — just without those stamps.

### On #106 (single-writer lock) — a robustness fix for your IndexLock (A8)

Here we **are** proposing something. We hit #106 independently before your
`0.4.3` and shipped our own lock; you've since solved it with `IndexLock`, so
we're not asking you to swap that out. But your `IndexLock.try_acquire()` has a
real robustness bug we'd like to fix: it treats **any** `OSError` from `flock` as
"held" and returns `False`. On a filesystem that can't lock (some network / FUSE
homes answer `ENOLCK` / `ENOTSUP`) that makes the index **permanently
unwritable** — every retry reads as contention forever — and an `os.open` failure
on a read-only home isn't caught at all. **A8** (below) distinguishes genuine
contention (`EWOULDBLOCK` / `EAGAIN` / `EACCES`) from an unlockable filesystem
(degrade to single-process with a warning, don't wedge). Small, and against your
own code.

---

## Track A — bug fixes (no API change)

| Unit | Fixes |
|---|---|
| **A1** header decoding | A non-ASCII `Subject`/`Received`/`Content-ID`/filename makes Python's `email` hand back a `Header`, the first `.strip()` raises, and **the whole sync dies** (only reaches stderr). Guarantees a decoded `str`; the per-message guard catches broadly so one bad message can't kill the run. *Our single most valuable fix.* |
| **A2** oversized emails | Messages over the size limit vanished silently. Now recorded in the DLQ as `too_large`, limit configurable, counts surfaced through the existing `index://status` resource (no new tool). |
| **A3** stale read/flag state | `get_email()` read read/flagged from the `.emlx` plist footer, which Mail doesn't reliably rewrite. Overlaid from the Envelope Index (one read-only SELECT). |
| **A4** local timestamps | Timestamps went out as UTC while Mail.app shows local time. Converts at every output boundary using the system zone (DST-correct); storage stays UTC. |
| **A5** sync rollback | An aborted sync transaction stayed open and blocked *every* later write with "database is locked". |
| **A6** per-thread connections | A background rebuild froze the server; `threading.local` connections fix it. |
| **A7** FTS rebuild integrity | A rebuild could leave the index with no FTS triggers, surviving restarts. Triggers restored as the first `finally` action. |
| **A8** harden `IndexLock` | *Against your 0.4.3 code, not our fork.* `try_acquire()` treats any `flock` `OSError` as contention → a non-locking filesystem wedges the index permanently; a read-only home raises uncaught. Distinguishes real contention from an unlockable FS (degrade + warn). See the #106 note above. |
| **A9** mailbox roles, not names | `INBOX` is a string; a German Mail has `Posteingang`, Exchange `Deleted Items`, Gmail `[Gmail]/…`. The no-arg call failed outright on a localized install. Resolves by role: exact name → Mail's `sentMailbox`/`trashMailbox`/… → normalized match → a table of localized names from Apple's own user guide → fail loudly listing what exists. |
| **A10** incomplete search ≠ absence | A tool reported "not found" when the search was actually incomplete (a mailbox cap, an unreadable mailbox, a timeout, a denied permission — all produce the *identical* empty answer). Each scan branch now counts what it left out and says so. |
| **A11** nested mailbox / GUID path | Path inference swallowed Apple's GUID directory into the mailbox name (`INBOX/<GUID>`) and stopped at the first `.mbox`, so a nested mailbox (`Archiv/2026`) was recorded under the wrong name. That name disagrees with the one the Envelope Index derives from the mailbox URL — **so every message in a nested mailbox lost its stable-id lookup**, and no sync repaired it. |

## Track B — diagnostics (additive; the tool surface grows)

An MCP server's stderr reaches nobody under a desktop client — the user sees "it
doesn't work" and the agent has no channel.

| Unit | Adds |
|---|---|
| **B1** build progress/phases | Build phase, progress, stall detection, a ring of the last 50 events — all servable through the existing `index://status` resource, no new tool. |
| **B2** `get_index_status()` | State, progress, whether `~/Library/Mail` is readable (the Full Disk Access test), DLQ counts + examples, actionable `next_steps`. |
| **B3** `refresh_index()` | Sync on demand; `full=True` rebuilds in the background. Reports `already_running`/`failed` rather than a blind "started". |
| **B4** server log file | Rotating log at `~/.apple-mail-mcp/server.log`, created `0600` even after rotation. |
| **B5** optional auto-build | Build the index on first `serve` when none exists — offered opt-in, default `false` (a product decision, not a bug fix). |

## Track C — writing, and stable identity

Your `_ensure_writable()` / `APPLE_MAIL_READ_ONLY` (#80) exists but currently
guards nothing — track C gives it something to guard.

| Unit | Adds |
|---|---|
| **C1** stable-identity schema | Schema v6: an `rfc822_message_id` column + index, in-place `v5→v6` migration. Pure storage, prerequisite for the rest. |
| **C2** expose message-id in reads | Every read path also hands out the RFC822 `Message-ID` header. Useful with no write tools at all: a Mail.app id is a per-mailbox ROWID that dies the moment any device files the message elsewhere; the header survives. |
| **C3** write tools | `set_flag(ids, color?)` (all seven Apple colours) and `set_read_status(ids, read?)`. Single or batch, per-reference result buckets — a batch never fails whole. Verifies `msg.id()` before writing. *This is what #80's gate can finally guard.* |
| **C4** message-id as write reference | The tools accept the stable header as a reference and never translate it back into a ROWID and then trust it — writes match `msg.messageId()` in JXA; reads verify the header on what came back. |

## Track D — fewer round-trips

| Unit | Adds |
|---|---|
| **D1** batch reads | `get_email` accepts a list of references, one entry per reference; one unreadable message never sinks the batch. |
| **D2** cross-account listing | `get_emails(account="all")` lists across every visible account in one call; exclusions still hold by UUID. |
| **D3** mailbox pagination | `get_emails(before=, after=, offset=)` makes a mailbox walkable backwards — otherwise only the newest N are reachable. |

## Track E — install UX

The single biggest friction for a non-technical user today is setup: installing
`uv`/`pip` and hand-editing `claude_desktop_config.json`. This removes it.

| Unit | Adds |
|---|---|
| **E1** one-click Claude Desktop install (`.mcpb`) | A double-click desktop-extension bundle (`manifest_version 0.3`) — the user installs by opening the file, no `uv`/`pip` and no JSON editing. A small Node launcher boots the Python server and **self-heals a missing `uv`** (fetches it, or explains clearly instead of failing silently). We'd contribute a **generic** bundle; our fork-specific auto-update / build-revision tracking stays on the fork. This is, in practice, the change that makes the server usable for people who aren't comfortable at a terminal. |

---

### What we'd like from you

Just tell us **which units (or whole tracks) you want as PRs** — and whether you
prefer them fine-grained (one per unit) or bundled per track. Our suggested first
wave, if you want a starting point: **A1, A3, A4, A9, A11** — small, low-risk,
each reviewable in minutes, and A9/A11 in particular make the server usable on
localized and nested-mailbox setups. **E1** (one-click install) is the highest
user-facing impact if you'd like to start there instead.

Each unit is implemented and tested on the fork; on your word we open exactly the
ones you want as a focused PR, **freshly rebased on your current `main`** (not a
stale branch), with `ruff check` / `ruff format --check` / `pytest` green. Happy
to adjust scope, split, or drop any of them.
