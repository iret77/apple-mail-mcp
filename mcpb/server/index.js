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
 * ONBOARDING — the shim never dies silently. The bundle promises
 * "double-click, it works", but the Python server needs `uv`. So:
 *   1. If `uvx` is missing, the shim installs uv itself (best-effort,
 *      into ~/.apple-mail-mcp/bin, no sudo, no PATH edits, bounded).
 *   2. If it still cannot get uv (offline, locked-down Mac, opt-out),
 *      it starts a minimal in-process MCP server that CONNECTS normally
 *      and answers every tool call with a plain-language fix. Claude
 *      Desktop drops the child's stderr, so a bare exit(1) would show
 *      the user only "Server disconnected" — a dead end. The fallback
 *      turns that into a message the user can act on, inside Claude.
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
  unlinkSync,
  mkdirSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

// Pinned to a TAG, not a branch. Following a moving branch meant every
// installation silently swapped its server within UPDATE_INTERVAL_H of
// any push — testers would report against a build nobody could name.
// A new server reaches them only through a new bundle.
const DEFAULT_REF =
  "git+https://github.com/iret77/apple-mail-mcp@server-v0.20.6";
const UPDATE_INTERVAL_H = 24;
// Bump on every bundle release. The stamp records it, so installing a
// new bundle always re-resolves the server once — otherwise a same-day
// bundle update would run against a cached, older server build.
const LAUNCHER_REVISION = "0.20.6";
// A cold `uvx --refresh` clones the repo and builds a wheel; 45s was
// not enough on a first run, and the timeout then marked the update as
// done anyway (see refreshCache) — leaving the old server in place for
// a full day. Give it room, but stay under the MCP init timeout.
const UPDATE_TIMEOUT_MS = 120_000;
// Installing uv is a one-time curl+unpack of a notarized binary; the
// same ceiling keeps it under the MCP init timeout.
const INSTALL_TIMEOUT_MS = 120_000;

/** Env values arrive as strings — "false" must not read as truthy. */
function envFlag(name, fallback = false) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  return /^(1|true|yes|on)$/i.test(raw.trim());
}

const ref = (process.env.APPLE_MAIL_MCP_REF || "").trim() || DEFAULT_REF;
const autoUpdate = envFlag("APPLE_MAIL_MCP_AUTO_UPDATE", true);
const forceRefresh = envFlag("APPLE_MAIL_MCP_REFRESH", false);
const autoInstallUv = envFlag("APPLE_MAIL_MCP_AUTO_INSTALL_UV", true);

const home = homedir();
// Our own, app-private prefix. The server already writes its index and
// the update stamp here, so write access is proven, not assumed — and
// it is NOT a TCC-protected path (unlike Desktop/Documents/Mail), so
// installing here raises no permission dialog.
const appDir = join(home, ".apple-mail-mcp");
const binDir = join(appDir, "bin");

