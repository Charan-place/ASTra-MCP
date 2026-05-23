#!/bin/bash
# ASTra installer — robust cross-platform setup
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== ASTra MCP Installer ==="

# ── 1. Pick a Python (3.10+) ─────────────────────────────────────────────────
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        VER=$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "")
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ -n "$MAJOR" ] && [ -n "$MINOR" ] && [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 10 ]; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "❌ Python 3.10+ not found on PATH."
    echo "   Install Python from https://python.org and re-run."
    exit 1
fi

echo "✅ Using $PY ($($PY --version))"

# ── 2. Verify pip ────────────────────────────────────────────────────────────
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "⚠️  pip missing for $PY. Bootstrapping…"
    "$PY" -m ensurepip --upgrade || {
        echo "❌ Could not bootstrap pip."
        exit 1
    }
fi

# ── 3. Install astra package ─────────────────────────────────────────────────
echo "📦 Installing ASTra and dependencies (may take 1-2 min on first run)…"
"$PY" -m pip install -e "$SCRIPT_DIR" --quiet --disable-pip-version-check 2>&1 || {
    echo "⚠️  System install failed. Trying user install…"
    "$PY" -m pip install --user -e "$SCRIPT_DIR" --quiet --disable-pip-version-check
}

# ── 4. Sanity check — can server import? ─────────────────────────────────────
if ! "$PY" -c "import astra.mcp.server" 2>/dev/null; then
    echo "❌ astra.mcp.server failed to import."
    echo "   Diagnose by running:"
    echo "     $PY -m astra.mcp.server"
    exit 1
fi
echo "✅ astra module imports OK."

# ── 5. Record interpreter for support diagnostics ────────────────────────────
cat > "$SCRIPT_DIR/.install_info" <<EOF
python=$PY
python_path=$(command -v "$PY")
version=$($PY --version 2>&1)
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

# ── 6. Warn if plugin.json's "python3" resolves to different binary ──────────
PLUGIN_PY=$(command -v python3 2>/dev/null || echo "")
RESOLVED_PY=$(command -v "$PY")
if [ -n "$PLUGIN_PY" ] && [ "$PLUGIN_PY" != "$RESOLVED_PY" ]; then
    if ! "$PLUGIN_PY" -c "import astra.mcp.server" 2>/dev/null; then
        echo ""
        echo "⚠️  WARNING: plugin.json uses 'python3' which resolves to:"
        echo "      $PLUGIN_PY"
        echo "   But astra was installed into:"
        echo "      $RESOLVED_PY"
        echo "   Installing astra into '$PLUGIN_PY' as well so Claude Code can find it…"
        "$PLUGIN_PY" -m pip install -e "$SCRIPT_DIR" --quiet --disable-pip-version-check 2>/dev/null || \
        "$PLUGIN_PY" -m pip install --user -e "$SCRIPT_DIR" --quiet --disable-pip-version-check 2>/dev/null || {
            echo "❌ Could not install into '$PLUGIN_PY'."
            echo "   Edit .claude-plugin/plugin.json and change \"command\" to: $RESOLVED_PY"
            exit 1
        }
        echo "✅ Installed into '$PLUGIN_PY' too."
    fi
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "✅ ASTra installed successfully."
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code"
echo "  2. Open any project folder — ASTra auto-indexes on first MCP call"
echo ""
echo "Diagnostics:"
echo "  Status:    $PY -m astra.cli.main status"
echo "  Dashboard: $PY -m astra.cli.main dashboard"
echo "  Debug:     $PY -m astra.mcp.server   (should hang waiting for stdin)"
echo "═══════════════════════════════════════════════════"
