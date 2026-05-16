#!/bin/sh
set -e

REPO_URL="https://github.com/tanzcoding/AlphaSolve/archive/refs/heads/main.zip"
REPO_DIR="AlphaSolve-main"

echo "========================================"
echo "  AlphaSolve Installer"
echo "========================================"
echo

# ── 1. Install uv ─────────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    echo "[1/3] uv already installed, skipping..."
else
    echo "[1/3] Installing uv..."
    echo
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "[ERROR] Neither curl nor wget found."
        exit 1
    fi
    if [ -f "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    elif [ -f "$HOME/.cargo/bin/uv" ]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
fi

# ── 2. Download AlphaSolve ────────────────────────────────
echo
echo "[2/3] Downloading AlphaSolve..."
echo
rm -rf "$REPO_DIR"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$REPO_URL" -o alpha.zip
else
    wget -q "$REPO_URL" -O alpha.zip
fi
unzip -qo alpha.zip
rm alpha.zip

# ── 3. Install ────────────────────────────────────────────
echo
echo "[3/3] Installing AlphaSolve..."
echo
cd "$REPO_DIR"
uv tool install .
cd ..
rm -rf "$REPO_DIR"

echo
echo "========================================"
echo "  Installation complete!"
echo "========================================"
echo
echo "To get started:"
echo "  1. Create a folder anywhere, put a problem.md in it"
echo "  2. Open a terminal in that folder"
echo "  3. Run: alphasolve"
echo