// Where a `uvx` might live. Our own binDir first (so a shim-installed uv
// wins), then the standard locations: the uv installer drops uvx in
// ~/.local/bin; Homebrew in /opt/homebrew/bin (Apple Silicon) or
// /usr/local/bin (Intel); cargo in ~/.cargo/bin. Claude Desktop launches
// MCP servers with a minimal PATH that usually omits these, so probe
// them explicitly.
const EXTRA_BIN_DIRS = [
  binDir,
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

// --------------------------------------------------------------------
// Provisioning: install uv when it is missing.
// --------------------------------------------------------------------

/**
 * Best-effort, never fatal. Returns { ok: true, uvx } on success, or
 * { ok: false, reason } where reason drives the fallback message:
 *   "path"    — our install dir is not writable; do not even try.
 *   "install" — network/installer/timeout failure.
 * The install goes into our own binDir (no sudo, no PATH edits) using
 * the official notarized installer; a file downloaded by curl carries no
 * quarantine attribute, so Gatekeeper does not block the binary.
 */
async function provisionUv() {
  // Preflight the exact question "is the install path writable?" at
  // runtime instead of assuming it. A locked-down or read-only home
  // fails here and skips straight to a specific fallback message.
  try {
    mkdirSync(binDir, { recursive: true });
    const probe = join(binDir, ".write-probe");
    writeFileSync(probe, "ok");
    unlinkSync(probe);
  } catch (e) {
    process.stderr.write(
      `[apple-mail-mcp] cannot write ${binDir}: ${e}\n`,
    );
    return { ok: false, reason: "path" };
  }

  process.stderr.write(
    `[apple-mail-mcp] 'uv' not found — installing it into ${binDir} ` +
      "(one-time, no admin rights)...\n",
  );

  const ok = await new Promise((resolve) => {
    const p = spawn(
      "/bin/sh",
      ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
      {
        stdio: ["ignore", "ignore", "inherit"],
        env: {
          ...env,
          // Land in our binDir, and never touch the user's shell
          // profiles — the shim finds uv by absolute path, so PATH
          // edits are neither needed nor wanted.
          UV_INSTALL_DIR: binDir,
          INSTALLER_NO_MODIFY_PATH: "1",
          UV_NO_MODIFY_PATH: "1",
        },
      },
    );
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      p.kill("SIGKILL");
      process.stderr.write(
        "[apple-mail-mcp] uv install timed out after " +
          INSTALL_TIMEOUT_MS / 1000 +
          "s; the extension will retry on next start.\n",
      );
      finish(false);
    }, INSTALL_TIMEOUT_MS);
    p.on("error", (e) => {
      process.stderr.write(
        "[apple-mail-mcp] uv install could not start: " + e + "\n",
      );
      finish(false);
    });
    p.on("exit", (code) => finish(code === 0));
  });

  if (!ok) return { ok: false, reason: "install" };
  // The installer exited clean; confirm the binary is actually there
  // and runnable before we commit to the real path.
  const found = resolveUvx();
  if (!found) {
    process.stderr.write(
      "[apple-mail-mcp] uv install reported success but uvx is not " +
        "on any known path.\n",
    );
    return { ok: false, reason: "install" };
  }
  process.stderr.write(`[apple-mail-mcp] uv installed: ${found}\n`);
  return { ok: true, uvx: found };
}

// --------------------------------------------------------------------
// Fallback: a minimal MCP server that explains the setup, in Claude.
// --------------------------------------------------------------------

// The real tool set (mirrors the manifest). Listing the real names
// keeps the UI honest and means whatever the user asks for lands on the
// setup message instead of failing mutely.
const TOOL_NAMES = [
  "list_accounts",
  "list_mailboxes",
  "get_emails",
  "get_email",
  "search",
  "get_email_links",
  "get_email_attachment",
  "get_attachment",
  "set_flag",
  "set_read_status",
  "get_index_status",
  "refresh_index",
];

function setupMessage(reason) {
  const manual =
    "To fix it by hand, open Terminal and run:\n" +
    "    curl -LsSf https://astral.sh/uv/install.sh | sh\n" +
    "then fully quit Claude Desktop (Cmd-Q) and reopen it.";
  const base =
    "Apple Mail for Claude isn't ready yet: the helper program 'uv' is " +
    "missing. uv is what launches the mail server in the background.";
  if (reason === "path") {
    return (
      base +
      "\n\nAutomatic installation could not run because the folder " +
      "~/.apple-mail-mcp is not writable on this Mac. Please install uv " +
      "manually.\n\n" +
      manual
    );
  }
  if (reason === "disabled") {
    return (
      base +
      "\n\nAutomatic uv installation is turned OFF in this extension's " +
      "settings (Configure → “Install uv automatically”). Turn it " +
      "back on and restart Claude, or install uv manually.\n\n" +
      manual
    );
  }
  // "install" and anything else: most often no network on first launch.
  return (
    base +
    "\n\nAutomatic installation failed — most often there is simply no " +
    "internet connection on the first launch. Make sure this Mac is " +
    "online and restart Claude Desktop; the extension installs uv itself. " +
    "Or install it manually.\n\n" +
    manual
  );
}

/**
 * Speak just enough MCP (line-delimited JSON-RPC over stdio) to connect
 * cleanly and hand back the setup message. Never returns — the stdin
 * listener keeps the process alive until Desktop closes the pipe.
 */
