**Title:** `feat: get_index_status() — a diagnosis, not a dump of numbers`

**Branch:** `iret77:feat/index-status-tool` · **Depends on:** **B1** and **B5**
(both contained in this PR, along with A5/A6)

**Tool count:** 8 → 9

---

Every index failure produces the *same* user-visible symptom — search finds
nothing — and the causes are indistinguishable from outside: no Full Disk Access,
an index that was never built, a build still warming up, a build wedged, an index
file with zero rows. Under a desktop client stderr goes nowhere, so the user
reports "it doesn't work" and neither they nor the assistant can get further.

### What changed

`get_index_status()` answers in two registers at once:

- **Machine-readable state** — `state` (`building` / `ready` / `empty` /
  `absent`), indexed and disk counts, `progress_percent`, `build_phase`,
  `build_appears_stalled`, `seconds_since_progress`, `mail_dir_accessible`,
  `last_error`, `recent_events`, plus the usual stats.
- **Instructions a person can follow** — `problem` / `note`, `user_message`, and
  an ordered `next_steps` written GUI-first, because most users have never opened
  a terminal. macOS grants Full Disk Access to the *responsible app* — whichever
  process launched the server — so the steps differ by setup, and
  `_index_guidance()` picks the right ones.

### Worth pushing back on

Two judgements in particular:

- **No Full Disk Access plus a working index is not a fault.** It is the
  deliberate manual setup (index built from a Terminal that has the permission),
  so it gets a `note`, not a `problem` — reporting it as broken sends the user
  chasing a permission they consciously withheld.
- **No fresh disk walk while a build runs.** It competes with the build for I/O
  exactly when a percentage is wanted, so the cached count from before the build
  is used as the denominator — and when there is none, `progress_percent` is
  omitted rather than invented.

Also: `assistant_instructions` is a field of prose aimed at the calling model. It
is unusual, and it is what makes the difference between the model dumping JSON at
the user and walking them through the fix. Happy to drop it if you find it too
opinionated.

**Why B5 is included:** the guidance has to know whether the server would build
the index itself on restart. Without that flag it reads out steps for a command
auto-build already runs.

### Changelog

```markdown
### Added

- **`get_index_status()` — index health with actionable next steps.** Every index
  problem looks the same from outside (search returns nothing) while the causes
  differ completely: no Full Disk Access, no index yet, a build warming up, a
  build wedged, or a database file with zero rows. The new tool reports machine-
  readable state (`state`, counts, `progress_percent`, `build_phase`,
  `build_appears_stalled`, `mail_dir_accessible`, `last_error`, `recent_events`)
  *and* an ordered, GUI-first `next_steps` list plus a plain-language
  `user_message` the assistant can relay. Running without Full Disk Access but
  with a working index is reported as a note rather than a problem, since that is
  a deliberate setup, and no fresh disk walk is performed while a build is running.
```


### Changelog and version

Deliberately not in the diff. Upstream's changelog entries carry issue numbers
under a release heading the maintainer owns, and twenty-two prepared branches
each editing the same `[Unreleased]` block would conflict twenty-two ways and
force a rebase after every merge. The prose is above, ready to paste; the
release number is yours to choose.

### Verification

```
uv run ruff check src/          # All checks passed!
uv run ruff format --check src/ # 16 files already formatted
uv run pytest -q                # 529 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/index-status-tool \
  --title "feat: get_index_status() — a diagnosis, not a dump of numbers" \
  --body-file upstream-prs/B2-index-status-tool.md
```
