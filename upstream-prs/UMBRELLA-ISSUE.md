**Title:** `Contributing fork improvements back: which would you like as PRs?`

---

Hi @imdinu, thanks for apple-mail-mcp. We maintain a fork
([iret77/apple-mail-mcp](https://github.com/iret77/apple-mail-mcp)), tested on a
~73K-message mailbox, that has accumulated a set of fixes and features we'd like
to contribute back.

Instead of opening a pile of PRs unprompted, this issue lists them so you can
choose. Tell us which you want and we'll open them as focused PRs: one topic
each, a clean diff against your current `main`, tests included, following
CONTRIBUTING. Any subset, any order.

How we'll keep them easy to review:

- One topic per PR, no unrelated changes.
- No PR touches `CHANGELOG.md`, `pyproject.toml`, or `server.json`: the release
  number and changelog placement stay yours. Changelog prose ships in each PR
  body, ready to paste.
- Fork-specific bits (build stamps, our auto-update launcher) are stripped. The
  `.mcpb` installer itself is worth contributing, minus those stamps (see Track E).

### #106 (single-writer lock): a robustness fix for your IndexLock (A8)

We hit #106 before your 0.4.3 and shipped our own lock; you've since solved it
with `IndexLock`, so we're not proposing a replacement. We did find one bug in it
worth fixing: `try_acquire()` treats any `flock` `OSError` as contention and
returns `False`. On a filesystem that can't lock (some network and FUSE homes
return `ENOLCK` or `ENOTSUP`), that leaves the index permanently unwritable, every
retry reading as contention; a read-only home isn't caught at all. A8 (below)
tells real contention (`EWOULDBLOCK` / `EAGAIN` / `EACCES`) apart from an
unlockable filesystem, degrading to single-process with a warning instead. Small,
and against your own code.

---

## Track A: bug fixes (no API change)

| Unit | Fixes |
|---|---|
| A1 · header decoding | A non-ASCII `Subject`, `Received`, `Content-ID`, or filename makes Python's `email` return a `Header` object; the first `.strip()` raises and the whole sync dies, reaching only stderr. Now guarantees a decoded `str`, and the per-message guard catches broadly so one bad message can't abort the run. |
| A2 · oversized emails | Messages over the size limit were dropped silently. Now recorded in the DLQ as `too_large`, with a configurable limit and counts surfaced through the existing `index://status` resource; no new tool. |
| A3 · stale read/flag state | `get_email()` read read/flagged state from the `.emlx` plist footer, which Mail doesn't reliably rewrite. Now overlaid from the Envelope Index with one read-only SELECT. |
| A4 · local timestamps | Timestamps went out as UTC while Mail.app shows local time. Now converted at each output boundary using the system zone (DST-correct); storage stays UTC. |
| A5 · sync rollback | An aborted sync transaction stayed open and blocked every later write with "database is locked". |
| A6 · per-thread connections | A background rebuild froze the server; per-thread (`threading.local`) connections fix it. |
| A7 · FTS rebuild integrity | A failed rebuild could leave the index with no FTS triggers, persisting across restarts. Triggers are now restored as the first `finally` action. |
| A8 · harden `IndexLock` | Against your 0.4.3 code, not our fork. `try_acquire()` treats any `flock` `OSError` as contention, so a non-locking filesystem wedges the index permanently and a read-only home raises uncaught. Now distinguishes real contention from an unlockable filesystem (degrade and warn). See #106 above. |
| A9 · mailbox roles, not names | `INBOX` is a string, but a German install has `Posteingang`, Exchange `Deleted Items`, Gmail `[Gmail]/…`, so the no-arg call failed outright on a localized setup. Now resolves by role: exact name, then Mail's `sentMailbox` / `trashMailbox` / …, then a normalized match, then a table of localized names from Apple's user guide, then a clear error listing what exists. |
| A10 · incomplete search ≠ absence | A tool could report "not found" when the search was actually incomplete: a mailbox cap, an unreadable mailbox, a timeout, or a denied permission all produce the same empty answer. Each scan branch now reports what it couldn't cover. |
| A11 · nested mailbox / GUID path | Path inference swallowed Apple's GUID directory into the mailbox name (`INBOX/<GUID>`) and stopped at the first `.mbox`, so a nested mailbox (`Archiv/2026`) was stored under the wrong name. That name disagrees with the one the Envelope Index derives from the mailbox URL, so every message in a nested mailbox lost its stable-id lookup, and no sync repaired it. |

## Track B: diagnostics (additive)

Under a desktop client the server's stderr reaches no one, so "it doesn't work"
arrives with no diagnostic channel. These give it one.

| Unit | Adds |
|---|---|
| B1 · build progress/phases | Build phase, progress, stall detection, and a ring of the last 50 events, served through the existing `index://status` resource, no new tool. |
| B2 · `get_index_status()` | State, progress, whether `~/Library/Mail` is readable (the Full Disk Access check), DLQ counts and examples, and actionable `next_steps`. |
| B3 · `refresh_index()` | Sync on demand; `full=True` rebuilds in the background. Reports `already_running` / `failed` instead of a blind "started". |
| B4 · server log file | Rotating log at `~/.apple-mail-mcp/server.log`, kept `0600` even after rotation. |
| B5 · optional auto-build | Build the index on first `serve` when none exists; opt-in, default `false` (a product decision, not a bug fix). |

## Track C: writing, and stable identity

Your `_ensure_writable()` / `APPLE_MAIL_READ_ONLY` (#80) exists but currently
guards nothing; this gives it something to guard.

| Unit | Adds |
|---|---|
| C1 · stable-identity schema | Schema v6: an `rfc822_message_id` column and index, with an in-place `v5→v6` migration. Storage only, and a prerequisite for the rest. |
| C2 · expose message-id in reads | Every read path also returns the RFC822 `Message-ID`. Useful even without write tools: a Mail.app id is a per-mailbox ROWID that dies as soon as any device files the message elsewhere; the header survives. |
| C3 · write tools | `set_flag(ids, color?)` (all seven Apple colours) and `set_read_status(ids, read?)`. Single or batch, with per-reference result buckets so a batch never fails as a whole; verifies `msg.id()` before writing. This is what #80's gate can finally guard. |
| C4 · message-id as write reference | The tools accept the stable header as a reference and never turn it back into a ROWID to trust: writes match `msg.messageId()` in JXA, reads verify the header on what came back. |

## Track D: fewer round-trips

| Unit | Adds |
|---|---|
| D1 · batch reads | `get_email` accepts a list of references and returns one entry per reference; one unreadable message doesn't sink the batch. |
| D2 · cross-account listing | `get_emails(account="all")` lists across every visible account in one call; exclusions still apply by UUID. |
| D3 · mailbox pagination | `get_emails(before=, after=, offset=)` makes a mailbox walkable backwards; otherwise only the newest N are reachable. |

## Track E: install UX

Setup is the biggest hurdle for non-technical users today: install `uv` or `pip`,
then hand-edit `claude_desktop_config.json`. This removes both.

| Unit | Adds |
|---|---|
| E1 · one-click Claude Desktop install (`.mcpb`) | A double-click desktop-extension bundle (`manifest_version 0.3`): the user installs by opening the file, with no `uv` or `pip` and no JSON editing. A small Node launcher starts the Python server and recovers a missing `uv`, fetching it or explaining clearly rather than failing silently. We'd contribute a generic bundle; our fork's auto-update and build-revision tracking stay behind. |

---

### What we'd like from you

Tell us which units or tracks you want, and whether you'd prefer them per-unit or
bundled per track. A good starting point is A1, A3, A4, A9, A11: small, low-risk
fixes, with A9 and A11 making the server work on localized and nested-mailbox
setups. If you'd rather lead with user impact, E1 (one-click install) is the
largest.

Each unit is implemented and tested on the fork. On your word we open the ones
you want as focused PRs, freshly rebased on your current `main`, with
`ruff check`, `ruff format --check`, and `pytest` green. Glad to split, rescope,
or drop any of them.
