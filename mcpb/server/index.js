#!/usr/bin/env node
/**
 * Apple Mail MCP — Claude Desktop (.mcpb) launcher shim.
 *
 * Claude Desktop runs this file with its bundled Node runtime. The shim
 * launches the Python MCP server via `uvx` and transparently proxies
 * stdio in both directions, so the .mcpb stays tiny — no bundled Python
 * or platform wheels — and uv fetches the correct macOS wheels on first
 * run.
 *
 * The server source is pulled from the public fork by git ref, so the
 * bundle tracks a specific branch/tag without needing to be re-packed.
 * Override the ref with APPLE_MAIL_MCP_REF (a tag, or the plain PyPI
 * name "apple-mail-mcp" once the write tools are released).
 */
import { spawn } from "node:child_process";
import { existsSync, accessSync, constants } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

const DEFAULT_REF =
  "git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read";
const ref = process.env.APPLE_MAIL_MCP_REF || DEFAULT_REF;

const home = homedir();
// The uv installer drops uvx in ~/.local/bin; Homebrew in
// /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel); cargo in
// ~/.cargo/bin. Claude Desktop launches MCP servers with a minimal PATH
// that usually omits these, so probe them explicitly.
const EXTRA_BIN_DIRS = [
  join(home, ".local", "bin"),
  "/opt/homebrew/bin",
  "/usr/local/bin",
  join(home, ".cargo", "bin"),
];

function resolveUvx() {
  if (process.env.UVX_BIN && existsSync(process.env.UVX_BIN)) {
    return process.env.UVX_BIN;
  }
  const dirs = [...(process.env.PATH || "").split(":"), ...EXTRA_BIN_DIRS];
  for (const dir of dirs) {
    if (!dir) continue;
    const candidate = join(dir, "uvx");
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {
      /* keep looking */
    }
  }
  return null;
}

const uvx = resolveUvx();
if (!uvx) {
  process.stderr.write(
    "[apple-mail-mcp] Could not find 'uvx'. Install uv " +
      "(https://docs.astral.sh/uv/) or set UVX_BIN to its absolute path.\n",
  );
  process.exit(1);
}

// Give the child (and any `uv` it spawns) a sane PATH so it can find
// git, python, etc. even under Desktop's minimal environment.
const env = {
  ...process.env,
  PATH: `${EXTRA_BIN_DIRS.join(":")}:${process.env.PATH || ""}`,
};

// Set APPLE_MAIL_MCP_REFRESH=1 to re-resolve a moving branch ref on
// launch (picks up new commits). Off by default — a plain launch reuses
// uv's cached build for a fast startup.
const args = ["--from", ref];
if (process.env.APPLE_MAIL_MCP_REFRESH) args.push("--refresh");
args.push("apple-mail-mcp");

const child = spawn(uvx, args, {
  stdio: ["pipe", "pipe", "inherit"], // stderr passes through to Desktop logs
  env,
});

// Byte-transparent JSON-RPC proxy between Desktop and the Python server.
process.stdin.pipe(child.stdin);
child.stdout.pipe(process.stdout);

child.on("error", (err) => {
  process.stderr.write(
    `[apple-mail-mcp] failed to launch uvx: ${err.message}\n`,
  );
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => child.kill(sig));
}
