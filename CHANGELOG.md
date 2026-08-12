# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.20.5] - 2026-08-12

Aus dem Health-Check gegen 0.20.4. Die drei Regressionen von zuvor
waren nicht mehr reproduzierbar; dafür stand ein Tool still, das seit
längerem niemand mit einem stringifizierten Bezug aufgerufen hatte.

### Fixed
- **`get_email_attachment()` und `get_email_links()` fanden gar
  nichts.** Beide lösen ihre Referenz auf, ohne sie zu normalisieren —
  `get_email` tut es, die beiden nicht. Damit brachen zwei ganz normale
  Client-Gewohnheiten sie vollständig:
  - MCP-Clients stringifizieren Zahlen. `"140295"` wurde als
    Message-ID-Header gelesen und fand nichts.
  - Manche Clients maskieren die spitzen Klammern, `<a@b>` kommt als
    `&lt;a@b&gt;` an — ein Header, den nichts trifft. Die Fehlermeldung
    gab dann die maskierte Form zurück, was wie ein Anzeigefehler
    aussah, aber die Ursache war.

  Beide Tools gehen jetzt durch dieselbe Tür wie die Batch-Tools. Ein
  Test prüft alle fünf Formen (int, stringifizierte Zahl, Header mit
  und ohne Klammern, maskiert), ein zweiter verbietet einem künftigen
  Leser, die Normalisierung wieder zu vergessen.
- **`accounts_searched` meldete eine leere Liste, obwohl gesucht
  wurde.** Das Feld entstand nur aus den Header-Gruppen; bei einer
  numerischen ID lief ein Scan über die Mailboxen des Standard-Kontos,
  und die Diagnose behauptete, es sei nichts durchsucht worden. Genau
  das dürfen Diagnosefelder nie. Jetzt stehen alle tatsächlich
  angefassten Konten drin, und `null` wird als "the default account"
  benannt.
- **Ein unvollständiger Scan wurde der Automation-Berechtigung
  angelastet.** Der generische Hinweis schickte Aufrufer in die
  Systemeinstellungen für eine Berechtigung, die meist in Ordnung ist —
  Mail verweigert schlicht einzelne Mailboxen (Drafts und Junk
  antworten mit -1728). Der Hinweis sagt jetzt, was das Skript ohnehin
  schon gemeldet hatte.

### Tests
- Die Selbstheilung eines unter 0.20.2/0.20.3 geschriebenen Index ist
  jetzt ein Test mit echten Dateien statt einer Behauptung in den
  Release Notes: eine Zeile mit `INBOX/<GUID>` wird beim nächsten Sync
  durch `INBOX` ersetzt.

## [0.20.4] - 2026-08-11

**Behebt eine Regression aus 0.20.2.** Wer 0.20.2 oder 0.20.3
installiert hat, sollte wechseln — dort lieferte `get_emails()` für
JEDE Zeile `message_id: null`, und der von `get_email()` gemeldete
`mailbox`-Wert war für Schreibaufrufe unbrauchbar.

> **Nach dem Update `refresh_index()` aufrufen.** Der erste Lauf ist
> groß: die unter 0.20.2/0.20.3 geschriebenen Zeilen tragen einen
> falschen Mailbox-Namen und werden komplett ersetzt.

### Fixed
- **Der Mailbox-Name schluckte Apples GUID-Verzeichnis.** Das echte
  Layout ist `<mailbox>.mbox/<GUID>/Data/…`; die Änderung aus 0.20.2
  sammelte jede Komponente bis `Data` und schrieb deshalb
  `INBOX/D85A1046-…` in den Index, wo `INBOX` hingehört. Zwei Folgen,
  beide aus dem Feld gemeldet:
  - Der stabile Header wird über `(account, mailbox, id)`
    nachgeschlagen — der Envelope Index sagt `INBOX`, der Index sagte
    `INBOX/D85A1046-…`. **Jede** Zeile **jeder** Auflistung kam mit
    `message_id: null` zurück, unabhängig vom Alter der Nachricht.
  - `get_email()` gab denselben Namen als `mailbox` heraus. Zurück an
    `set_flag()` gereicht — der Weg, den die Tool-Doku empfiehlt —
    scheiterte er mit "No mailbox matching".

  Ursache der Ursache: die Testfixtures für 0.20.2 waren **erfunden**.
  Sie beschrieben ein Layout ohne GUID-Verzeichnis, das ich nie an
  einem echten Mail-Verzeichnis geprüft hatte. Die neuen Fixtures
  benutzen Pfade aus einem realen Postfach und sind als solche
  markiert; der Fall "Untermailbox" ist ausdrücklich als unbelegt
  gekennzeichnet.
- Eine nicht existierende Mailbox ohne `account` meldet nicht mehr
  "does not exist in account None". Der Schreibpfad war in 0.20.2
  behoben, dieser Lesepfad übersehen.

## [0.20.3] - 2026-08-11

Aus der Nachuntersuchung des gemeldeten `message_id: null`. Die Ursache
war Latenz, kein Defekt — der schnelle Auflistungspfad liest Apples
Live-Index, der stabile Header kommt aus unserem, und der hängt
zwischen den Syncs hinterher. Belegt an einer festen Nachricht: vorher
`null`, nach `refresh_index()` gesetzt. Die Rohausgaben derselben
Untersuchung zeigten aber zwei Fehler, nach denen niemand gesucht hatte.

### Fixed
- **Der Mailbox-Scan verschwieg, wo er die Nachricht gefunden hat.**
  Strategie 3 antwortet genau dann, wenn der Index die Nachricht NICHT
  kennt — frisch eingetroffen, oder gerade von einem anderen Gerät
  verschoben. Das ist der einzige Fall, in dem der Aufrufer den Ort
  nicht selbst herleiten kann, und es war der einzige Rückgabepfad von
  vieren ohne `_with_location`. Das JXA-Skript hatte die Mailbox in der
  Hand und gab weder sie noch das Konto zurück. `CLAUDE.md` sagt seit
  jeher zu, dass `get_email` den aktuellen Ort meldet. Relevant über
  die Auskunft hinaus: die Mehrdeutigkeitsprüfung beim Schreiben
  verlangt genau `account` + `mailbox`.
