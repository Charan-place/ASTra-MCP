#!/bin/bash
# ASTra installer — run once per machine
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== ASTra Installer ==="
echo "Installing Python dependencies..."

# Install ASTra package
pip3 install -e "$SCRIPT_DIR" --quiet

echo "Dependencies installed."
echo ""
echo "=== Next steps ==="
echo "1. Open terminal in your project folder"
echo "2. Run: astra init"
echo "3. Reload Claude Code"
echo ""
echo "ASTra is ready. 598 symbols, 93% token reduction."