function startFallbackServer(reason) {
  const message = setupMessage(reason);
  process.stderr.write(
    `[apple-mail-mcp] setup required (${reason}) — running the ` +
      "in-Claude setup helper instead of the mail server.\n",
  );

  const send = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
  const reply = (id, result) => send({ jsonrpc: "2.0", id, result });

  function handle(line) {
    let req;
    try {
      req = JSON.parse(line);
    } catch {
      return; // ignore anything that is not a JSON message
    }
    const { id, method } = req;

    switch (method) {
      case "initialize":
        reply(id, {
          protocolVersion:
            (req.params && req.params.protocolVersion) || "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: {
            name: "apple-mail-mcp",
            version: LAUNCHER_REVISION,
          },
          instructions: message,
        });
        return;
      case "tools/list":
        reply(id, {
          tools: TOOL_NAMES.map((name) => ({
            name,
            description:
              "⚠️ Setup required — unavailable until uv is " +
              "installed. Call it to see how to finish setup.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: true,
            },
          })),
        });
        return;
      case "tools/call":
        // A successful envelope with isError:true is how MCP surfaces a
        // tool-level problem as readable text rather than a protocol error.
        reply(id, { content: [{ type: "text", text: message }], isError: true });
        return;
      case "ping":
        reply(id, {});
        return;
      case "resources/list":
        reply(id, { resources: [] });
        return;
      case "resources/templates/list":
        reply(id, { resourceTemplates: [] });
        return;
      case "prompts/list":
        reply(id, { prompts: [] });
        return;
      default:
        // Notifications (no id) get no response, per JSON-RPC.
        if (id === undefined || id === null) return;
        send({
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: `Method not found: ${method}` },
        });
    }
  }

  let buf = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) handle(line);
    }
  });
  process.stdin.on("end", () => process.exit(0));
  process.stdin.on("error", () => process.exit(0));
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => process.exit(0));
  }
}

// --------------------------------------------------------------------
// Real server: self-update, then proxy to the Python MCP server.
// --------------------------------------------------------------------

const stampDir = appDir;
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
async function refreshCache(uvx) {
  process.stderr.write("[apple-mail-mcp] checking for updates...\n");
  // Only a clean exit means the cache really holds the requested ref.
  // Stamping unconditionally turned a timed-out update into "already
  // up to date" and pinned the old server for UPDATE_INTERVAL_H — the
  // bundle said 0.10.2 while the server still reported the build from
  // two rounds earlier.
  const ok = await new Promise((resolve) => {
    const p = spawn(
      uvx,
      ["--refresh", "--from", ref, "apple-mail-mcp", "--version"],
      { stdio: "ignore", env },
    );
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      p.kill("SIGKILL");
      process.stderr.write(
        "[apple-mail-mcp] update timed out after " +
          UPDATE_TIMEOUT_MS / 1000 +
          "s; running the cached build. It will retry on next start. " +
          "To do it once by hand, without a timeout:\n" +
          "  uvx --refresh --from " +
          ref +
          " apple-mail-mcp --version\n",
      );
      finish(false);
    }, UPDATE_TIMEOUT_MS);
    p.on("error", (e) => {
      process.stderr.write("[apple-mail-mcp] update failed: " + e + "\n");
      finish(false);
    });
    p.on("exit", (code) => finish(code === 0));
  });
  if (ok) touchStamp();
}

function runServer(uvx) {
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
}

// --------------------------------------------------------------------
// Entry.
// --------------------------------------------------------------------

let uvx = resolveUvx();
let fallbackReason = null;

if (!uvx) {
  if (autoInstallUv) {
    const result = await provisionUv();
    if (result.ok) uvx = result.uvx;
    else fallbackReason = result.reason;
  } else {
    fallbackReason = "disabled";
  }
}

if (!uvx) {
  // Could not get a working uvx. Do not exit(1) into a silent
  // "disconnected" — connect and tell the user how to finish setup.
  startFallbackServer(fallbackReason || "install");
} else {
  if (updateIsDue()) await refreshCache(uvx);
  runServer(uvx);
}