- **Eine Schreibweise für den Message-ID-Header.** Die `.emlx` behält
  die spitzen Klammern, Apples `messageId` lässt sie weg — dieselbe
  Nachricht kam als `<a@b>` oder als `a@b` zurück, je nachdem welche
  Strategie geantwortet hatte. `search()` und `get_emails()` liefern
  immer die Klammerform; `get_email()` tut es jetzt auch. Für den
  Abgleich war das nie ein Problem (alles läuft über `_header_key`),
  für jeden, der Strings vergleicht, schon.

## [0.20.2] - 2026-08-11

Aus dem zweiten Health-Check. Der Check selbst lief durch (PASS,
freigegeben) — die drei gemeldeten Randnotizen führten aber auf einen
Fehler, der größer war als die Meldung.

### Fixed
- **Verschachtelte Mailboxen verloren ihren Pfad.** Apple legt eine
  Untermailbox als Kindverzeichnis ihrer Elternmailbox ab
  (`Archiv.mbox/2026.mbox/Data/…`); der Disk-Walk hörte beim ERSTEN
  `.mbox` auf und schrieb jede Nachricht aus `Archiv/2026` als in
  `Archiv` liegend in den Index. Apples Envelope Index leitet aus der
  Mailbox-URL dagegen `Archiv/2026` ab — und die Auflistung schlägt den
  stabilen Message-ID-Header über `(account, mailbox, id)` nach. Für
  jede Nachricht in einer Untermailbox ging er deshalb verloren, und
  kein Sync reparierte das: die Zeile war indiziert, nur unter einem
  Namen, nach dem niemand fragt.

  Aufgefallen ist das bei der Suche nach der Ursache eines gemeldeten
  `message_id: null`. Ob es DIESE Meldung erklärt, ist offen: der
  Melder hat keine verschachtelten Mailboxen. Der Fehler ist unabhängig
  davon reproduziert und behoben — die Ursache jener Beobachtung wird
  weiter gesucht.

  Betroffen war außerdem alles, was den Mailbox-Namen als Handle nutzt:
  Schreibziele, die Mehrdeutigkeitsprüfung und die Mailbox-Filterung
  der Suche.
  **Nach dem Update einmal `refresh_index()` laufen lassen** — die
  falsch benannten Zeilen heilen sich beim nächsten Sync selbst.
- Ein fehlender `account`- oder `mailbox`-Parameter wird in
  Fehlermeldungen **benannt** statt interpoliert: "the default account"
  statt "no such account: null".

### Added
- `get_index_status()` meldet `log_file_exists`, `log_file_bytes` und
  `log_file_modified`. Ein Client ohne Dateizugriff sah bisher nur den
  Pfad — "Logging ist konfiguriert" und "Logging funktioniert" sahen
  gleich aus. Der Inhalt bleibt auf der Platte: Log-Zeilen enthalten
  Betreffs und Dateipfade.

## [0.20.1] - 2026-08-11

Hotfix. Ein Health-Check auf echtem macOS hat zwei Fehler derselben
Bauart gefunden, die 0.20.0 mit grüner Testsuite ausgeliefert hat:
portierter Aufrufcode ohne das, was er aufruft — von Mocks verdeckt.

### Fixed
- **Jeder Schreibvorgang über eine `message_id` schlug fehl.** Das
  generierte JXA-Skript benutzte eine `writeFailed`-Map, die nirgends
  deklariert war: `ReferenceError: Can't find variable: writeFailed`,
  einmal pro Konto. Betroffen war genau die Referenz, die dieses
  Projekt empfiehlt.
- **Jeder Schreibvorgang über eine numerische ID, die der Index
  platzieren kann, schlug fehl.** `server.py` rief
  `manager.count_email_locations(...)` auf; die Methode existierte auf
  `IndexManager` nicht. In den Tests war der Manager ein MagicMock.
- **Ein bloßes `get_emails()` konnte für ALLE Konten antworten.** Ist
  der AccountMap-Cache kalt — `ensure_loaded()` spricht mit Mail und
  kann unter parallelen Aufrufen langsam sein oder scheitern — blieb
  die Konto-UUID `None` und die Envelope-Abfrage lief ungefiltert. Das
  Ergebnis sah aus wie eine korrekte Antwort auf eine andere Frage.
  Jetzt fällt der Pfad auf JXA zurück statt die Abfrage zu weiten.
- Eine rein numerische **Zeichenkette** ist die ID, nicht ein
  Message-ID-Header. MCP-Clients stringifizieren Zahlen routinemäßig;
  `"150540"` als Header zu lesen löste eine Suche in jedem sichtbaren
  Konto aus.

### Added
- `get_index_status()` liefert `failed_parse_examples` — Pfad, Mailbox,
  Grund und Detail der letzten Dead-Letter-Einträge. Die bloße Zahl war
  eine Sackgasse: sie sagt, dass drei Nachrichten fehlen, nicht welche.

### Tests
- Das generierte Schreib-Skript wird jetzt in node **ausgeführt**, für
  alle drei Gruppenformen und beide Fehlerpfade. Vorher wurde nur
  geprüft, ob ein Bezeichner im Text vorkommt — was bei einer nicht
  deklarierten Variablen grün ist.
- Ein AST-Test prüft, dass jede `manager.*`-Aufrufstelle in `server.py`
  auf `IndexManager` wirklich existiert.

## [0.20.0] - 2026-08-11

Three rounds of adversarial review over the 22 units prepared for
upstream found defects that had only ever been fixed on those branches.
This release brings all of them back into the shipped code.

### Added
- `get_emails(before_id=)` — the second half of a keyset cursor. Mail
  stores whole seconds, so a timestamp alone is not a position: every
  message sharing the oldest second of a page was unreachable. Pass the
  `date_received` **and** the `id` of the last row you saw. `before_id`
  without `before` is rejected rather than ignored, because silently
  dropping it returns the newest page — an endless loop for a
  backwards walk. An empty or blank `before` counts as no `before`.
- `refresh_index(full=True)` answers `"unconfirmed"` when the rebuild
  has not begun reading mail within a few seconds. Not knowing is not
  the same as "started", and a stuck build used to look identical to a
  healthy one.

