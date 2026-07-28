#!/usr/bin/env bash
#
# Build the Claude Desktop (.mcpb) bundle for apple-mail-mcp.
#
# The bundle is a thin launcher (mcpb/server/index.js) that runs the
# Python MCP server via `uvx`; no Python is bundled. Output is a single
# platform-independent .mcpb you double-click to install in Claude
# Desktop.
#
# The file name carries the version, so a downloaded bundle says which
# one it is without being opened — an unversioned name cost a full
# debugging round when a stale download looked like the current build.
#
# Usage:
#   ./scripts/build-mcpb.sh            # -> dist/apple-mail-mcp-<version>.mcpb
#   ./scripts/build-mcpb.sh path/to/out.mcpb
#
# Requires Node 18+ (for the `mcpb` CLI). Uses a global `mcpb` if present,
# otherwise falls back to `npx @anthropic-ai/mcpb`.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(node -p "require('$REPO/mcpb/manifest.json').version")"
OUT="${1:-$REPO/dist/apple-mail-mcp-$VERSION.mcpb}"
mkdir -p "$(dirname "$OUT")"

# Pre-flight. Twice in one day a bundle was built from a tree whose
# tests could not even be collected — once with merge markers still in
# a test file. Care did not catch it; this does. Set MCPB_SKIP_CHECKS=1
# only when you know exactly why.
if [ "${MCPB_SKIP_CHECKS:-0}" != "1" ]; then
  if grep -rlE '^(<<<<<<<|>>>>>>>) ' "$REPO/src" "$REPO/tests" 2>/dev/null | head -1 | grep -q .; then
    echo "[build] REFUSED: merge conflict markers in src/ or tests/" >&2
    exit 1
  fi
  echo "[build] running checks..."
  uv run ruff check "$REPO/src" >/dev/null || { echo "[build] REFUSED: lint" >&2; exit 1; }
  uv run ruff format --check "$REPO/src" >/dev/null || { echo "[build] REFUSED: format" >&2; exit 1; }
  uv run pytest -q >/dev/null 2>&1 || { echo "[build] REFUSED: tests fail" >&2; exit 1; }
  echo "[build] checks passed"
fi

if command -v mcpb >/dev/null 2>&1; then
  MCPB=(mcpb)
else
  MCPB=(npx -y @anthropic-ai/mcpb@latest)
fi

"${MCPB[@]}" validate "$REPO/mcpb/manifest.json"
"${MCPB[@]}" pack "$REPO/mcpb" "$OUT"

echo "[build] bundle ready at $OUT (version $VERSION)"
