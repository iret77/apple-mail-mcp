**Title:** `feat: optional index auto-build on first serve (default off)`

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
2. **`[index] auto_build` / `APPLE_MAIL_INDEX_AUTO_BUILD`, default `false`.** When
   enabled, the first `serve` without an index kicks off `build_from_disk()` on a
   daemon thread, so the server answers immediately and the build reports its own
   outcome.

`_start_watcher()` moves out of the sync branch so a first build can hand over to
it too; otherwise `--watch` quietly does nothing on a fresh install.

### Worth pushing back on — this is the actual question

**The default is a product decision, not a bug fix, so it ships off.** With `true`
the server walks `~/Library/Mail` unasked on first start — minutes on a large
mailbox, and it needs Full Disk Access. Whether that is acceptable for your users
is your call, not mine; flipping it is one line, and I deliberately left it off
rather than choosing for you.

If you prefer `true`, the honest framing for the changelog is "first run builds
the index automatically", and the Full Disk Access requirement should probably
move up in the README.

### Changelog

```markdown
### Added

- **Optional index auto-build on first `serve`** — `[index] auto_build` /
  `APPLE_MAIL_INDEX_AUTO_BUILD`, default `false`. When enabled and no index
  exists, the first `serve` builds one on a background thread so a fresh install
  is usable without a separate `apple-mail-mcp index` run. Off by default because
  it walks `~/Library/Mail` unasked, which takes minutes on a large mailbox and
  requires Full Disk Access.

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
uv run pytest -q                # 498 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/optional-auto-build \
  --title "feat: optional index auto-build on first serve (default off)" \
  --body-file upstream-prs/B5-optional-auto-build.md
```
