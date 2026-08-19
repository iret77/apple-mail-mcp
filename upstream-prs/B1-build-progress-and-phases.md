**Title:** `feat: build phases, a heartbeat, and an error worth reading`

**Branch:** `iret77:feat/build-progress-and-phases` · **Depends on:** **A6** (and
through it A5) — same file, both contained in this PR

---

**No new tool.** This adds the internals that make an index build observable.

To be precise about what that means: this PR does **not** change the
`index://status` resource — it adds the state such a reader needs
(`is_building()`, `build_progress()`, `last_error`, `recent_events()`,
`has_usable_index()`). Wiring them into `index://status` is two lines and left
deliberately out of this diff, so the internals can be judged on their own; say
the word and I will add it here rather than in a follow-up.

The problem: a build runs on a daemon thread and reports nothing. From outside,
three different situations produce the same observation — zero emails indexed:
the build is warming up, the build is wedged, or the build died minutes ago. Under
a desktop client stderr reaches nobody, so there is no other channel to tell them
apart.

### What changed

- **`is_building()`** — set before the first statement that can fail and cleared
  in `finally`, so it cannot stick `True`. It was previously possible to report
  "building" for the rest of the process's life.
- **`build_progress()`** — a phase-aware heartbeat: `clearing`,
  `reading_metadata`, `indexing`, plus rows written, files seen and seconds since
  that last changed. **The phase is what makes zero interpretable**: a build spends
  its first minutes reading Apple's metadata before writing a single row, so
  `reading_metadata` gets a 600 s stall budget and `indexing` 120 s.
- **`last_error`** on *every* failure, not just the unreadable-mail-directory
  case. With only that one recorded, any other exception left a reader seeing "no
  errors" while the build was dead. Cleared by the next clean run.
- **A 50-entry event ring** (`record_event` / `recent_events`) for build and sync
  start/finish/failure. It mirrors to the logger and never raises: diagnostics must
  not break the operation they describe.
- **`has_usable_index()`** — an index *file* can exist while holding nothing,
  because an interrupted or permission-denied first build leaves an empty database
  behind. Syncing that forever never populates it.
- **`sync_updates()` records why it returned 0** — that number means both "no
  changes" and "could not read Mail".

`build_from_disk()` moves its setup inside the `try`, so the cleanup block has to
cope with a connection that was never opened — exactly the failure being
reported. Returning from `finally` would swallow the exception responsible for
it, so it is a conditional.

### Worth pushing back on

The stall budgets (600 s / 120 s) are calibrated on one large mailbox (~64k
messages, spinning disk cache cold). They are per-phase constants rather than
config; if you would rather they were tunable, that is one env var away.

### Changelog

```markdown
### Added

- **Index builds are observable.** A build runs on a daemon thread, so "warming
  up", "wedged" and "died minutes ago" all looked identical from outside — zero
  emails indexed, no output anywhere the user could reach. `IndexManager` now
  tracks a build phase (`clearing` → `reading_metadata` → `indexing`) with a
  heartbeat (rows written, files seen, seconds since progress, a per-phase stall
  budget), records `last_error` for *every* failure rather than one case, keeps a
  50-entry ring of lifecycle events, and distinguishes an index file that exists
  from one that holds messages (`has_usable_index()`).
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
uv run pytest -q                # 513 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/build-progress-and-phases \
  --title "feat: build phases, a heartbeat, and an error worth reading" \
  --body-file upstream-prs/B1-build-progress-and-phases.md
```
