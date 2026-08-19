**Title:** `fix: resolve well-known mailboxes by role, not by name`

**Branch:** `iret77:fix/mailbox-roles-not-names` · **Depends on:** nothing (⊘)

---

**On a non-English Mail, the default mailbox does not exist.** `INBOX` is a
string, and a German install has `Posteingang`; older macOS wrote `Eingang`;
Exchange uses `Deleted Items`; Gmail nests everything under `[Gmail]/`;
dovecot-style servers prefix with `INBOX.`. `get_emails()` with no arguments
therefore fails outright for a large share of users — the very first call they
make.

### What changed

`MailCore.getMailbox()` resolves in order, most reliable first:

1. **Exact name** — cheap, and right when it hits.
2. **Mail's own notion of the role** — `specialMailbox()` probes `sentMailbox` /
   `draftsMailbox` / `trashMailbox` / `junkMailbox` on the account and then the
   application. Language- and provider-independent where the property exists; the
   probe never throws, it returns null and the chain continues.
3. **The role table** — localized and legacy names for de, fr, es, it, pt-BR, nl,
   sv, da, fi, no, pl, ru, tr, ja, ko, zh-Hans, zh-Hant.
4. **Normalized match** — lowercase, drop provider hierarchy (`[Gmail]/…`, a
   leading `INBOX.`), compare the last path segment. This is what makes
   `INBOX.Sent` answer a request for `Sent`.

   Stages 3 and 4 are in that order on purpose, and both refuse a **nested**
   mailbox when a role was requested. Normalization drops hierarchy, so a user's
   own `Projects/INBOX` reduces to "inbox" — without the top-level test it
   answers a request for the real inbox whenever it is listed first, which is
   also the kind of defect a test can hide by fixing the listing order.
   `[Gmail]/Sent Mail` and `INBOX.Sent` still resolve: a provider prefix is
   hierarchy the normalizer is entitled to ignore, a user folder is not.
5. **Fail loudly** — the error names the role and lists the mailboxes that
   actually exist.

`isDiscardMailbox()` (trash or junk) rides on the same logic, so any "never touch
a discard mailbox" rule holds on a localized or Exchange account without a word
list of its own.

Also adds `docs/troubleshooting.md` → "Mailbox Not Found on a Localized Mail", and
a test asserting that every method the Python side calls is actually present on
`MailCore` after evaluation — a JS object literal absorbs an unterminated comment
in silence, and the resulting `not a function` would only appear on macOS at call
time.

### Worth pushing back on

**Every entry in the role table is sourced from Apple's localized Mail user guide
or is a documented provider/legacy name.** Nothing there is a translation I made
up, and that rule matters: a wrong name can match somebody's real folder, which
is worse than a missing one. If you would rather ship a smaller table (say the
five languages you can verify), stages 1–3 already carry most of the benefit.

The tests shell out to `node` to evaluate the JXA layer without macOS. They skip
when `node` is absent; `macos-latest` runners have it.

### Changelog

```markdown
### Fixed

- **Well-known mailboxes resolve by role, not by name.** A mailbox name changes
  with the system language (`Posteingang`), the macOS version (`Eingang`) and the
  provider (`Deleted Items`, `[Gmail]/Sent Mail`, `INBOX.Trash`), so the `INBOX`
  default failed outright on a localized Mail — the first call a user makes.
  Resolution now tries the exact name, then Mail's own `sentMailbox` /
  `draftsMailbox` / `trashMailbox` / `junkMailbox` properties, then a normalized
  match that strips provider hierarchy, then a table of localized names taken from
  Apple's localized Mail user guide, and finally fails with the mailboxes that do
  exist. Trash/junk detection uses the same role logic.
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
uv run pytest -q                # 524 passed
```

### Open

```bash
gho iret77 pr create --repo imdinu/apple-mail-mcp --base main \
  --head iret77:fix/mailbox-roles-not-names \
  --title "fix: resolve well-known mailboxes by role, not by name" \
  --body-file upstream-prs/A9-mailbox-roles-not-names.md
```
