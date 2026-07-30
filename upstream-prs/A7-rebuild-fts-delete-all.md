**Title:** `perf: empty the FTS index in one statement, not one delete per row`

**Branch:** `iret77:perf/rebuild-fts-delete-all` · **Depends on:** **A6** (and
through it A5) — same file, both contained in this PR

---

`build_from_disk()` cleared the tables **before** dropping the FTS triggers. With
`emails_ad` still attached, `DELETE FROM emails` fires one FTS5 delete per row —
on a 60k index that is minutes of work during which nothing is written, and it is
entirely wasted, because the FTS content is rebuilt from scratch immediately
afterwards.

### What changed

- Drop the triggers **first**, then clear.
- Empty the FTS index with a single `INSERT INTO emails_fts(emails_fts)
  VALUES('delete-all')` instead of the per-row deletes the trigger would have
  done.
- The triggers are recreated in `finally` (extracted as
  `_recreate_fts_triggers()`), and **before** anything else in that block:
  `DROP TRIGGER` is DDL and autocommits, so a rollback does not bring the
  triggers back. If a build ends without recreating them, they are gone from the
  database *file* — surviving restarts — and every later insert lands in `emails`
  but never in `emails_fts`, so body search silently stops seeing new mail.

### Worth pushing back on

`'delete-all'` is an FTS5 external-content command; it is the documented way to
clear such an index, but it does assume the table stays external-content. The
per-row path would still be correct, just slow.

### Changelog

```markdown
### Performance

- **A rebuild no longer spends minutes deleting FTS rows it is about to
  recreate.** The tables were cleared while the FTS triggers were still attached,
  so `DELETE FROM emails` fired one FTS5 delete per row — minutes of wasted work
  on a large index, with nothing written meanwhile. The triggers are now dropped
  first and the FTS index is emptied with a single `'delete-all'`. The triggers
  are recreated as the first step of the cleanup block, because `DROP TRIGGER`
  autocommits: a build that ended without recreating them left them missing from
  the database file, and every later insert silently skipped the search index.
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
uv run pytest -q                # 501 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:perf/rebuild-fts-delete-all \
  --title "perf: empty the FTS index in one statement, not one delete per row" \
  --body-file upstream-prs/A7-rebuild-fts-delete-all.md
```
