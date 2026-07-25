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
 * The server source is pulled from the public fork by git ref. Because
 * that ref is a moving branch, the shim also self-updates: at most once
 * per UPDATE_INTERVAL_H it re-resolves the ref in a short, best-effort
 * pre-step before starting the server. A failed or slow update never
 * blocks startup — the previously cached build is used instead.
 *
 * Every knob is surfaced through the bundle's Configure dialog
 * (manifest `user_config`), which injects the env vars read below.
 */
import { spawn } from "node:child_process";
import {
  existsSync,
  accessSync,
  constants,
  statSync,
  readFileSync,
  writeFileSync,
  mkdirSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

const DEFAULT_REF =
  "git+https://github.com/iret77/apple-mail-mcp@feat/write-ops-flag-read";
const UPDATE_INTERVAL_H = 24;
// Bump on every bundle release. The stamp records it, so installing a
// new bundle always re-resolves the server once — otherwise a same-day
// bundle update would run against a cached, older server build.
const LAUNCHER_REVISION = "0.5.5";
const UPDATE_TIMEOUT_MS = 45_000; // stay well under the MCP init timeout

/** Env values arrive as strings — "false" must not read as truthy. */
function envFlag(name, fallback = false) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  return /^(1|true|yes|on)$/i.test(raw.trim());
}

const ref = (process.env.APPLE_MAIL_MCP_REF || "").trim() || DEFAULT_REF;
const autoUpdate = envFlag("APPLE_MAIL_MCP_AUTO_UPDATE", true);
const forceRefresh = envFlag("APPLE_MAIL_MCP_REFRESH", false);

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
  // Tell the server it was started from the bundle. It has no
  // `apple-mail-mcp` on the user's PATH, so any command it suggests
  // must go through uvx — and it needs `ref` to name the right source.
  APPLE_MAIL_MCP_LAUNCHER: "mcpb",
  APPLE_MAIL_MCP_REF: ref,
};

const stampDir = join(home, ".apple-mail-mcp");
const stampFile = join(stampDir, ".mcpb-update-stamp");

/** Identity of what we last resolved: bundle revision + source ref. */
const stampId = `${LAUNCHER_REVISION}|${ref}`;

function updateIsDue() {
  if (forceRefresh) return true;
  if (!autoUpdate) return false;
  try {
    // A different bundle revision or a different source means the
    // cached build is not what this launcher expects — refresh now,
    // regardless of age.
    if (readFileSync(stampFile, "utf8").trim() !== stampId) return true;
    const ageH = (Date.now() - statSync(stampFile).mtimeMs) / 3_600_000;
    return ageH >= UPDATE_INTERVAL_H;
  } catch {
    return true; // no stamp yet → first run
  }
}

function touchStamp() {
  try {
    mkdirSync(stampDir, { recursive: true });
    writeFileSync(stampFile, stampId);
  } catch {
    /* stamping is best-effort */
  }
}

/**
 * Best-effort pre-step: re-resolve the moving ref so the launch below
 * picks up new commits. Bounded and non-fatal — on timeout or error we
 * simply run whatever uv already has cached.
 */
async function refreshCache() {
  process.stderr.write("[apple-mail-mcp] checking for updates...\n");
  await new Promise((resolve) => {
    const p = spawn(
      uvx,
      ["--refresh", "--from", ref, "apple-mail-mcp", "--version"],
      { stdio: "ignore", env },
    );
    const timer = setTimeout(() => {
      p.kill("SIGKILL");
      process.stderr.write(
        "[apple-mail-mcp] update timed out; using cached build\n",
      );
      resolve();
    }, UPDATE_TIMEOUT_MS);
    const done = () => {
      clearTimeout(timer);
      resolve();
    };
    p.on("error", done);
    p.on("exit", done);
  });
  touchStamp();
}

if (updateIsDue()) await refreshCache();

const child = spawn(uvx, ["--from", ref, "apple-mail-mcp"], {
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
