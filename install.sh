#!/bin/sh
set -e

echo "========================================"
echo "  AlphaSolve Installer"
echo "========================================"
echo

if command -v uv >/dev/null 2>&1; then
    echo "uv already installed, skipping..."
else
    echo "Step 1/2: Installing uv (Python package manager)..."
    echo
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo
        echo "[ERROR] Neither curl nor wget found. Please install one of them first."
        exit 1
    fi
    # uv installs to ~/.local/bin or ~/.cargo/bin
    if [ -f "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    elif [ -f "$HOME/.cargo/bin/uv" ]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
fi

echo
echo "Step 2/2: Installing AlphaSolve..."
echo
uv tool install -e .

echo
echo "========================================"
echo "  Installation complete!"
echo "========================================"
echo
echo "You can now open any folder, create a problem.md,"
echo "open a terminal in that folder, and run:"
echo "  alphasolve"
echo
