# Upstream PR bodies

One file per unit, ready to paste. **Fork-only** — `scripts/fork-only.py strip`
removes this directory, so it never travels to `imdinu/apple-mail-mcp`.

Each file carries:

- the **title** (one line, conventional-commit style, as upstream's history uses)
- the **body**: what, why, and the judgements a reviewer should push back on
- **Depends on**: the units that must land first, if any
- **Changelog**: prose the maintainer can paste into `CHANGELOG.md` under the
  release they choose
- **Verification**: the three checks upstream's CONTRIBUTING.md requires
- the exact `gh pr create` invocation — **documented, never run from here**

## Why the branches do not touch CHANGELOG.md

Upstream's changelog entries carry issue numbers and sit under a release
heading the maintainer owns. Twenty-two branches each editing the same
`[Unreleased]` block would conflict twenty-two ways and force a rebase after
every merge, so the prose ships in the PR body instead, where it costs the
maintainer one paste and no conflicts. `pyproject.toml` / `server.json` versions
are untouched for the same reason: the release number is the maintainer's call.

## Opening a PR (only after explicit approval)

Every branch is already pushed to `iret77/apple-mail-mcp`, so a PR is one
command per unit — from *any* checkout, since `--head` names the fork branch:

```bash
gho iret77 pr create \
  --repo imdinu/apple-mail-mcp \
  --base main \
  --head iret77:<branch> \
  --title "<title from the file>" \
  --body-file upstream-prs/<file>.md
```

`gho iret77` rather than bare `gh`: the ambient `$GITHUB_TOKEN` is the byte5ai
PAT and lacks the scope for this fork.

**Nothing in this directory has been sent anywhere.** No PR, no issue, no push
to upstream.

## Sequencing (from UPSTREAM_PR_PLAN.md)

1. **First wave:** A1, A3, A4 — three small fixes, reviewable in minutes.
2. **Second wave, after the first feedback:** A2, A5–A8, the `manager.py` stack,
   pointing at upstream's open #106.
3. **A9 and A10** fit either wave; A9 in particular makes the server usable at
   all on a non-English Mail.
4. **Track D** can follow at any point — none of it depends on B or C.
5. **Then an umbrella issue** carrying the unit table. Tracks B and C become PRs
   only after the maintainer says which they want.
