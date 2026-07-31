**Title:** `feat: get_emails(account="all") lists across every visible account`

**Branch:** `iret77:feat/cross-account-listing` · **Depends on:** nothing (⊘)

---

"What's unread?" is one question, and answering it cost one call per account —
after first discovering the account names. Most setups here have six.

The Envelope Index query already means "every account" when given no UUID, so the
capability was already there; only this tool's defaulting stood in the way. Two
defaults, in fact:

- the **account** default, which scoped the query to the first account, and
- the **`INBOX` mailbox** default, which under `"all"` would keep only those
  accounts that happen to have a mailbox by that name — on a localized Mail, none
  of them. So `"all"` without an explicit `mailbox` drops that default too; an
  explicit `mailbox` still applies.

### What changed

`account="all"` (or `"*"`) leaves the account scope open on the Envelope Index fast
path. Exclusions are unaffected: they filter on **UUIDs** further down, not on the
account name, so `"all"` cannot become a hole in the excluded-account boundary
(#90) — there is a test for exactly that.

**The JXA fallback refuses rather than answering narrowly.** It walks one account
at a time, so silently falling through would answer a different question than the
one that was asked, and the caller would have no way to notice.

### Worth pushing back on

- The result is a flat list ordered by date across accounts, with no per-account
  grouping. Each row carries `account`, so grouping is the caller's job; a
  `group_by` parameter felt like scope creep.
- `"all"` and `"*"` become reserved words, and the match is
  case-insensitive after trimming — so an account named "All" or " all "
  cannot be targeted by name either. Documented rather than worked around; a
  separate `scope` parameter would avoid the collision entirely if you prefer
  that shape.
- `"all"` requires Full Disk Access (the Envelope Index). Users without it get an
  error naming the reason rather than a partial answer — deliberate, but it does
  mean the parameter is unavailable in exactly the setup that most needs the
  convenience.

### Changelog

```markdown
### Added

- **`get_emails(account="all")` lists across every visible account in one call.**
  "Every visible account" means the accounts Mail currently has, minus the
  hidden ones: Apple's Envelope Index keeps rows for accounts that have since
  been removed, and an unscoped query handed those back under their bare UUID.
  Answering "what's unread?" previously took one call per account, after first
  discovering the account names. The Envelope Index query already meant "every
  account" when given no UUID; only the tool's own defaulting stood in the way —
  including the `INBOX` mailbox default, which under `"all"` would have kept only
  the accounts that happen to have a mailbox by that name (on a localized Mail,
  none). Excluded accounts (#90) are still filtered, by UUID. The JXA fallback
  refuses the request rather than silently answering for a single account.
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
uv run pytest -q                # 500 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:feat/cross-account-listing \
  --title 'feat: get_emails(account="all") lists across every visible account' \
  --body-file upstream-prs/D2-cross-account-listing.md
```