### Fixed
- **A mailbox path is matched whole.** `projects/inbox` resolved the
  account's real INBOX whenever Mail listed it first, because only the
  last segment was compared. Asking for a nested folder now finds that
  folder or nothing.
- **An account whose mailboxes Mail refuses to list says so**, instead
  of "no mailbox matching X. Available: " — a verdict the lookup never
  established.
- **Every index writer takes the write lock**: the file watcher (it
  defers its batch rather than dropping it) and the single-row stale
  entry cleanup (it skips when the index is busy). `rebuild()` takes it
  before its DELETE.
- **A lock that could not be taken says so.** A lock file that cannot be
  created, or a filesystem whose `flock` answers `ENOTSUP`, now degrades
  loudly instead of returning what a real acquisition returns — and
  only genuine contention refuses, so a network home directory no
  longer makes the index permanently unwritable.
- **Attachments and links follow a moved message.** Both header read
  paths now ask Mail once every indexed location has turned out stale,
  so between two syncs a message could be read while
  `get_email_attachment()` reported it missing.
- **A live lookup searches the account that was asked for**, rather than
  stopping at the first hit anywhere and then filtering it out.
- **An id that exists in more than one mailbox is refused, not guessed.**
  Naming a `mailbox` without an `account` does not disambiguate it —
  every account has an INBOX.
- **A timed-out write reports an UNKNOWN outcome**, not one that never
  reached the message, and no longer reports a blank cause.
- **`account="all"` means the accounts Mail still has.** Apple's
  Envelope Index keeps rows for removed accounts; those came back under
  their bare UUID. Hidden accounts are now excluded in SQL rather than
  after `LIMIT`, so they no longer consume the page.
- **Full Disk Access is probed on every status call.** The answer came
  from a process-lifetime cache, so access revoked while the server ran
  still read as granted. A Mac where Mail was never set up is now
  diagnosed as that, not as a missing permission.
- **A zero-row index is built, not synced** — a sync reconciles what is
  already indexed and can never fill an empty database, so an
  interrupted first build left `state: "empty"` forever.
- **A failing finalization is recorded and still ends the build**, and
  the build flag now outlives the final flush, so a status call during
  the heaviest writes no longer answers "ready".
- **Connections die with their thread**, including when the OS hands a
  finished thread's id to a new one. Short-lived worker threads used to
  leak a SQLite file descriptor and an open transaction each.
- **`index`, `rebuild`, the startup sync and the watcher write their
  failures to the log file.** Installing a handler is not logging: under
  a desktop client stderr reaches nobody, so `server.log` stayed empty
  next to an index that had silently stopped updating. An existing
  world-readable log is tightened when it is opened.
- **Oversized messages are checked before the mailbox cap**, so a capped
  mailbox no longer swallows them without a dead-letter row; a parse
  returning nothing is recorded too; a failure after the insert removes
  the partial row so the next sync retries it; and a rebuild clears
  dead-letter rows whose file parses again.
- **A failing rollback no longer masks the error that caused it.**
- Attachment search returns the `rfc822_message_id` it selects — every
  hit used to raise `KeyError` in the caller.

### Changed
- The shipped documentation said the server has 8 tools in seven
  places. It has 12.

## [0.19.0] - 2026-07-28

### Added
- `get_emails(before=, after=, offset=)` — a mailbox can be walked
  backwards. Until now only the newest N per mailbox were reachable, so
  a stored cursor deep in the backlog could not be approached at all,
  and `search()` needs keywords and is useless for a gapless reverse
  scan. Pass the oldest `date_received` you have seen as `before` to get
  the next page; that is stable while new mail arrives, unlike `offset`.
- The JXA fallback refuses these parameters instead of dropping them —
  ignoring a window would page the same newest N forever and let the
  caller conclude the backlog is empty.

## [0.18.0] - 2026-07-28

Fork release (iret77). Adds write tools, stable message identity and
diagnostics on top of upstream 0.4.2.

The jump from 0.4.2 to 0.18.0 unifies two version lines that had drifted
apart: the `.mcpb` bundle was numbered per build and had reached 0.17.0,
while the package still carried upstream's 0.4.2. Package, `server.json`,
the bundle manifest and the launcher now share one number — two numbers
claimed a difference in content that does not exist, and made a bug
report something you had to translate.

### Added
- **Write tools** `set_flag(refs, color?)` and `set_read_status(refs, read?)`
  — single or batch (max 500), with per-reference outcome buckets
  (`updated`, `unchanged`, `not_found`, `failed`, `skipped_hidden`) so a
  batch never fails as a whole. Both honour `APPLE_MAIL_READ_ONLY`.
- **RFC822 Message-ID as a first-class reference.** Schema v6 stores
  `rfc822_message_id`; every read path returns it and every tool that
  takes a message accepts it. A header is never translated back into a
  ROWID and then trusted.
- **`flag_color`** in `get_email` and in listings, resolved for a whole
  page in one call.
- **`get_emails(account="all")`** lists across every visible account in
  one call; **`get_email([...])`** fetches up to 50 messages per call.
- **`get_index_status()`** and **`refresh_index(full?)`** — an MCP
  server's stderr reaches nobody, so the state has to be askable.
- Rotating file log at `~/.apple-mail-mcp/server.log`, owner-only.
- `APPLE_MAIL_INDEX_AUTO_BUILD`, `APPLE_MAIL_INDEX_MAX_EMAIL_MB`.
- Claude Desktop `.mcpb` bundle.

### Fixed
- An undecodable header (`Header` object instead of `str`) aborted the
  entire index sync.
- Oversized `.emlx` files were skipped silently; now recorded in the DLQ.
- `get_email` reported stale read/flagged state from the `.emlx` footer.
- Timestamps were emitted in UTC while Mail.app shows local time.
- Four causes of "database is locked": an open transaction after a failed
  sync, a shared SQLite connection across threads, a thread-only write
  lock where two processes contend, and one FTS trigger firing per row.
- Well-known mailboxes resolve by role, not by name — a German install has
  no "INBOX", it has "Posteingang".
