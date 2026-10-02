#!/usr/bin/env bash
# Build dist/image-tools.plugin: a zip of the plugin that can be uploaded directly in Claude Desktop.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/.claude-plugin" "$STAGE/server" "$ROOT/dist"
cp "$ROOT/.claude-plugin/plugin.json" "$STAGE/.claude-plugin/"
cp "$ROOT/.mcp.json" "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/"
cp "$ROOT/server/image_tools.py" "$STAGE/server/"
cp -R "$ROOT/skills" "$STAGE/"

if command -v claude >/dev/null; then
  claude plugin validate "$STAGE"
fi

OUT="$ROOT/dist/image-tools.plugin"
rm -f "$OUT"
(cd "$STAGE" && zip -qr "$OUT" . -x "*.DS_Store")
echo "Built $OUT"
