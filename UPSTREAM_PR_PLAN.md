# Upstream-PR-Plan — iret77/apple-mail-mcp → imdinu/apple-mail-mcp

Stand: 2026-07-27. Basis geprüft: `git merge-base upstream/main feat/write-ops-flag-read`
== `upstream/main` HEAD (`ee655d4`). **Upstream ist uns 0 Commits voraus**, wir sind 30
voraus. Kein Rebase nötig, keine Konflikte zu erwarten.

Umfang gesamt: 22 Dateien, +7025/−217.

## Vorgaben aus dem Upstream-CONTRIBUTING.md

- „Keep the diff focused — avoid unrelated changes in the same PR."
- „PRs are typically squash-merged into `main`." → unsere Commit-Historie landet
  nicht im Upstream; **entscheidend ist der Diff pro PR**, nicht unsere Reihenfolge.
- Pflicht vor jedem PR: `ruff check src/`, `ruff format --check src/`, `pytest`.
- Tests gehören dazu; die meisten mocken JXA und laufen ohne Mail.app.
- Lizenz: GPL-3.0, Beiträge werden darunter lizenziert.

## Was Upstream schon hat (geprüft, nicht vermutet)

- `_ensure_writable()` + `APPLE_MAIL_READ_ONLY` (#80) — **das Gate existiert, aber
  bewacht bisher nichts.** Starkes Argument für den Write-PR.
- Die Resource `index://status`.
- 8 Tools.

## Was NICHT upstream gehen darf

| Sache | Wo | Grund |
|---|---|---|
| `mcpb/`, `scripts/build-mcpb.sh`, `dist/` | eigener Branch | Fork-Distribution |
| `install_mode` / `source_ref` / `APPLE_MAIL_MCP_LAUNCHER` / `APPLE_MAIL_MCP_REF` | server.py:478–498, 2724 | beschreibt unseren Bundle-Launcher |
| `SERVER_REVISION` | server.py:418 | unser Build-Stempel |
| README-Absatz zum `.mcpb`-Bundle | README.md | Fork-spezifisch |
| CLAUDE.md-Passagen mit Fork-Bezug | CLAUDE.md | prüfen, Rest ist gute Doku |

## Struktureller Aufwand, der vor dem ersten PR zu leisten ist

1. **Tests umverteilen.** Alles Neue liegt in `tests/test_write_ops.py` (3059 Zeilen) —
   ein Sammelbecken, benannt nach unserem Branch. Upstream erwartet die Tests in der
   jeweils passenden Datei: `test_disk.py`, `test_manager.py`, `test_server.py`,
   `test_sync.py`, `test_watcher.py`, `test_config.py`. Nur die echten Write-Tool-Tests
   bleiben in einer neuen `test_write_ops.py`.
2. **Branches neu schneiden, nicht cherry-picken.** Unsere Historie verschränkt Themen
   (mehrere „fix: N defects found by review"-Commits korrigieren jeweils frühere).
   Jeder Upstream-Branch wird von `upstream/main` aus mit dem **Endzustand** der
   betroffenen Dateien gebaut, nicht durch Nachspielen der Commits.
3. **Fork-Spezifika ausbauen** (Tabelle oben), bevor der Diff entsteht.

---

# Die PR-Kette

Reihenfolge = Abhängigkeit. PR 1–3 sind untereinander unabhängig und können parallel
laufen; ab PR 4 baut jeder auf dem vorigen auf.

## PR 1 — `.emlx`-Parsing: unlesbare Header und übergroße Dateien

**Branch:** `fix/emlx-parsing-robustness` · **Basis:** `upstream/main`
**Dateien:** `index/disk.py`, `index/sync.py`, `index/schema.py`, `config.py`
**Quelle:** `887c7e2`, Header-Teil von `439afa8`, `1343067`

Ein einziger nicht-ASCII-Header (`Subject`, `Received`, `Content-ID`, Dateiname eines
Anhangs) ließ Pythons `email`-Modul ein `Header`-Objekt statt `str` liefern; der erste
`.strip()` warf `AttributeError` und **jeder Sync brach ab** — bei uns einen Tag lang,
unbemerkt, weil der Fehler nur auf stderr ging. Dazu: übergroße `.emlx` verschwanden
still.

- `header_text()` / `_filename_text()` — jeder Headerzugriff im Parser liefert
  garantiert dekodierten `str`. Ein Guard-Test stellt sicher, dass im Parser kein
  roher `msg["…"]`-Zugriff zurückkehrt.
- Der Per-Message-Guard im Sync fängt `Exception` statt nur `(OSError, ValueError,
  UnicodeDecodeError)` — eine kaputte Mail darf nie den ganzen Lauf killen.
- Übergroße Mails landen als `too_large` in der DLQ statt im Nichts;
  `APPLE_MAIL_INDEX_MAX_EMAIL_MB` (Default 25) macht die Grenze konfigurierbar.
- Zählwerte (`failed_jobs_count`, `skipped_too_large`) über die **bestehende**
  Resource `index://status` sichtbar — kein neues Tool nötig.

**Risiko:** gering, rein defensiv. **Warum zuerst:** heilt einen Totalausfall und
hängt an nichts.

## PR 2 — Index-Schreibzugriffe: Nebenläufigkeit und Transaktionen

**Branch:** `fix/index-write-durability` · **Basis:** `upstream/main`
**Dateien:** `index/manager.py`, `index/watcher.py`
**Quelle:** `42f454f`, `29fa029`, `e422543`, `4a07445`, `f1a1048`

Vier reale „database is locked"-Ursachen, nacheinander gefunden:

- **Verbindungen pro Thread** (`threading.local`) — ein Rebuild im Hintergrund-Thread
  legte sonst den Server lahm.
- **Rollback bei fehlgeschlagenem Sync** — eine abgebrochene Transaktion blieb offen
  und blockierte danach *jeden* Schreibzugriff.
- **Cross-Prozess-Lock** (`fcntl.flock` auf `<index>.lock`). Ein `threading.Lock`
  reicht nicht: Claude Desktop startet den Server **zweimal** (Upstream-Issue #106).
  Fällt auf Thread-Lock zurück, wenn die Lockdatei nicht anlegbar ist.
- **FTS-Trigger vor dem `DELETE` fallen lassen**, Leeren per einmaligem
  `INSERT INTO emails_fts(emails_fts) VALUES('delete-all')`, Trigger als **erste**
  Aktion im `finally` wiederherstellen. DDL committet implizit — ein Rollback holt
  sie nicht zurück, ein Fehler hätte den Index dauerhaft ohne Trigger hinterlassen.
- Der Watcher benutzt dasselbe Lock und verliert beim Draining keinen Batch mehr.

**Risiko:** mittel — betrifft den Kern. Vollständig durch `test_manager.py` /
`test_watcher.py` abgedeckt. **Für Upstream besonders relevant**, weil #106 (zwei
Instanzen) dort offen ist.

## PR 3 — Gelesen/Markiert live statt aus dem `.emlx`-Footer; lokale Zeitstempel

**Branch:** `fix/live-flags-and-local-time` · **Basis:** `upstream/main`
**Dateien:** `server.py`, `index/envelope_direct.py`
**Quelle:** `88c49f2`, Zeitzonen-Teil von `439afa8`

- `get_email()` las Gelesen-/Markiert-Status aus dem Plist-Footer der `.emlx`. Den
  schreibt Mail nicht zuverlässig neu — nach einer Änderung in Mail.app lieferte
  Strategy 0 veraltete Werte. `_overlay_live_flags()` überlagert sie aus Apples
  Envelope Index (ein Read-only-Select, `fetch_message_flags`).
- Alle Zeitstempel gingen als UTC raus, Mail.app zeigt Ortszeit — bei uns 12:54 statt
  14:54. `to_local_iso()` konvertiert an jeder Ausgabegrenze über die **System**-Zone
  (`astimezone()`, DST-korrekt), nie über eine fest verdrahtete. Speicherung bleibt UTC.

**Risiko:** gering. Sichtbarste Korrektur für Endnutzer. Guter Einstiegs-PR, um beim
Maintainer Vertrauen aufzubauen, bevor die großen kommen.

## PR 4 — Diagnose: `get_index_status()` und `refresh_index()`

**Branch:** `feat/index-diagnostics` · **Basis:** PR 2
**Dateien:** `server.py`, `cli.py`, `config.py`, `index/manager.py`
**Quelle:** `7463cee`, `9fce22e`, `f3946fa`, `3b09fbc`, `b8e2d07`, `faedf7c`, `823690d`

Der Anlass ist konkret: **stderr eines MCP-Servers erreicht niemanden.** Läuft der
Server unter einem Desktop-Client, ist er eine Blackbox — der Nutzer sieht „geht nicht"
und der Agent hat keinen Kanal.

- `get_index_status()` — Zustand (`building`/`ready`/`empty`/`absent`), Build-Phase,
  Fortschritt, Sekunden seit letztem Fortschritt, Stall-Erkennung, laufender Sync,
  Erreichbarkeit von `~/Library/Mail` (= Full Disk Access), DLQ-Zahlen, Ereignis-Ring
  (letzte 50), Pfad zum Logfile, plus handlungsfähige `next_steps`.
- `refresh_index(full=False)` — Sync auf Zuruf, `full=True` baut im Hintergrund neu.
  Meldet ehrlich `already_running` / `failed` statt blind „started" (via `on_started`-
  Callback mit begrenztem Warten).
- Datei-Logging mit `0600` auch nach Rotation (`_OwnerOnlyRotatingFileHandler`).
- Der Docstring von `refresh_index` beansprucht bewusst „rebuild / re-index / neu
  aufbauen" **und** stellt klar, dass es *nicht* Apples Envelope Index ist — sonst
  schickt das Modell den Nutzer zu „Postfach › Neu aufbauen" in Mail.app. Das ist uns
  live passiert.

**Vor dem PR zu entfernen:** `install_mode`, `source_ref`, `SERVER_REVISION`
(Bundle-Launcher-Erkennung). Entweder streichen oder als neutrales
„wie wurde ich gestartet" verallgemeinern — **Entscheidung offen**.

**Risiko:** gering im Code, aber **API-Erweiterung**: 8 → 10 Tools. Der Maintainer
muss die Tool-Oberfläche wollen. Kandidat für ein vorheriges Issue.

## PR 5 — Schreib-Tools: `set_flag` (mit Farben) und `set_read_status`

**Branch:** `feat/write-tools` · **Basis:** PR 4
**Dateien:** `builders.py`, `server.py`
**Quelle:** `5e3b38f`, `4fd819f`, `75861ac`, Teile von `b8d8857`

**Aufhänger für den Maintainer:** `_ensure_writable()` und `APPLE_MAIL_READ_ONLY`
existieren upstream bereits (#80) — bisher bewachen sie nichts. Diese PR liefert das
Erste, was sie zu bewachen haben.

- `set_flag(ids, color?)` mit allen sieben Apple-Farben (`msg.flagIndex`: rot 0 …
  grau 6), `"none"` entmarkiert, `"default"` markiert ohne Farbe.
- `set_read_status(ids, read?)`.
- Einzeln oder als Batch (max. 500); Rückgabe in Eimern
  `{updated, unchanged, not_found, skipped_hidden}` — **ein Batch scheitert nie als
  Ganzes**, jede ID landet in genau einem Eimer.
- `applyToMessage()` verifiziert `msg.id() === targetId` **vor** dem Schreiben.
- No-Ops werden übersprungen (`needs_change_js`) — schon markierte Mails erzeugen
  keinen Schreibzugriff.
- Aufgelöste und gescannte Gruppen laufen in **getrennten** `osascript`-Aufrufen, damit
  ein langsamer Scan die schnellen, präzisen Writes nicht mitreißt.
- Ausgeschlossene Konten (#90): IDs, die dorthin auflösen, gehen nach
  `skipped_hidden` und **nie** an JXA.
- Regressionstest `TestWriteImplyingToolsHaveGuard` erzwingt das Read-only-Gate für
  jeden künftigen Tool-Namen mit `set_`/`flag_`/`mark_`-Präfix.

**Risiko:** hoch für den Maintainer — es sind die ersten mutierenden Tools des
Projekts. **Deshalb vorher ein Issue**, nicht direkt ein PR über 1500 Zeilen.

## PR 6 — Stabile Identität: die RFC822-Message-ID als Referenz

**Branch:** `feat/stable-message-identity` · **Basis:** PR 5
**Dateien:** `index/schema.py`, `index/manager.py`, `index/search.py`, `index/disk.py`,
`index/sync.py`, `index/watcher.py`, `builders.py`, `server.py`, `jxa/mail_core.js`
**Quelle:** `94ec9d0`, `13d694d`

Der inhaltlich stärkste Teil und der, der Upstream am meisten bringt — er repariert
eine Annahme, die im ganzen Projekt steckt: **eine Mail.app-ID ist eine ROWID pro
Mailbox.** Sie stirbt, sobald irgendein Gerät die Nachricht ablegt (Normalfall bei
Handy + Tablet am selben Konto), und die Nummer gehört danach womöglich einer anderen
Nachricht in derselben Mailbox.

- Schema v6: Spalte `rfc822_message_id` + Index, Migration v5→v6 als In-place-`ALTER`.
- Alle Lesewege geben den Header aus: `search()` (FTS und Anhänge), `get_emails()`
  (Envelope-Schnellpfad per gebündeltem `get_rfc822_ids()`, JXA-Pfade über `messageId`
  im Standard-Property-Set), `get_email()`.
- Alle Tools nehmen beide Formen an: `get_email`, `get_email_links`,
  `get_email_attachment`, `get_attachment`, `set_flag`, `set_read_status`.
- **Kein Header wird je in eine ROWID zurückübersetzt und dann geglaubt.** Beim
  Schreiben wählt der Index nur Konto und Scan-Reihenfolge; verglichen wird in JXA
  gegen `msg.messageId()`. Beim Lesen wird jeder Fundort geholt und der Header geprüft;
  bei Abweichung geht es zum nächsten, am Ende ein Fehler statt fremder Post.
- `MailCore.batchFetch` fällt pro Property weich (eine verweigerte Property wird mit
  `null` aufgefüllt), wirft aber weiterhin, wenn **gar nichts** lesbar war — eine
  unlesbare Mailbox darf nie als „0 Mails" durchgehen.
- Zeilen von vor v6 haben NULL; `get_index_status` meldet das als `without_stable_id`.

**Risiko:** mittel. Schema-Migration + Verhalten der Lese-Tools. Voll getestet.
Ohne PR 5 nur zur Hälfte begründbar — die Schreibseite ist der Grund, warum es zählt.

---

# Empfohlenes Vorgehen

1. **PR 3 zuerst** (klein, sichtbar, unstrittig) — dann PR 1, dann PR 2. Drei Bugfix-PRs
   ohne API-Änderung, die zeigen, dass wir das Projekt verstanden haben.
2. **Danach ein Issue** zu Schreib-Tools + Diagnose, mit Verweis auf das bereits
   vorhandene, aber leere `APPLE_MAIL_READ_ONLY`-Gate. Erst nach Rückmeldung des
   Maintainers PR 4 → 5 → 6.

# Offene Entscheidungen (brauchen Deine Ansage)

1. **Auto-Build des Index beim ersten Start** (`b8d8857`, `APPLE_MAIL_INDEX_AUTO_BUILD`,
   Default `true`) — meinungsstarke Verhaltensänderung. Eigener kleiner PR, mit
   Default `false` anbieten, oder ganz im Fork behalten?
2. **`install_mode` / `source_ref` / `SERVER_REVISION`** in `get_index_status` —
   streichen oder verallgemeinern?
3. **CLAUDE.md**: unsere Fassung ist stark gewachsen (+104 Zeilen). Anteilig pro PR
   mitliefern oder in einem Doku-PR am Ende?
4. **Deine stehende Regel „keine PRs/Issues in fremden Repos"** steht dem hier
   entgegen. Diese Vorbereitung folgt Deiner ausdrücklichen Anweisung von heute —
   bestätige bitte, bevor irgendetwas Richtung `imdinu` rausgeht.