- Message-ID matching ignores angle brackets and stray whitespace; the
  `.emlx` header keeps them, Apple's `messageId` property does not.
- **An incomplete search is no longer reported as absence.** A mailbox cap,
  an unreadable mailbox, a skipped trash folder, a timeout, a denied Apple
  Events permission or an unfinished recovery all leave the question open,
  and the result says so.

## [0.4.2] - 2026-07-02

### Added

- **`APPLE_MAIL_INDEX_EXCLUDE_ACCOUNTS` — hide entire accounts from the server.** A comma-separated env var (and `[index] exclude_accounts` TOML key) listing account *display names* (exact, case-sensitive, like `APPLE_MAIL_DEFAULT_ACCOUNT`) that should be invisible to the whole MCP server — not just skipped at index time. Excluded accounts are: never written to the index (`index`/`rebuild`/`--watch` skip them), filtered out of `search()` results (defense-in-depth for any stale rows), and gated out of the live tools — `list_accounts`, `list_mailboxes`, `get_emails`, and `get_email` all behave as if the account does not exist (empty results / "not found"), with no fall-through to JXA. `apple-mail-mcp status` and the `index://status` resource report the configured exclusions. Motivated by users with regulated-data accounts (e.g. PHI) on a shared machine who need a first-class way to keep an account out of LLM reach. Because account display names exist only via JXA (the index stores UUIDs), names are resolved to UUIDs with a single JXA call that runs *only* when exclusions are configured — the common no-exclusions path stays JXA-free. A configured name that matches no account is logged at WARNING and left unhidden, so typos fail loud rather than silently exposing mail. The `disk_email_count` stat (`status` / `index://status`) also skips excluded accounts (once resolved), so exclusions don't read as a fake index-coverage gap. **Known limitation:** `get_email()` called with a raw `message_id` and *no* account, for a message in an excluded account, can still be reached via the live JXA fallback (the account isn't known until the message is located). In practice the message id is undiscoverable through the (now-filtered) listing/search tools. (#90)

### Fixed

- **Clean "not found" errors instead of raw JXA noise.** A missing message id (`get_email`) and an unknown mailbox (`get_emails`) previously surfaced the underlying doubled-up JXA failure (`...Error: Error: Message not found... (-1728)` / `Can't get object. (-1728)`). These now return concise, model-friendly messages — `Message <id> not found.` and `Mailbox '<name>' not found in account '<account>'.` — while genuine failures (Mail.app down, permissions) still propagate intact. (#76)
- **`get_emails()` no longer returns `[]` for Gmail-backed accounts**: the Envelope Index fast path matched mailbox membership only via the `messages.mailbox` FK and a raw-name `LIKE` on the mailbox URL. Gmail accounts keep every message in `[Gmail]/All Mail`, with INBOX (and other label-backed mailbox) membership recorded only in the `labels` table, so INBOX queries matched zero rows and returned a silent empty list; mailbox names containing spaces or brackets ("Sent Mail", "All Mail") also never matched because URLs store percent-encoded paths. The fast path now resolves the requested mailbox to ROWIDs first (percent-decoding URLs, matching the full path or its bare final segment, case-insensitively) and treats a message as a member via either `messages.mailbox` or a `labels` row. An unknown mailbox raises the new `MailboxNotFoundError` and falls back to JXA instead of masquerading as an empty mailbox. Two adjacent silent-scoping holes are closed in the same path: an unresolvable account name now falls back to JXA instead of silently querying every account, and an unspecified account scopes to the first account (matching the documented default and the JXA path) instead of all of them. (#102)

## [0.4.1] - 2026-06-12

### Fixed

- **Watcher transaction discipline** — `_process_pending()` ran deletes and adds inside one implicit transaction with a single commit and no rollback on `sqlite3.Error`. Python's `sqlite3` holds an implicit transaction open until commit/rollback, so a mid-batch failure left partial work pending that the *next* batch's commit would silently persist. Deletes and adds now commit as separate transactions, each rolling back on failure. Both `watcher.py` and `sync.py` also switch from a separate `SELECT last_insert_rowid()` statement to `cursor.lastrowid`, which can't be invalidated by interleaved statements. (#95)
- **Inline images without a MIME filename are now indexed and retrievable** — `multipart/related` HTML email (newsletters, marketing, rich-formatted business mail) embeds images as inline parts with a `Content-ID` and no filename. `_extract_attachments` dropped them, so `attachment_count` read 0 and the parts were unreachable. Such parts now get a synthetic `inline_<cid>.<ext>` filename derived from the sanitized Content-ID plus a mime-type extension; `get_attachment_content` derives the same name, so the advertised filename retrieves the bytes. (#86)
- **Input validation at the MCP tool boundary** — `limit`/`offset` are clamped (negative `LIMIT` means *unlimited* in SQLite; oversized limits push entire result sets into the model's context — ceiling is 200), `before`/`after` must be canonical zero-padded `YYYY-MM-DD` (malformed dates previously flowed into SQL string comparisons and silently returned wrong results; the explicit `ValueError` lets the calling model self-correct), and the `APPLE_MAIL_STRATEGY3_*` env vars are clamped to sane ranges. (#96)

### Performance

- **`_flush_batch` no longer issues one SELECT per attachment-bearing email** — rows with attachments insert individually via `cursor.lastrowid` (`INSERT OR REPLACE` always yields a fresh rowid); the attachment-free majority keeps the `executemany` fast path. Removes hundreds of redundant queries per batch during full index builds. (#97)

### Changed

- **CI now runs the test suite** — `lint.yml` previously ran only ruff; the suite ran exclusively on the maintainer's machine. A new job runs pytest on `macos-latest` across Python 3.11/3.12/3.13. (#94)
- **`tests/test_v016.py` split by feature** — the version-named regression dump's 9 classes moved to `test_watcher.py`, `test_disk.py`, `test_server.py`, and `test_cli_profile.py`. Also adds the suite's first real-file SQLite contention test (the shared fixtures use `:memory:`, which never locks). (#98)
- **`search()` docstring documents the empty-result shape** — the deliberate `{"result": [], "hint": ...}` dict on zero matches is now described in the Returns section. (#99)

## [0.4.0] - 2026-05-28

### Performance

- **`list_accounts()` and `get_emails()` now skip the AppleScript round-trip on the fast path** — both tools previously spawned `osascript` for every call, paying the JXA IPC ceiling (~150ms and ~1.2s respectively on a ~73K-message mailbox). `list_accounts()` now serves from the existing `AccountMap` cache (5-minute TTL) when warm — repeat calls within a session drop from ~150ms to ~1ms. `get_emails()` reads Apple's Envelope Index SQLite directly (the same `~/Library/Mail/V*/MailData/Envelope Index` that BastianZim, rusty, and pl-lyfx query), joining through the `subjects` and `addresses` lookup tables to materialize text columns — every filter (`all`, `unread`, `flagged`, `today`, `last_7_days`, `this_week`) is served from direct integer columns on `messages` without any JXA fallback for live state. Measured 75–250× speedup on a ~73K-message mailbox: list_accounts 153ms→~2ms (warm), get_emails 1247ms→~5ms. Both tools cascade to the existing JXA path automatically if the Envelope Index isn't accessible (schema mismatch, missing file, restrictive permissions), preserving correctness on any Mail.app build. New module `index/envelope_direct.py` with 22 unit tests; `index/accounts.py` gains `reset()` and `get_cached_accounts()` for the cache path and test isolation.

### Added

- **TOML configuration file at `~/.apple-mail-mcp/config.toml`** — every existing `APPLE_MAIL_*` env var now has a sibling key in a structured TOML file. Resolution order is CLI flag > environment variable > file value > built-in default, so existing env-only deployments keep working unchanged. The file is for durable user policy (default account/mailbox, index scope, read-only) that's awkward to maintain across multiple MCP client configs — set it once in `config.toml` instead of pasting the same `env: {}` block into Claude Desktop + Cursor + Cline. Schema is versioned (`config_version = 1`) and validated with file-path context: bad keys, wrong types, negative values, version mismatches, and a subtle bool-in-int-slot trap all fail loud rather than silently degrading. The "empty list = explicit empty, not default" semantics are intentional and tested — `exclude_mailboxes = []` means "no exclusions" rather than falling back to the `{"Drafts"}` default. New `tomllib`-based loader (stdlib in 3.11+) with no added runtime dependency. 33 new tests in `tests/test_config.py` cover the precedence semantics across all four layers.
- **`apple-mail-mcp init` CLI command** — writes a heavily-commented `config.toml` template to `~/.apple-mail-mcp/`. Every available key is documented inline alongside its matching env var; all values are commented out so the template preserves current defaults and users opt in by uncommenting. The file is written with `0o600` permissions, matching the project's existing posture for `index.db` and the attachment cache. `--force` overrides an existing file. A roundtrip test loads the template back through the validator on every run, catching any drift between the schema and the documentation before it reaches a user.
- **Read-only mode is now enforced at MCP tool boundaries (#80)** — `_ensure_writable()` helper in `server.py` raises `PermissionError` when read-only is active (via env, TOML key, or `apple-mail-mcp serve -r`). Future write tools must call this as their first line. An AST-based regression test in `tests/test_server.py` scans `server.py` for `@mcp.tool` functions whose names start with write-implying prefixes (`mark_`, `move_`, `send_`, `reply_`, `forward_`, `delete_`, `create_`, `update_`, `set_`, `archive_`, `trash_`, `flag_`, `unflag_`) and asserts each one calls the guard. Passes vacuously today (no write tools exist), fires the moment a contributor forgets the check on a future write tool. The flag was decorative before; the infrastructure now in place keeps it honest as the write-ops cluster (#22, #23, #24, #64, #65) lands.

### Documentation

- **`docs/configuration.md`** restructured around TOML-first with env vars as overrides. New "Precedence" section documents the CLI > env > file > default order, and the env-var table gains a matching TOML-key column.
- **`CLAUDE.md` Configuration section** updated with the new precedence model, matching TOML key column, empty-list semantics note, and #80 enforcement pointer. `apple-mail-mcp init` added to the CLI Commands list.
- **README** gains a brief "Configure (Optional)" subsection pointing to `apple-mail-mcp init` and the configuration docs.

## [0.3.3] - 2026-05-14

### Fixed

- **External-attachment lookup for nested MIME parts** — Apple Mail stores externally-referenced attachments under `Attachments/<msg_id>/<part>/` where `<part>` is an RFC-style MIME part number. Top-level parts use flat integers (`2/`), but nested parts (common in forwarded emails: `multipart/mixed > multipart/mixed > application/pdf`) use dot notation (`2.2/`, `1.16/`, etc.). The previous implementation tracked a flat attachment counter and routed every lookup to a top-level subdir, so any nested attachment came back as `size: 0` from `parse_emlx()` and `None` from `get_attachment_content()`. New `_mime_part_numbers()` helper walks the MIME tree and builds an `id(part) → "2.2"` map; `_extract_attachments()`, `get_attachment_content()`, and `_find_external_attachment()` now use real part numbers instead of the flat counter. Scoped check against a real ~72K-message mailbox: 4,063 dot-notation subdirs (~18% of all attachments) were affected; flat-attachment behavior is preserved (regression test included). Thanks to @scottwb for the fix and tests. (#85)
- **Hardened `_mime_part_numbers` lookup fallback** — `_extract_attachments` and `get_attachment_content` used `part_numbers.get(id(part), "")` to resolve the MIME part number for the external-attachment subdir lookup. An empty-string fallback would silently collapse the `Path / "<subdir>"` join to the attachments root directory (`Path("/x") / "" == Path("/x")`), potentially returning a wrong file via the single-file-in-dir fallback in `_find_external_attachment`. Both call sites now check for a missing part number explicitly and skip the external lookup rather than misroute. Defensive — doesn't trigger today (the helper covers every leaf part), but locks in the invariant for future refactors. Two new regression tests in `TestMimePartNumbersFallback`.

### Security

- **Attachment cache files are now chmod'd to `0o600`** — `get_email_attachment` and `get_attachment` write extracted attachment content to `~/.apple-mail-mcp/attachments/<random>/<filename>`. The cache directory was already `0o700`, but the file itself inherited the user's umask (typically `0o644`). On single-user installs this is moot, but on shared hosts (CI runners, multi-user dev VMs, lab machines) other local users could read the cached attachment content before the 24-hour cleanup. The file is now explicitly `chmod`'d to `0o600` immediately after write, matching the existing `0o600` posture documented for the index database. New regression test in `TestGetAttachment`. (#79)

### Changed

- **CI workflows opt into Node 24 ahead of the forced cutover** — `lint.yml` and `release.yml` now set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` at the workflow level. GitHub will force all JavaScript actions to Node 24 by default on June 2nd, 2026, and remove Node 20 from runners on September 16th, 2026. Opting in now silences the deprecation warning that appears on every Actions run today and verifies our pinned action versions (`actions/checkout@v4`, `astral-sh/setup-uv@v5`, `actions/upload-artifact@v4`, `actions/download-artifact@v4`, `pypa/gh-action-pypi-publish@release/v1`) work correctly on the newer runtime well before it becomes mandatory.

## [0.3.2] - 2026-05-10

### Added

- **`--profile PATH` flag on `index` and `rebuild`** — wraps the operation in `cProfile` and writes a `pstats`-format dump to the given path. Stdlib-only (no new runtime dependencies). Intended for users diagnosing slow indexing on their own data and for sharing actionable performance traces in bug reports. Documented in `docs/profiling.md` along with recommended visualizers (`flameprof` for flame charts, `gprof2dot` for call graphs, `snakeviz` for interactive exploration). Surfaced in response to community feedback on #60 — wall-clock invariance at 100k+ mailbox scale was hard to diagnose without a contributor-friendly profiling path. Open follow-up tracked in #84 (sync inventory walk dominates wall-clock at >100k mailboxes) — large-mailbox contributors are explicitly asked to share `--profile` dumps there. `flameprof>=0.4` added to the `dev` dependency group so contributors get a flame-chart renderer on `uv sync`.
- **`docs/profiling.md`** — methodology page covering when to profile, how to capture, how to read the two halves of a flame chart (cumulative vs self-time), and what patterns in a profile signal which optimization strategies. Includes a reference breakdown from a real ~60k-message mailbox showing balanced overhead across the parse pipeline (no single dominant bottleneck on this dataset). Wired into the mkdocs nav.

### Fixed

- **`index://status` resource no longer walks the disk on every call** — `IndexManager.get_stats()` now caches `disk_email_count` with a 60-second TTL via a new `_get_disk_email_count_cached()` helper. Previously, every read of the `index://status` MCP resource triggered a full `get_disk_inventory()` filesystem walk under `~/Library/Mail/V*/`, which is O(N files) and would dominate response latency for clients polling the resource on a tight loop. The cache is automatically invalidated at the end of `build_from_disk()` and `sync_updates()` so the next status call after a sync reflects truth. Failures (`PermissionError`, `FileNotFoundError`) are deliberately *not* cached so subsequent calls retry in case Full Disk Access has since been granted. New public `invalidate_disk_count_cache()` method for callers that need to force a fresh read. (#78)
- **Memory allocation in `_estimate_attachment_size`** — replaced chained `raw.replace("\n", "").replace("\r", "").replace(" ", "")` with `str.count()` (allocation-free) followed by a bounded trailing-padding scan. Verified via `tracemalloc` on a 19.3 MB base64 payload: peak allocation drops from **38.4 MB → 19.3 MB** (2x reduction; new impl peak equals input size, allocating nothing additional). The original issue's "80 MB" estimate assumed all three `.replace()` calls would each allocate a full copy, but CPython short-circuits `str.replace` when no replacements occur — so the realistic peak is 2x not 4x. Still real, still GC-pressure-inducing during bulk indexing of attachment-heavy mailboxes. The fix preserves exact semantics — including the padding subtraction that the originally-proposed Option B (`int(len(raw) * 0.75)`) would have lost — verified by the existing `test_base64_size_estimation` test plus a new whitespace-heavy regression test. (#81)

## [0.3.1] - 2026-05-08

### Changed

- **`APPLE_MAIL_INDEX_MAX_EMAILS` is now uncapped by default** — the per-mailbox ceiling that silently truncated large mailboxes at 5000 messages is gone. The env var still works as an opt-in ceiling for users who want to bound disk/memory usage; setting it to an integer enforces the same per-mailbox limit as before. `get_index_max_emails()` returns `int | None` and all consumers (sync, build_from_disk, get_stats) treat `None` as no cap. The 5000 default predated disk-first sync (when JXA timed out at 60s and 5000 was the realistic indexable count); the bottleneck has long since moved, and the silent default no longer matched the README's "full-coverage body search" claim. Existing indexes built under the old default will *not* automatically backfill older messages — run `apple-mail-mcp rebuild` to re-index without the cap.
- **`apple-mail-mcp status` surfaces capped mailboxes** — when `APPLE_MAIL_INDEX_MAX_EMAILS` is set and any mailbox is at the ceiling, `status` now prints a "Capped: N mailbox(es)" line with a hint to raise or unset the env var. Previously this state was only visible via the `index://status` MCP resource.

### Documentation

- **Removed the "Migrating from apple-mcp?" README section** — the `supermemoryai/apple-mcp` migration table was kept for compatibility framing during the early v0.x cycle. The reference is no longer load-bearing for new users; dropping it tightens the README without losing information that's still findable in v0.3.0 release notes if needed.

## [0.3.0] - 2026-05-07

### Fixed

- **Watcher race during `build_from_disk()`** — in `IndexManager.build_from_disk` the FTS5 sync triggers were dropped for the bulk-insert pass and recreated *after* `rebuild_fts_index()`. Any concurrent INSERT (file watcher, separate process holding `--watch`) that landed between the FTS rebuild and the trigger recreation entered `emails` but never reached `emails_fts`, leaving the row permanently unsearchable. The trigger recreation now happens *before* the FTS rebuild — concurrent inserts during the rebuild fire the recreated trigger, and the rebuild itself re-syncs everything in `emails`, double-covering the window. (Surfaced via Gemini code review.)
- **Stale FTS5 entry auto-cleanup** — when `get_email()` Strategy 0 finds an indexed `.emlx` path that no longer exists on disk (the message was deleted or moved between syncs), the dead row is now removed from the index and a clear `"deleted or moved"` error is returned. Previously the cascade fell through to Strategy 3 and timed out (~1.3% of `get_email` calls in observed traffic). Adds `IndexManager.delete_email()` primitive. (#74)
- **Dead letter queue for `.emlx` parse failures** — files that fail to parse in the watcher or disk-sync paths are now recorded in a new `failed_index_jobs` table (path, account, mailbox, error type/message, first/last seen, attempt count). Previously such failures were swallowed silently after the v0.1.8 watcher hardening. Successful re-parses automatically clear the entry. Surfaced in `apple-mail-mcp status` and the `index://status` resource via a new `failed_jobs_count`. Schema bumped to v5 with a forward-only migration. (#58)
- **DLQ write failures now log at ERROR level** — when the `failed_index_jobs` INSERT itself fails (disk full, DB corruption, schema mismatch), both the watcher and disk-sync paths now log at ERROR with diagnostic context instead of swallowing silently (sync) or emitting WARNING (watcher). Surfaces operationally-significant failure modes that were previously invisible. (#77)

### Changed

- **`cyclopts` constraint relaxed to stable** — was `>=5.0.0a1` (pre-release), now `>=4.10`. Removes the need for `--prerelease=allow` in `claude_desktop_config.json` and other install configs. No API changes; the cyclopts surface used by `cli.py` is identical between 4.x and 5.x. (#75)
- **HTML stripping during indexing now uses selectolax** (lexbor C parser) for ~5x faster `_strip_html()` on realistic email HTML (5-25 KB body parts). BeautifulSoup is kept as a fallback if selectolax raises or fails to import. All existing XSS-bypass tests pass under both paths. New `selectolax>=0.4.8` dependency. (#59)
- **`sync_from_disk()` now uses a SQL temp table for diffing** instead of materializing the full disk and DB inventories as Python dicts. Memory at sync time stays flat (~2-3 MB delta) regardless of mailbox size; previously the dicts grew linearly to ~116 MB at 200K emails. Time cost is ~1.8x (sub-second even at 200K). Adds `iter_disk_inventory()` streaming variant of `get_disk_inventory()` in `disk.py`. All existing sync tests pass — behavior is preserved (added/deleted/moved counts, mtime sort, per-mailbox cap). (#60)

### Added

- **`index://status` MCP resource** — read-only JSON snapshot of the FTS5 search index (counts, size, last sync, staleness). Lets MCP clients assess index health without invoking a tool. (#12)
- **Benchmark suite expansion + refresh** — added `sweetrb/apple-mail-mcp` (TypeScript, AppleScript-based, 40+ tools, npm) and `BastianZim/apple-mail-mcp` (Python, reads Envelope Index SQLite + `.emlx` directly, no AppleScript) to the competitor list, then re-ran the full sweep on a 72K-message mailbox. Charts and the benchmarks doc are refreshed; positioning copy updated to reflect that the FTS5 differentiator is now precisely "full-coverage body search" — BastianZim implements a body parameter but caps live-scanning at the 5000 most recent messages (silent miss on older mail). Per-scenario charts now mark BastianZim as "5K cap" in the capability matrix and exclude it from the body-search bar chart so the comparison stays apples-to-apples.
- **`server.json` declares `runtimeHint: "uvx"`** — spec-compliant signal to MCP registries that the canonical launch command is `uvx apple-mail-mcp`. No effect on existing clients that already invoke the package directly.

### Documentation

- **Discovery descriptions refreshed** — `pyproject.toml`, `server.json`, and `mkdocs.yml` all now describe the project as "Apple Mail MCP server with full-coverage FTS5 body search. Reliable on large mailboxes where AppleScript-based servers timeout." Replaces the older "the only one that works reliably" wording, which the v0.3.0 bench refresh showed was no longer uniquely ours (BastianZim also handles large mailboxes — just with a 5000-message body-search cap).
- **Schema-version references updated to v5** across `CLAUDE.md`, `docs/architecture.md`, and `docs/search.md`.
- **New documentation sections** for the `index://status` MCP resource (`CLAUDE.md`, `docs/architecture.md`, `docs/tools.md`) and the `failed_index_jobs` DLQ (`docs/search.md`, `docs/troubleshooting.md`).

## [0.2.2] - 2026-04-13

### Added

- **Mailbox name alias resolution** — JXA `getMailbox()` now resolves common cross-provider aliases (e.g., `Sent Messages` → `Sent Items` on Outlook, `Trash` → `Deleted Items`) and falls back to case-insensitive matching. (#73)
- **Configurable Strategy 3 timeout** — Strategy 3 (iterate-all-mailboxes fallback in `get_email`) now exposes `APPLE_MAIL_STRATEGY3_TIMEOUT` and `APPLE_MAIL_STRATEGY3_MAX_MAILBOXES` environment variables.

### Fixed

- **Bare wildcard `*` query** — no longer crashes FTS5 with a syntax error. (#72)

### Tests

- Added watcher tests for noisy events, nested mbox, V11 directory layouts, and pending-changes limits.
- Added corrupt `.emlx` parser tests (bad byte counts, truncated content, empty files, missing newline). +10 tests; total 325 passing.

## [0.2.1] - 2026-04-05

### Added

- **Search pagination** — new `offset` parameter on `search()` for paginated results. Use with `limit` to page through large result sets. (#8)
- **Status command completeness** — `apple-mail-mcp status` now reports attachment count, disk email count, and index coverage percentage. (#43)

### Changed

- **Strategy 3 JXA moved to builders.py** — the inline JXA script for iterating all mailboxes is now a `GetEmailBuilder` class, consistent with the existing builder pattern. (#56)

### Fixed

- **Case-insensitive attachment filename matching** — `get_email_attachment` now matches filenames regardless of case or whitespace, fixing failures when LLM clients re-serialize filenames with minor differences. (#71)

## [0.2.0] - 2026-04-01

### Added

- **Date-range filtering for search** — new `before` and `after` parameters (YYYY-MM-DD) on `search()`. Filter results by date across all scopes including attachments. (#9)
- **Highlighted search results** — new `highlight` parameter on `search()`. When enabled, matched terms are wrapped in `**markers**` in subject and content_snippet using FTS5 `highlight()` and `snippet()`. (#11)
- **`get_email_links()` tool** — extracts hyperlinks from an email's HTML content. Replaces the links mode of `get_attachment()`. (#55)
- **`get_email_attachment()` tool** — extracts a named file attachment and saves to disk. Replaces the attachment mode of `get_attachment()`. (#55)
- **CLI wrappers** — all MCP tools now accessible as CLI commands: `search`, `read`, `emails`, `accounts`, `mailboxes`, `extract`. Output JSON to stdout. (#61)
- **Skill generator** — `apple-mail-mcp integrate claude` generates a Claude Code skill file for CLI-based email access. (#62)
- **`--read-only` server flag** — `apple-mail-mcp serve --read-only` (or `APPLE_MAIL_READ_ONLY=true`) prepares for v0.3.0 write operations. (#63)
- **Dynamic Mail version detection** — auto-detects the highest `V*` directory under `~/Library/Mail/` instead of hardcoding `V10`. (#57)

### Changed

- **`get_attachment()` deprecated** — still registered for backwards compatibility, but delegates to `get_email_links()` or `get_email_attachment()`. Will be removed in v0.3.0.

### Fixed (from v0.1.8)

- **Watcher crash on file add** — `parse_emlx()` exceptions beyond `OSError`/`ValueError`/`UnicodeDecodeError` (e.g. malformed plist, missing headers) no longer kill the watcher thread. The watcher now skips unparseable files and continues processing.
- **Attachment cache leak** — `_cleanup_old_attachments()` is now called automatically when extracting attachments, preventing unbounded disk usage from cached files.
- **Attachment cache permissions** — cache directory is now created with `0o700` permissions to protect sensitive email attachment content.
- **Empty search error messages** — search index errors (corrupt DB, SQLite issues) now return actionable error messages instead of empty strings. Suggests `apple-mail-mcp rebuild` when the index is broken.
- **Misleading get_email timeout message** — when `get_email` times out, the error now checks whether account/mailbox were already provided and gives context-appropriate advice instead of always saying "Provide account/mailbox".
- **Renamed `this_week` filter to `last_7_days`** — `this_week` kept as alias for backwards compatibility. (#49)
- **`search_fts_highlight()` bugs** — fixed missing account/mailbox/exclude_mailboxes filters, integer row indexing, and missing FTS5 retry logic.
- **Case-sensitive mailbox filtering** — `search(mailbox="INBOX")` now matches `Inbox`, `inbox`, etc. Previously returned zero results on case mismatch. (#67)
- **Updated patrickfreyer benchmark config** and added `rusty_apple_mail_mcp` to benchmarks.

## [0.1.7] - 2026-03-11

### Added

- **Strategy 0 (disk read) for `get_email()`** — reads email content directly from `.emlx` files on disk, bypassing JXA/Apple Events entirely. Fastest path when the search index is available. Falls through to JXA strategies on failure. (Thanks to @vkostakos for the initial implementation in PR #53)
- Extracts read/flagged status from `.emlx` plist footer flags bitmask
- Extracts `date_sent`, `reply_to`, `Message-ID` from MIME headers for full schema parity
- `get_email` benchmark scenario with dynamic message ID discovery
- `CONTRIBUTING.md` for new contributors
- This changelog

### Fixed

- `date_received` now uses the `Received` header (delivery time) instead of `Date` header (composition time). Previously both `date_received` and `date_sent` were identical. Run `apple-mail-mcp rebuild` after upgrading to fix historical emails.

### Changed

- Updated project messaging across all descriptions to reflect disk-first architecture
- Re-ran competitive benchmarks with new `get_email` scenario
- Updated all docs, descriptions, and online listings for v0.1.7

## [0.1.6] - 2026-03-08

### Changed

- Hardened benchmark harness with error detection, probe screening, and crash guards
- Updated documentation and charts with corrected benchmark results
- Bumped `server.json` to 0.1.6

## [0.1.5] - 2026-03-06

### Added

- External attachment support (reads from `.mbox` sibling directories)
- Scan hardening for corrupt/oversized `.emlx` files
- Mailbox cap documentation and warnings

### Fixed

- Guard external attachment reads against oversized files
- Path traversal guard for attachment extraction

## [0.1.4] - 2026-03-04

### Fixed

- `.partial.emlx` file indexing
- Public API exports
- Attachment fidelity in parsed results
- Scan resilience for edge cases

## [0.1.3] - 2026-03-02

### Added

- Attachment support with FTS5 sanitizer rewrite
- 3-strategy `get_email()` cascade (specified mailbox, index lookup, iterate all)
- Schema v4 with `attachments` table

### Fixed

- Strategy 2 over-scoping by defaults
- Race-safe mtime sort
- FK pragma, `message_id` scoping, `exclude_mailboxes`

## [0.1.2] - 2026-02-28

### Added

- MCP Registry manifest (`server.json`)

### Fixed

- FTS5 search now respects account/mailbox filters (#4)
- FTS5 mailbox filter regression
- Async lock to prevent concurrent `ensure_loaded()` races

## [0.1.1] - 2026-02-25

### Added

- Documentation site (GitHub Pages)
- Competitive benchmarking suite against 7 Apple Mail MCP servers

## [0.1.0] - 2026-02-22

### Added

- Initial release
- Fast MCP server for Apple Mail with batch JXA (87x faster than naive iteration)
- FTS5 search index (700-3500x faster body search)
- 6 MCP tools: `list_accounts`, `list_mailboxes`, `get_emails`, `get_email`, `search`, `get_attachment`
- Disk-based sync for index building
- Real-time file watcher for index updates

[0.3.2]: https://github.com/imdinu/apple-mail-mcp/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/imdinu/apple-mail-mcp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/imdinu/apple-mail-mcp/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/imdinu/apple-mail-mcp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/imdinu/apple-mail-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.8...v0.2.0
[0.1.8]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/imdinu/apple-mail-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/imdinu/apple-mail-mcp/releases/tag/v0.1.0
