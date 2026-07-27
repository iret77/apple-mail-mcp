# Upstream-PR-Plan — iret77/apple-mail-mcp → imdinu/apple-mail-mcp

Stand: 2026-07-27. Basis geprüft: `git merge-base upstream/main feat/write-ops-flag-read`
== `upstream/main` HEAD (`ee655d4`). **Upstream ist uns 0 Commits voraus**, wir sind 30
voraus (22 Dateien, +7025/−217). Kein Rebase nötig.

## Leitgedanke

Der Maintainer bekommt **kein Alles-oder-nichts-Paket**. Jede Einheit ist so
geschnitten, dass er sie einzeln mergen, in Ruhe reviewen, umbauen oder ablehnen kann,
ohne dass die anderen dadurch fallen. Wo eine Abhängigkeit technisch unvermeidbar ist,
steht sie unten ausdrücklich dabei — samt Zusage, dass wir bei Ablehnung eines Stücks
den Rest darauf umbauen.

## Vorgaben aus dem Upstream-CONTRIBUTING.md

- „Keep the diff focused — avoid unrelated changes in the same PR."
- „PRs are typically squash-merged into `main`." → unsere Commit-Historie landet nicht
  drüben; **es zählt der Diff pro PR**, nicht unsere Reihenfolge.
- Pflicht: `ruff check src/`, `ruff format --check src/`, `pytest`.
- Lizenz GPL-3.0.

## Was Upstream schon hat (geprüft, nicht vermutet)

