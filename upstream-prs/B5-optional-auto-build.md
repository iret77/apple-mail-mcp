**Title:** `feat: optional index auto-build on first serve`

**Branch:** `iret77:feat/optional-auto-build` · **Depends on:** nothing (⊘)

---

Today `serve` syncs when an index exists and does nothing at all when one does
not. A fresh install therefore starts, says nothing, and body search comes back
empty — with no hint that the index was never built.

### What changed

Two things, deliberately separate:

1. **The silent case gets a voice.** With no index and no auto-build, startup now
   says so and names the command that fixes it. This part costs nothing and is
   unconditional.
2. **`[index] auto_build` / `APPLE_MAIL_INDEX_AUTO_BUILD`** (the default is a
   product call, covered below). When enabled, the first `serve` without an index
   kicks off `build_from_disk()` on a daemon thread, so the server answers
   immediately and the build reports its own outcome.

`_start_watcher()` moves out of the sync branch so a first build can hand over to
it too; otherwise `--watch` quietly does nothing on a fresh install.

### Worth pushing back on — this is the actual question

**The default is a product decision, not a bug fix, and it depends on how the
server reaches the user.** For a plain PyPI/CLI install we suggest `false`: with
`true` the server walks `~/Library/Mail` unasked on first start, which is minutes
on a large mailbox and needs Full Disk Access. But if you adopt the one-click
`.mcpb` installer (the E1 unit in our umbrella issue), `true` is the right
default: someone who double-clicks an installer expects search to work, not to
run `apple-mail-mcp index` by hand. Our fork ships `true` for exactly that
reason, paired with the `.mcpb`. Flipping it is one line either way.

If you prefer `true` regardless of packaging, the honest changelog framing is
"first run builds the index automatically", and the Full Disk Access requirement
should probably move up in the README.

### Changelog

```markdown
### Added

- **Optional index auto-build on first `serve`.** `[index] auto_build` /
  `APPLE_MAIL_INDEX_AUTO_BUILD`. When enabled and no index exists, the first
  `serve` builds one on a background thread so a fresh install is usable without a
  separate `apple-mail-mcp index` run. The default is a product decision: off for
  a plain install (it walks `~/Library/Mail` unasked, minutes on a large mailbox,
  and requires Full Disk Access), on when shipped via the one-click `.mcpb`
  installer.

### Fixed

- **A `serve` with no index no longer starts silently.** Body search returned
  nothing with no indication that the index had never been built; startup now says
  so and names the command that builds it. `--watch` also works on a fresh
  install, where the watcher previously never started because only the sync path
  handed over to it.
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
uv run pytest -q                # 499 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/optional-auto-build \
  --title "feat: optional index auto-build on first serve" \
  --body-file upstream-prs/B5-optional-auto-build.md
```
