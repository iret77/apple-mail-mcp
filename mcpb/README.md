# Apple Mail — Claude Desktop bundle (`.mcpb`)

A double-click installer for **Claude Desktop / Cowork** that adds the
Apple Mail MCP tools — including the write tools (`set_flag`,
`set_read_status`).

The bundle is deliberately tiny (~3 kB): `server/index.js` is a Node
launcher shim that starts the Python MCP server via **`uvx`** and proxies
stdio. No Python is bundled — `uv` fetches the right macOS wheels on first
run, and the server code is pulled from the public fork by git ref: each
bundle pins one `server-vX.Y.Z` tag, so a new server arrives with a new
bundle.

## Prerequisites (on the Mac)

- **macOS** with Apple Mail configured.
- **uv** (provides `uvx`) — nothing to do by hand. If it is missing, the
  launcher installs it on first run into `~/.apple-mail-mcp/bin` (no admin
  rights, no `PATH` changes; setting *Install uv automatically*). If that
  is off or fails (offline, managed Mac), the extension still connects and
  answers every request with instructions to install uv by hand. An
  existing uv is found in `~/.local/bin`, Homebrew and `~/.cargo/bin` even
  under Claude Desktop's minimal `PATH`; anywhere else, set `UVX_BIN` to
  its absolute path.
- **Full Disk Access** for whatever builds the search index (needed for
  `.emlx` reads and reliable message-id resolution): System Settings →
  Privacy & Security → Full Disk Access.

## Build the bundle

From a checkout (any OS with Node 18+ — the `.mcpb` is
platform-independent):

```bash
./scripts/build-mcpb.sh          # -> dist/apple-mail-mcp-<version>.mcpb
```

## Install

1. Double-click `dist/apple-mail-mcp-<version>.mcpb`. Claude Desktop
   shows an install dialog → **Install**.
2. Talk to Claude: *"flag mail 12345 red"*, *"mark these three as
   unread"*. Grant the Mail automation prompt on the first tool call.

That's it — no terminal step. The search index builds itself in the
background on first run (needs Full Disk Access for Claude Desktop), and
the write tools resolve message ids by scanning meanwhile, so they work
right away. The write tools return per-reference buckets
(`updated` / `unchanged` / `not_found` / `failed` / `skipped_hidden`).

To build the index up front instead, run (`<tag>` is the `server-v…` tag
your bundle pins — `get_index_status()` shows it as `source_ref`):

```bash
uvx --from git+https://github.com/iret77/apple-mail-mcp@<tag> apple-mail-mcp index --verbose
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
| **Automatic updates** | on | Re-resolve the configured source once a day at startup. With the default pinned build this changes nothing; it matters when **Source** names a branch. |
| **Install uv automatically** | on | Install the `uv` helper on first run if it is missing. Off for managed Macs — the extension then shows how to install it by hand. |
| **Build the search index automatically** | on | Off switches to the manual (no-disk-access) mode above. |
| **Read-only mode** | off | Disable the write tools (`set_flag`, `set_read_status`). |
| **Default account** | — | Account used when a request doesn't name one. |
| **Hidden accounts** | — | Comma-separated accounts to hide completely (never indexed, searched, read, or written). |
| **Source (advanced)** | — | Which build to run; empty = the `server-v…` tag this bundle pins. Advanced: a uv requirement such as `apple-mail-mcp` (PyPI) or another git ref. |

`UVX_BIN` remains an environment-only escape hatch for a non-standard
`uvx` location. All the usual `APPLE_MAIL_*` settings apply too — see the
[main README](../README.md).

## Updating

A new server version arrives with a new bundle: install it over the old
one. The bundle pins one `server-v…` tag, and a tag does not move.

With **Automatic updates** on (the default), the launcher re-resolves its
source at most once every 24 h when Claude Desktop starts the extension,
and always once after a new bundle is installed. That only brings in new
code when **Source** names a moving branch. It is bounded (120 s) and
best-effort — a slow or failed check never blocks startup; the last
working build is used instead.

To force a refresh now, restart the extension after:

```bash
uvx --refresh --from git+https://github.com/iret77/apple-mail-mcp@<tag> apple-mail-mcp --version
```

## License / attribution

This is a fork of [imdinu/apple-mail-mcp](https://github.com/imdinu/apple-mail-mcp)
(GPL-3.0); the bundle inherits that license. Not affiliated with or
endorsed by Apple Inc.
