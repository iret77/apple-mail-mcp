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

```bash
pipx install apple-mail-mcp
```

Add to your MCP client:

```json
{
  "mcpServers": {
    "mail": {
      "command": "apple-mail-mcp"
    }
  }
}
```

### Permissions: who needs Full Disk Access?

Building the index reads `~/Library/Mail`, which macOS protects. TCC
grants that access to the **process that launches the server**, not to
this package — the single most common setup mistake:

| Index built by | Grant Full Disk Access to |
|---|---|
| The server, automatically | **the app that starts it** (e.g. Claude Desktop) |
| You, via `apple-mail-mcp index` | **the terminal app** you run it in |

Granting it to your terminal does *not* help when an MCP client spawns
the server — the client is the responsible app then.

If you'd rather not grant your MCP client full disk access, use the
manual route: set `APPLE_MAIL_INDEX_AUTO_BUILD=false`, build the index
from a terminal that has access, and the server will read it from
`~/.apple-mail-mcp/index.db` (not a protected path). Search and the
write tools keep working; only live disk reads fall back to a slower
path.

Call the `get_index_status()` tool at any time — it reports which setup
is active and what to do next.

### Build the Search Index (Recommended)

```bash
# Requires Full Disk Access for Terminal
# System Settings → Privacy & Security → Full Disk Access → Add Terminal

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
| `get_emails(filter?, limit?)` | Get emails — all, unread, flagged, today, last_7_days |
| `get_email(message_id)` | Get single email with full content + attachments |
| `search(query, scope?, before?, after?, highlight?)` | Search — all, subject, sender, body, attachments |
| `get_email_links(message_id)` | Extract links from an email |
| `get_email_attachment(message_id, filename)` | Extract attachment content |
| `get_attachment(message_id, filename)` | *Deprecated* — use `get_email_attachment()` |
| `set_flag(message_ids, color?)` | **Write** — flag/unflag one email or a batch, optionally by color (red, orange, yellow, green, blue, purple, gray) |
| `set_read_status(message_ids, read?)` | **Write** — mark one email or a batch read (seen) or unread (unseen) |
| `get_index_status()` | Index health and setup diagnostics — build state, progress, and whether Full Disk Access is missing |

### Write operations

`set_flag` and `set_read_status` take a single message id or a list, and
return per-id outcome buckets (`updated`, `not_found`, `skipped_hidden`) so
partial success is always visible. Both are refused when the server runs
read-only (`APPLE_MAIL_READ_ONLY=true`, `[server] read_only = true`, or
`apple-mail-mcp serve -r`). Message ids are located via the search index;
when it can't place an id, the tools fall back to an `account`+`mailbox`
hint and then to a bounded mailbox scan, so they work even before the index
exists. The index is built automatically on first run (see
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
