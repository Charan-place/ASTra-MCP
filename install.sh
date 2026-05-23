#!/bin/bash
# ASTra installer — run once per machine
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== ASTra Installer ==="
echo "Installing Python dependencies..."

# Install ASTra package
# Prefer python3.14 if available (matches plugin.json), fall back to pip3
if command -v python3.14 &>/dev/null; then
    python3.14 -m pip install -e "$SCRIPT_DIR" --quiet
else
    pip3 install -e "$SCRIPT_DIR" --quiet
fi

echo "Dependencies installed."
echo ""
echo "=== Next steps ==="
echo "1. Open terminal in your project folder"
echo "2. Run: astra init"
echo "3. Reload Claude Code"
echo ""
echo "ASTra is ready. 598 symbols, 93% token reduction."
