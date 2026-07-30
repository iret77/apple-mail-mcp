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

## Upstream issues these touch

Verified against the tracker rather than assumed:

| Issue | State | Unit |
|---|---|---|
| #80 — `APPLE_MAIL_READ_ONLY` is set up but never enforced | closed | **C3** gives the gate something to guard |
| #90 — account-level index exclusion | closed | **C3**, **D2** keep the boundary intact |
| #106 — two `--watch` instances cause a write storm | **open** | **A8** removes the concurrent-writer failure, but **not** the watcher path — the PR body says so explicitly rather than claiming the issue |

## Sequencing (from UPSTREAM_PR_PLAN.md)

1. **First wave:** A1, A3, A4 — three small fixes, reviewable in minutes.
2. **Second wave, after the first feedback:** A2, A5–A8, the `manager.py` stack,
   pointing at upstream's open #106.
3. **A9 and A10** fit either wave; A9 in particular makes the server usable at
   all on a non-English Mail.
4. **Track D** can follow at any point — none of it depends on B or C.
5. **Then an umbrella issue** carrying the unit table. Tracks B and C become PRs
   only after the maintainer says which they want.

## Review round (Codex, adversarial)

Before any PR is opened, every unit was reviewed against explicit acceptance
criteria — written first, so expectations could not be derived from the thing
being judged. Each unit got its own `codex exec` run with the diff, the PR body
and the criteria; all 22 came back `FIX-FIRST`.

Every finding was then verified at the code level rather than taken on trust.
The ones that survived are fixed on the branch that introduced them, each with a
test that fails without the fix — several proved by reverting the fix and
watching the test go red.

**What the review caught that the mechanical gate could not:**

| Class | Examples |
|---|---|
| Regression against upstream | A1 stopped decoding RFC 2047 subjects (`=?UTF-8?B?…?=` indexed literally) |
| Hard runtime error | C2: `search(scope="attachments")` raised `KeyError` — the test mocked the very dict whose shape was wrong |
| Silent data loss | D3: three messages in one second, `limit=2` → the third unreachable forever |
| Persistent corruption | A7: `DROP TRIGGER` outside the `try` — one failure and the database file keeps a schema with no FTS triggers, surviving restarts |
| Wrong-target write | C3: an id in two mailboxes resolved to an arbitrary one; A9: `Projects/INBOX` answered a request for the inbox on a German account |
| Self-inflicted race | A6: twelve threads on a fresh database → `vtable constructor failed`, `duplicate column name` |
| Overclaiming | A7 and A10 promised changes their diff did not contain; B1 claimed `index://status` served data it never wired up |
| Stale documentation | Six shipped files still advertised eight tools; three still documented schema v5 |

Test theatre was a recurring theme: several tests passed with the fix reverted.
Those were rewritten to exercise the tool rather than the helper, or the real
function rather than a mock of it.

### Second round

Every branch was reviewed again after the first round's fixes, with the
round-1 findings attached as a checklist to verify — `FIXED` / `NOT FIXED` /
`PARTIAL` per item, against the diff rather than the commit message — plus two
standing questions: *did the fixes introduce anything*, and *what did both
rounds miss*.

**Two of my own fixes were wrong, and the round caught them:**

- **A9 was not fixed at all.** Moving the role table ahead of the generic match
  did nothing, because `mailboxRole()` normalizes the hierarchy away before
  comparing — `Projects/INBOX` still answered a request for the inbox. My test
  passed only because it listed the real inbox first. A role request now
  requires a *top-level* mailbox, and the tests assert both listing orders.
- **A6's interrupt test was a stub.** Resolving a rebase conflict had moved its
  body into a neighbouring test, leaving the named one with nothing but imports
  — it passed unconditionally. Both are whole again, and the interrupt test
  fails when `BaseException` is narrowed back.

**And several defects that neither the mechanical gate nor round 1 found:**

| Unit | Defect |
|---|---|
| A2 | A build that skips every file never committed its DLQ row — it survived only as a side effect of a later `executescript()` |
| A2 | A mailbox at its cap swallowed oversized files: the cap check ran before the size check |
| A8 | Any early failure leaked the write lock, wedging every later build and sync with "already running" |
| B1/B4 | The rollback-on-interrupt correction reached one branch as documentation only — the code still caught plain `Exception` |
| B2 | `get_index_status()` **created** the index database, so the next `serve` took the sync path instead of building |
| B4 | `index`/`rebuild` installed the log handler and then reported failures to stderr only — the log stayed empty |
| C3 | An unreadable mailbox during a scan was still reported as a missing message |
| C4 | Stale index rows ended the search instead of falling back to a live one; `["<a@b>", "a@b"]` was written twice |
| D2 | Excluded accounts were filtered *after* SQL `LIMIT`, so hidden mail consumed the page and visible mail was skipped |
| D3 | `before_id` without `before` was ignored, returning the newest page — an endless loop for a backwards walk |

Findings judged invalid are recorded as such rather than acted on: the
80-column complaints about Markdown (upstream's own docs exceed it; the rule is
ruff's, for Python), and "unfocused diff" on stacked units (that is the stack —
GitHub shows only the top unit's changes once the parent merges).
