#!/usr/bin/env bash
# Build the Claude Desktop extension (dist/image-tools.mcpb) from the shared server code.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/server" "$ROOT/dist"
cp "$ROOT/desktop/manifest.json" "$ROOT/desktop/pyproject.toml" "$STAGE/"
cp "$ROOT/server/image_tools.py" "$STAGE/server/"
cp "$ROOT/LICENSE" "$STAGE/"
[ -f "$ROOT/desktop/icon.png" ] && cp "$ROOT/desktop/icon.png" "$STAGE/"

npx -y @anthropic-ai/mcpb validate "$STAGE/manifest.json"
npx -y @anthropic-ai/mcpb pack "$STAGE" "$ROOT/dist/image-tools.mcpb"
echo "Built $ROOT/dist/image-tools.mcpb"
