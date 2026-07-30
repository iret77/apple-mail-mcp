**Title:** `fix: an incomplete search is reported as a missing message`

**Branch:** `iret77:fix/incomplete-search-is-not-absence` · **Depends on:** nothing (⊘)

---

Several code paths answer "not found" for situations that establish nothing of
the kind: a scan that hit its mailbox cap, a mailbox Mail refused to read, an
account that could not be enumerated at all, a recorded `.emlx` path that no
longer exists. All of them produce the *identical* empty answer, so a stale index
row, a mistyped reference and a genuinely deleted message are indistinguishable
from outside — and each one costs a full debugging round.

### What changed

- `GetEmailBuilder` counts what it left out and marks the error `INCOMPLETE:` with
  the number of mailboxes not searched. When the account itself could not be
  enumerated, it says so instead of reporting a missing message — nothing was
  searched at all.
- Strategy 0: a missing `.emlx` file means the recorded **path** is stale, nothing
  more. The row is cleaned and the cascade **continues** into the live strategies,
  instead of raising "deleted or moved" on the unverified assumption that they
  would fail too. (A test had frozen that assumption in; it is replaced.)
- The "not found" messages distinguish "Mail was reachable and it was not there"
  from "the search did not cover everything".

### Worth pushing back on

A numeric id still cannot be searched across accounts — it is a per-mailbox
ROWID, so the same number is a different message elsewhere, and widening the
search would risk returning a stranger's mail. This PR states that limit in the
message rather than removing it. (Track C's stable-identity work is what actually
removes it, by making the RFC822 header a first-class reference.)

### Changelog

```markdown
### Fixed

- **An incomplete search is no longer reported as a missing message.** A scan that
  hit its mailbox cap, a mailbox Mail refused to read, an account that could not
  be enumerated, and a genuinely deleted message all produced the same "not
  found" — so a stale index row and a real deletion were indistinguishable. Scans
  now count what they left out and say so (`INCOMPLETE: N mailbox(es) not
  searched`), an account that cannot be enumerated reports that rather than an
  absence, and a missing `.emlx` file cleans the stale row and continues into the
  live strategies instead of concluding the message is gone.
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
  --head iret77:fix/incomplete-search-is-not-absence \
  --title "fix: an incomplete search is reported as a missing message" \
  --body-file upstream-prs/A10-incomplete-search-is-not-absence.md
```
