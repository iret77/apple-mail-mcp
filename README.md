# Apple Mail MCP

<!-- mcp-name: io.github.imdinu/apple-mail-mcp -->

<p align="center">
  <img src="docs/assets/social-card.svg" alt="Apple Mail MCP — Full-coverage FTS5 body search" width="720">
</p>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](https://www.apple.com/macos/)
[![MCP](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![CI](https://github.com/imdinu/apple-mail-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/imdinu/apple-mail-mcp/actions/workflows/lint.yml)

The only Apple Mail MCP server with **full-coverage body search** — reliable on large mailboxes where AppleScript-based servers timeout. 8 tools for reading, searching, and extracting email content.

**[Read the docs](https://imdinu.github.io/apple-mail-mcp/)** for the full guide.

## Quick Start

### Claude Desktop — the easy way

No terminal, no Python, no config file.

1. **[⬇ Download the bundle](https://github.com/iret77/apple-mail-mcp/releases/latest/download/apple-mail-mcp.mcpb)**
   — one link, always the current build. (Every release also carries a
   version-stamped copy, `apple-mail-mcp-<version>.mcpb`, if you want to
   know what you downloaded without opening it. All releases:
   [Releases](https://github.com/iret77/apple-mail-mcp/releases).)
2. **Double-click it.** Claude Desktop installs it as an extension.
3. **Grant Full Disk Access to Claude Desktop** — see the box below.
   This step is not optional and it is the one people miss.
4. **Quit Claude Desktop completely and start it again.** A permission
   granted while the app is running does not reach the already-running
   process.

That is the whole installation. The bundle is a thin launcher: it runs
the Python server via `uvx` and fetches the code from this repository,
so nothing is bundled and updates arrive with the next bundle you
install.

> ### ⚠️ Full Disk Access — read this, it is the usual reason nothing works
>
> Reading your mail means reading `~/Library/Mail`, which macOS
> protects. macOS grants that permission to **the application that
> launches the server** — not to this package, and not to your
> terminal.
>
> **With the bundle, that application is Claude Desktop.**
>
> System Settings → Privacy & Security → **Full Disk Access** → switch
> on **Claude**. Then quit Claude Desktop and start it again.
>
> Granting it to Terminal does nothing for the bundle: the terminal
> never launches this server.
>
> **Don't want to give Claude Desktop full disk access?** Then build the
> index yourself, from a terminal that has the permission, and turn the
> automatic build off:
>
> ```bash
> pipx install apple-mail-mcp && apple-mail-mcp index
> ```
>
> Set *Build the search index automatically* to **off** in the
> extension's settings. The server then reads the finished index at
> `~/.apple-mail-mcp/index.db`, which is not a protected path. Search,
> flagging and read/unread keep working; only live disk reads fall back
> to a slower route.

### What happens on the first start

The search index is built in the background. On a large mailbox this
takes minutes and shows nothing at all while it runs — that is normal,
not a hang, and the server answers the whole time.

**If anything looks wrong, do not go digging. Ask your assistant:**

> Ask the Apple Mail integration for its index status and show me the
> result.

It reports which build is answering, what the index is doing, how far
along it is, and whether `~/Library/Mail` is readable — in plain
language. That answer is also what makes a bug report useful.

### Other MCP clients

```bash
pipx install apple-mail-mcp
```

```json
{
  "mcpServers": {
    "mail": {
      "command": "apple-mail-mcp"
    }
  }
}
```

Here the client you configure is the application that launches the
server, so **it** is the one that needs Full Disk Access. Building the
index yourself from a terminal works the same way as above.

Building the bundle from source: `./scripts/build-mcpb.sh`, details in
[`mcpb/README.md`](mcpb/README.md).

### Who needs Full Disk Access — quick reference

| Index built by | Permission goes to |
|---|---|
| The server, automatically | **the app that starts it** — Claude Desktop with the bundle, otherwise your MCP client |
| You, via `apple-mail-mcp index` | **the terminal app** you run it in |

The rule behind both rows: macOS grants the permission to the process
that launches the server, never to the package itself.

### Building the index by hand

Only needed if the launching app has no Full Disk Access, or if you
prefer to keep it that way. With the bundle and the permission granted,
the server does this itself on first start.

```bash
# The TERMINAL needs Full Disk Access for this route:
# System Settings → Privacy & Security → Full Disk Access → add Terminal

apple-mail-mcp index --verbose
```

### Configure (Optional)

```bash
apple-mail-mcp init   # writes ~/.apple-mail-mcp/config.toml
```

Writes a commented config file you can edit to set defaults like your
primary account or mailbox. Every key has a matching `APPLE_MAIL_*` env
var if you prefer environment-based config. See
[Configuration](https://imdinu.github.io/apple-mail-mcp/configuration/)
for the full schema and precedence rules.

## Tools

| Tool | Purpose |
|------|---------|
| `list_accounts()` | List email accounts |
| `list_mailboxes(account?)` | List mailboxes |
| `get_emails(filter?, account?, limit?)` | Get emails — all, unread, flagged, today, last_7_days. `account="all"` lists across **every** visible account in one call |
| `get_email(ref)` | Get an email with full content, attachments, flag colour, and its current account/mailbox. Takes a **list** (max 50) to fetch a whole page in one round-trip |
| — | *Every tool above takes either the numeric id or the RFC822 `Message-ID` header. Prefer the header: the numeric id is a per-mailbox ROWID and stops resolving as soon as another device files the mail elsewhere.* |
| `search(query, scope?, before?, after?, highlight?)` | Search — all, subject, sender, body, attachments |
| `get_email_links(message_id)` | Extract links from an email |
| `get_email_attachment(message_id, filename)` | Extract attachment content |
| `get_attachment(message_id, filename)` | *Deprecated* — use `get_email_attachment()` |
| `set_flag(refs, color?)` | **Write** — flag/unflag one email or a batch (max 500), optionally by color (red, orange, yellow, green, blue, purple, gray) |
| `set_read_status(refs, read?)` | **Write** — mark one email or a batch read (seen) or unread (unseen) |
| `get_index_status()` | Index health and setup diagnostics — build state, progress, and whether Full Disk Access is missing |
| `refresh_index(full?)` | Update the index on demand — the index otherwise syncs only at server start |

### Fewer round-trips

Two things make surveys and triage cheap, because the cost of reading
mail here is the round-trip, not the read — a single message comes off
disk in 1-5 ms:

- **`get_emails(account="all")`** answers across every account at once.
  Without it you need one call per account, and you have to know their
  names first.
- **`get_email([ref, ref, ...])`** fetches up to 50 messages in one
  call. The result is one entry per reference, in order, each either
  `{"ref", "email"}` or `{"ref", "error"}` — one unreadable message
  never sinks the batch. The cap is about how much text fits in a
  model's context, not about speed.
- **Listings already carry `flag_color`** for flagged messages,
  resolved for the whole page in one call. Reading a colour scheme back
  needs no per-message follow-up.

A survey that used to take 58 tool calls now takes one.

### Flag colours carry no meaning

`set_flag` writes Apple Mail's seven colours and nothing more. This
server attaches **no** semantics to any of them — red as urgent, red as
read-later, red as one particular client are all equally valid, and
which one applies is the user's own convention. It belongs wherever
they keep their instructions, not in this tool. A model that does not
know what a colour means here is told to ask rather than invent a
scheme.

### An incomplete search is never reported as absence

A message is reported missing only when the search actually covered
everywhere it could be. A mailbox cap, a mailbox Mail refused to read,
a skipped trash folder, a timeout, a denied Automation permission — all
of these leave the question open, and the result says so instead of
implying the mail is gone. Write results carry a `diagnostics` block
naming what was searched and what was not.

### Write operations

`set_flag` and `set_read_status` take a single message id or a list, and
return per-id outcome buckets (`updated`, `unchanged`, `not_found`,
`skipped_hidden`) so partial success is always visible. Messages that
already hold the requested state are reported as `unchanged` and never
re-written — each write is a server round-trip on IMAP/Exchange. Both are refused when the server runs
read-only (`APPLE_MAIL_READ_ONLY=true`, `[server] read_only = true`, or
`apple-mail-mcp serve -r`). Message ids are located via the search index;
when it can't place an id, the tools fall back to an `account`+`mailbox`
hint and then to a bounded mailbox scan, so they work even before the index
exists. If a message has been moved in the meantime — by your phone, another
mail client, or a server-side rule — its id is dead; the tools then re-find it
by its RFC822 `Message-ID`, which survives moves, and report that in `hint`. The index is built automatically on first run (see
`APPLE_MAIL_INDEX_AUTO_BUILD`), so no manual setup is required.

## Performance

Tested against [6 other Apple Mail MCP servers](https://imdinu.github.io/apple-mail-mcp/benchmarks/) on a real **~73K-message** mailbox:

- **Only server with full-coverage body search.** Most competitors don't support body search at all; the one that does (BastianZim) live-scans only the 5000 most recent messages — silent miss on anything older. Our FTS5 index covers the entire mailbox.
- **~3ms single email fetch** via disk-first `.emlx` reading (no JXA round-trip).
- **~1ms `list_accounts` and ~5ms 50-email listing** via direct Envelope-Index SQLite reads (0.4+) — same path BastianZim/rusty/pl-lyfx use, with JXA as the correctness fallback.
- **~7ms subject search** via FTS5 — competitive with native Rust on the same operation.
- **Reliable across all 6 benchmarked operations** on a 73K mailbox; AppleScript-based servers timeout, throw syntax errors, or skip operations they don't support.

![Capability Matrix](docs/benchmark_overview.png)

## Configuration

Apple Mail MCP works out of the box. To customize defaults, run
`apple-mail-mcp init` to generate a `config.toml` template — or use
the matching `APPLE_MAIL_*` environment variables. See the
[Configuration docs](https://imdinu.github.io/apple-mail-mcp/configuration/)
for the full schema and the CLI > env > file > default precedence.

Per-client env overrides via the MCP client's launch config also work:

```json
{
  "mcpServers": {
    "mail": {
      "command": "apple-mail-mcp",
      "args": ["--watch"],
      "env": {
        "APPLE_MAIL_DEFAULT_ACCOUNT": "Work"
      }
    }
  }
}
```

## CLI Usage

All tools are also available as standalone CLI commands (no MCP server needed):

```bash
apple-mail-mcp search "quarterly report" --scope subject
apple-mail-mcp search "invoice" --after 2026-01-01 --limit 10
apple-mail-mcp read 12345
apple-mail-mcp emails --filter unread --limit 10
apple-mail-mcp accounts
apple-mail-mcp mailboxes --account Work
apple-mail-mcp extract 12345 invoice.pdf
```

All commands output JSON. Generate a [Claude Code skill](https://imdinu.github.io/apple-mail-mcp/configuration/#cli-commands) for CLI-based access:

```bash
apple-mail-mcp integrate claude > ~/.claude/skills/apple-mail.md
```

## Development

```bash
git clone https://github.com/imdinu/apple-mail-mcp
cd apple-mail-mcp
uv sync
uv run ruff check src/
uv run pytest
```

## License

GPL-3.0-or-later