- `_ensure_writable()` + `APPLE_MAIL_READ_ONLY` (#80) — **das Gate existiert, bewacht
  aber nichts.** Aufhänger für Track C.
- Die Resource `index://status` — dort können Zählwerte andocken, ohne ein neues Tool.
- 8 Tools.

## Was nicht upstream geht

| Sache | Wo | Grund |
|---|---|---|
| `mcpb/`, `scripts/build-mcpb.sh`, `dist/`, diese Datei | Fork-Branches | Fork-Distribution |
| `install_mode`, `source_ref`, `APPLE_MAIL_MCP_LAUNCHER`, `APPLE_MAIL_MCP_REF` | server.py:478–498, 2724 | beschreibt unseren Bundle-Launcher |
| `SERVER_REVISION` | server.py:418 | unser Build-Stempel |
| `.mcpb`-Absatz im README | README.md | Fork-spezifisch |

---

# Die Einheiten

`⊘` = kann ohne Auswirkung auf alle anderen abgelehnt werden.
`↑` = baut auf der genannten Einheit auf (textuell, nicht inhaltlich — bei Ablehnung
bauen wir um).

## Track A — Fehlerbehebungen, keine API-Änderung

Acht Stück, alle klein und für sich prüfbar. Keine erweitert die Tool-Oberfläche, alle
reparieren nachweislich kaputtes Verhalten.

| # | Branch | Diff | Abh. |
|---|---|---|---|
| A1 | `fix/emlx-header-decoding` | `disk.py`, `sync.py` | ⊘ |
| A2 | `fix/oversized-emails-visible` | `disk.py`, `config.py`, `schema.py`, `server.py` (nur Resource) | ⊘ |
| A3 | `fix/stale-emlx-flags` | `server.py`, `envelope_direct.py` | ⊘ |
| A4 | `fix/local-timestamps` | `server.py` | ⊘ |
| A5 | `fix/sync-transaction-rollback` | `manager.py` | ⊘ |
| A6 | `fix/per-thread-connections` | `manager.py` | ↑ A5 |
| A7 | `perf/rebuild-fts-delete-all` | `manager.py` | ↑ A6 |
| A8 | `fix/cross-process-write-lock` | `manager.py`, `watcher.py` | ↑ A6 |

**A1 — unlesbare Header.** Ein nicht-ASCII-`Subject`, `Received`, `Content-ID` oder
Anhangs-Dateiname lässt Pythons `email`-Modul ein `Header`-Objekt statt `str` liefern;
der erste `.strip()` wirft `AttributeError` und **der komplette Sync bricht ab** — bei
uns einen Tag lang unbemerkt, weil der Fehler nur auf stderr ging. `header_text()` /
`_filename_text()` garantieren dekodierten `str`; ein Guard-Test verhindert die Rückkehr
roher Headerzugriffe. Dazu: der Per-Message-Guard im Sync fängt `Exception` statt nur
drei Typen — eine kaputte Mail darf nie den ganzen Lauf killen.
*Der wichtigste Einzel-PR der Reihe.*

**A2 — übergroße Mails.** Verschwanden still. Jetzt als `too_large` in der DLQ, Grenze
über `APPLE_MAIL_INDEX_MAX_EMAIL_MB` (Default 25) konfigurierbar, Zählwerte über die
**bestehende** Resource `index://status` sichtbar — kein neues Tool.

**A3 — veralteter Gelesen-/Markiert-Status.** `get_email()` las beides aus dem
Plist-Footer der `.emlx`, den Mail nicht zuverlässig neu schreibt. Overlay aus Apples
Envelope Index (ein Read-only-Select).

**A4 — Zeitzone.** Alle Zeitstempel gingen als UTC raus, Mail.app zeigt Ortszeit (bei
uns 12:54 statt 14:54). `to_local_iso()` konvertiert an jeder Ausgabegrenze über die
**System**-Zone, DST-korrekt, nie über eine fest verdrahtete. Speicherung bleibt UTC.

**A5–A8 — vier Ursachen von „database is locked".** Nacheinander gefunden, deshalb
einzeln schneidbar; sie liegen aber alle in `manager.py` und stapeln daher textuell:

- **A5 Rollback** — eine abgebrochene Transaktion blieb offen und blockierte danach
  *jeden* Schreibzugriff.
- **A6 Verbindungen pro Thread** (`threading.local`) — ein Hintergrund-Rebuild legte
  sonst den Server lahm.
- **A7 FTS-Trigger** vor dem `DELETE` fallen lassen, einmaliges
  `INSERT INTO emails_fts(emails_fts) VALUES('delete-all')`, Trigger als **erste**
  Aktion im `finally` zurück. DDL committet implizit — ein Rollback holt sie nicht
  wieder, ein Fehler hätte den Index dauerhaft ohne Trigger hinterlassen.
- **A8 Cross-Prozess-Lock** (`fcntl.flock`). Ein `threading.Lock` reicht nicht: Claude
  Desktop startet den Server **zweimal** — das ist deren offenes Issue **#106**. Fällt
  auf Thread-Lock zurück, wenn die Lockdatei nicht anlegbar ist.

## Track B — Diagnose (additiv, Tool-Oberfläche wächst)

Anlass: **stderr eines MCP-Servers erreicht niemanden.** Unter einem Desktop-Client ist
der Server eine Blackbox — der Nutzer sieht „geht nicht", der Agent hat keinen Kanal.

| # | Branch | Diff | Abh. |
|---|---|---|---|
| B1 | `feat/build-progress-and-phases` | `manager.py` | ↑ A6 |
| B2 | `feat/index-status-tool` | `server.py`, `manager.py` | ↑ B1 |
| B3 | `feat/refresh-index-tool` | `server.py` | ⊘ |
| B4 | `feat/server-log-file` | `cli.py`, `config.py` | ⊘ |
| B5 | `feat/optional-auto-build` | `cli.py`, `config.py` | ⊘ |

**B1** liefert das Innenleben ohne neue Tools: Build-Phase, Fortschritt, Zeit seit
letztem Fortschritt, Stall-Erkennung, Ereignis-Ring (letzte 50). Nützt schon der
bestehenden Resource `index://status`. Wer B2 nicht will, kann B1 trotzdem nehmen.

**B2** `get_index_status()` — Zustand, Fortschritt, Erreichbarkeit von
`~/Library/Mail` (= Full Disk Access), DLQ-Zahlen, handlungsfähige `next_steps`.
*Vor dem PR zu entfernen: `install_mode`, `source_ref`, `SERVER_REVISION`.*

**B3** `refresh_index(full=False)` — Sync auf Zuruf, `full=True` baut im Hintergrund
neu; meldet ehrlich `already_running`/`failed` statt blind „started". Der Docstring
beansprucht bewusst „rebuild / neu aufbauen" **und** stellt klar, dass es nicht Apples
Envelope Index ist — sonst schickt das Modell den Nutzer zu „Postfach › Neu aufbauen"
in Mail.app. Live passiert.

**B4** Datei-Logging mit `0600` auch nach Rotation.

**B5 — Auto-Build, ausdrücklich als Opt-in mit Default `false`.** Baut den Index beim
ersten Start im Hintergrund, wenn keiner existiert. Wir schlagen den konservativen
Default vor, weil das eine Produktentscheidung des Maintainers ist und keine
Fehlerbehebung: der Server liefe sonst beim ersten Start ungefragt über
`~/Library/Mail`. Unser Bundle setzt die Variable ohnehin explizit, der Code-Default
kostet uns also nichts. Ein Satz gehört in den PR: *„Wenn du den Default umdrehen
willst, ist das eine Zeile — wir haben ihn bewusst konservativ gelassen."*

## Track C — Schreiben und stabile Identität

| # | Branch | Diff | Abh. |
|---|---|---|---|
| C1 | `feat/stable-identity-schema` | `schema.py`, `manager.py`, `sync.py`, `disk.py`, `watcher.py` | ⊘ |
| C2 | `feat/expose-message-id-in-reads` | `search.py`, `server.py`, `builders.py`, `mail_core.js` | ↑ C1 |
| C3 | `feat/write-tools` | `builders.py`, `server.py` | ⊘ |
| C4 | `feat/message-id-as-write-reference` | `server.py`, `builders.py` | ↑ C2, C3 |

**C1** Schema v6: Spalte `rfc822_message_id` + Index, Migration v5→v6 als
In-place-`ALTER`. Reine Datenhaltung, ändert kein Verhalten — schafft nur die
Voraussetzung.

**C2** Alle Lesewege geben den Header aus: `search()` (FTS und Anhänge), `get_emails()`
(Envelope-Schnellpfad per gebündeltem `get_rfc822_ids()`, JXA-Pfade über `messageId` im
Standard-Property-Set), `get_email()`. **Nützt für sich allein**, auch ohne jedes
Schreib-Tool: ein Client, der eine Mail über einen Aufruf hinaus festhalten will, hat
bisher nur eine ROWID, die beim nächsten Verschieben tot ist. Enthält die Härtung von
`MailCore.batchFetch` — eine verweigerte Property wird mit `null` aufgefüllt statt die
ganze Auflistung mitzureißen; wirft weiterhin, wenn **gar nichts** lesbar war, denn
eine unlesbare Mailbox darf nie als „0 Mails" durchgehen.

**C3** `set_flag(ids, color?)` mit allen sieben Apple-Farben (`msg.flagIndex`: rot 0 …
grau 6) und `set_read_status(ids, read?)`. Einzeln oder Batch (max. 500), Rückgabe in
Eimern `{updated, unchanged, not_found, skipped_hidden}` — **ein Batch scheitert nie als
Ganzes**. `applyToMessage()` verifiziert `msg.id() === targetId` vor dem Schreiben;
No-Ops werden übersprungen; aufgelöste und gescannte Gruppen laufen in getrennten
`osascript`-Aufrufen; ausgeschlossene Konten (#90) gehen nie an JXA. Ein
Regressionstest erzwingt das Read-only-Gate für jeden künftigen
`set_`/`flag_`/`mark_`-Tool-Namen.
*Aufhänger: euer `APPLE_MAIL_READ_ONLY` aus #80 bewacht damit endlich etwas.*

**C4** Die Tools nehmen den Header als Referenz an. **Kein Header wird je in eine ROWID
zurückübersetzt und dann geglaubt** — beim Schreiben wählt der Index nur Konto und
Scan-Reihenfolge, verglichen wird in JXA gegen `msg.messageId()`; beim Lesen wird jeder
Fundort geholt und der Header geprüft, bei Abweichung der nächste versucht, am Ende ein
Fehler statt fremder Post.

---

# Vorgehen

Nicht alle 17 auf einmal aufmachen — das ist für einen einzelnen Maintainer eine Lawine
und erreicht das Gegenteil von „detailliert entscheiden können".

1. **Erste Welle: A1, A3, A4.** Drei kleine, risikoarme Fehlerbehebungen, in Minuten
   prüfbar. Zeigt, dass wir das Projekt verstanden haben und sauber arbeiten.
2. **Zweite Welle nach erstem Feedback: A2, A5–A8.** Der `manager.py`-Stapel, mit
   Hinweis auf ihr offenes #106.
3. **Dann ein Sammel-Issue** („Fork von X: was wir gebaut haben und in welchen Stücken
   wir es anbieten") mit der Tabelle aus diesem Dokument. Der Maintainer sagt, was er
   sehen will — PRs aus Track B und C entstehen erst danach.

# Vorarbeiten vor dem ersten Diff

1. **Tests umverteilen.** Alles Neue liegt in `tests/test_write_ops.py` (3059 Zeilen),
   benannt nach unserem Branch. Upstream erwartet Tests bei ihrem Code: `test_disk.py`,
   `test_manager.py`, `test_server.py`, `test_sync.py`, `test_watcher.py`,
   `test_config.py`.
2. **Branches vom Endzustand neu schneiden, nicht cherry-picken.** Unsere Historie
   verschränkt Themen (mehrere „fix: N defects found by review" korrigieren jeweils
   Früheres). Jeder Branch entsteht von `upstream/main` aus mit dem Endzustand der
   betroffenen Stellen.
3. **Fork-Spezifika ausbauen** (Tabelle oben).
4. **CLAUDE.md anteilig aufteilen** — unsere Fassung ist um 104 Zeilen gewachsen; jeder
   PR bringt seinen Doku-Anteil mit, statt am Ende einen Doku-Klotz.

# Freigabe

Nichts geht Richtung `imdinu`, bevor Christian es ausdrücklich freigibt — weder PR noch
Issue noch Push.
