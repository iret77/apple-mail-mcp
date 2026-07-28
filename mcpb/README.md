# Apple Mail — Claude Desktop bundle (`.mcpb`)

A double-click installer for **Claude Desktop / Cowork** that adds the
Apple Mail MCP tools — including the write tools (`set_flag`,
`set_read_status`).

The bundle is deliberately tiny (~3 kB): `server/index.js` is a Node
launcher shim that starts the Python MCP server via **`uvx`** and proxies
stdio. No Python is bundled — `uv` fetches the right macOS wheels on first
run, and the server code is pulled from the public fork by git ref.

## Prerequisites (on the Mac)

- **macOS** with Apple Mail configured.
- **uv** installed (provides `uvx`): <https://docs.astral.sh/uv/>. The
  shim probes `~/.local/bin`, Homebrew, and `~/.cargo/bin`, so a standard
  uv install is found even under Claude Desktop's minimal `PATH`. If uv
  lives elsewhere, set `UVX_BIN` to its absolute path.
- **Full Disk Access** for whatever builds the search index (needed for
  `.emlx` reads and reliable message-id resolution): System Settings →
  Privacy & Security → Full Disk Access.

## Build the bundle

From a checkout (any OS with Node 18+ — the `.mcpb` is
platform-independent):

```bash
./scripts/build-mcpb.sh          # -> dist/apple-mail-mcp.mcpb
```

## Install

1. Double-click `dist/apple-mail-mcp.mcpb`. Claude Desktop shows an
   install dialog → **Install**.
2. Talk to Claude: *"flag mail 12345 red"*, *"mark these three as
   unread"*. Grant the Mail automation prompt on the first tool call.

That's it — no terminal step. The search index builds itself in the
background on first run (needs Full Disk Access for Claude Desktop), and
the write tools resolve message ids by scanning meanwhile, so they work
right away. The write tools return per-id buckets
(`updated` / `not_found` / `skipped_hidden`).

To build the index up front instead, run:

```bash
uvx --from git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read apple-mail-mcp index --verbose
```

## Two setups — pick one

Body search needs an index built from `~/Library/Mail`, which macOS
protects. TCC grants that access to the *responsible app*, so a bundled
server can only inherit it from Claude itself — there is no way to scope
it to this extension alone. Both routes are supported:

**A — Automatic (convenient).** Grant Claude **Full Disk Access**
(System Settings → Privacy & Security → Full Disk Access), restart it,
and leave *Build the search index automatically* on. The index builds
itself on first run and stays current. Note this grants disk access to
Claude as a whole, not just to this extension.

**B — Manual (least privilege).** Don't grant Claude disk access; turn
*Build the search index automatically* **off**, and build the index from
a terminal that has Full Disk Access:

```bash
apple-mail-mcp index --verbose
```

The extension then reads that index from `~/.apple-mail-mcp/index.db`,
which is not a protected path. Re-run the command (or schedule it via
launchd) to pick up new mail.

| | A — Automatic | B — Manual |
|---|---|---|
| Body search | ✅ always current | ✅ as of the last build |
| Flag / read-unread | ✅ | ✅ (Apple Events, Mail-scoped only) |
| Single-email read | ~3 ms (disk) | slower live path |
| Claude's disk access | full | none |
| Upkeep | none | re-run the index command |

`get_index_status()` reports which mode is active and what to do next.

## Configuration

Open **Claude Desktop → the extension → Configure**. The bundle declares
these fields, so they're editable in the UI — no environment variables
and no terminal needed:

| Field | Default | Purpose |
|---|---|---|
| **Automatic updates** | on | Check once a day for a newer build at startup. |
| **Build the search index automatically** | on | Off switches to the manual (no-disk-access) mode below. |
| **Read-only mode** | off | Disable the write tools (`set_flag`, `set_read_status`). |
| **Default account** | — | Account used when a request doesn't name one. |
| **Hidden accounts** | — | Comma-separated accounts to hide completely (never indexed, searched, read, or written). |
| **Source (advanced)** | — | Which build to run; empty = the default branch build. Set to `apple-mail-mcp` once the write tools ship to PyPI. |

`UVX_BIN` remains an environment-only escape hatch for a non-standard
`uvx` location. All the usual `APPLE_MAIL_*` settings apply too — see the
[main README](../README.md).

## Updating

Automatic: with **Automatic updates** on (the default), the launcher
re-resolves the branch at most once every 24 h when Claude Desktop starts
the extension. It's bounded (45 s) and best-effort — a slow or failed
check never blocks startup; the last working build is used instead.

To force an update now, restart the extension after:

```bash
uvx --refresh --from git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read apple-mail-mcp --version
```

## License / attribution

This is a fork of [imdinu/apple-mail-mcp](https://github.com/imdinu/apple-mail-mcp)
(GPL-3.0); the bundle inherits that license. Not affiliated with or
endorsed by Apple Inc.
