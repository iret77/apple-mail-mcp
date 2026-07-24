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
2. Build the search index once (grant the automation prompt on first
   run):

   ```bash
   uvx --from git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read apple-mail-mcp index --verbose
   ```

3. Talk to Claude: *"flag mail 12345 red"*, *"mark these three as
   unread"*. The write tools return per-id buckets
   (`updated` / `not_found` / `skipped_hidden`).

## Configuration

The launcher reads a few environment variables (set them in the bundle's
config, or your shell for the `index` command):

| Variable | Purpose |
|---|---|
| `APPLE_MAIL_MCP_REF` | Which build to run. Default: `git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read`. Point at a tag, or the plain `apple-mail-mcp` once the write tools ship to PyPI. |
| `APPLE_MAIL_MCP_REFRESH` | Set to `1` to re-resolve a moving branch on launch (picks up new commits). Off by default for a fast startup. |
| `UVX_BIN` | Absolute path to `uvx` if it's not in a standard location. |
| `APPLE_MAIL_READ_ONLY` | `true` disables the write tools. |

All the usual `APPLE_MAIL_*` settings apply too — see the
[main README](../README.md).

## Updating to the latest branch commit

`uvx` caches the resolved build. To pull newer commits from the branch,
refresh once (next Desktop launch uses it), or set `APPLE_MAIL_MCP_REFRESH=1`:

```bash
uvx --refresh --from git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read apple-mail-mcp --help
```

## License / attribution

This is a fork of [imdinu/apple-mail-mcp](https://github.com/imdinu/apple-mail-mcp)
(GPL-3.0); the bundle inherits that license. Not affiliated with or
endorsed by Apple Inc.
