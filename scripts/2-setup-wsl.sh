#!/usr/bin/env bash
set -euo pipefail
TARGET_DIR="${COCKPIT_HOME:-$HOME/agentic-cockpit}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$TARGET_DIR"
if [ "$SRC_DIR" != "$TARGET_DIR" ]; then
  cp -a "$SRC_DIR"/. "$TARGET_DIR"/
fi
chmod +x "$TARGET_DIR/bin/cockpit" "$TARGET_DIR/scripts/"*.sh "$TARGET_DIR/tui/pmo_dashboard.py"

if ! command -v vibe >/dev/null 2>&1; then
  uv tool install mistral-vibe 2>/dev/null || pip install --user mistral-vibe 2>/dev/null || true
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$TARGET_DIR/bin/cockpit" "$HOME/.local/bin/cockpit"
echo "OK : Agentic Cockpit V2 installé dans $TARGET_DIR (commande 'cockpit' prête)."
