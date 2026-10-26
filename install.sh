#!/bin/sh
# Qubotu installer: installs uv if needed, installs Qubotu from PyPI, checks for the
# Codex CLI, and sets Qubotu to start at login on macOS.
#
#   curl -LsSf <url>/install.sh | sh
#
# Environment:
#   QUBOTU_NO_SERVICE=1   don't set up the login service
#   QUBOTU_PACKAGE=...    install from somewhere other than PyPI (a wheel path or URL)

set -eu

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

PACKAGE="${QUBOTU_PACKAGE:-qubotu}"

step "Checking for uv"
if command -v uv >/dev/null 2>&1; then
    say "Found $(uv --version)."
else
    say "Installing uv (https://docs.astral.sh/uv/)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The uv installer adds itself to the shell profile, which this shell hasn't read.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || { say "uv was installed but isn't on PATH. Open a new terminal and run this again."; exit 1; }
fi

step "Installing Qubotu"
uv tool install --upgrade "$PACKAGE"
BIN_DIR="$(uv tool dir --bin)"
QUBOTU="$BIN_DIR/qubotu"
say "Installed $("$QUBOTU" --version)."
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) uv tool update-shell >/dev/null 2>&1 || true
       say "Added $BIN_DIR to your PATH. Open a new terminal to use the qubotu command." ;;
esac

step "Checking for the Codex CLI"
if command -v codex >/dev/null 2>&1; then
    say "Found $(codex --version 2>/dev/null || echo codex)."
    CODEX_OK=1
else
    CODEX_OK=0
    say "Qubotu runs agents on the Codex CLI, which isn't installed. Install it with one of:"
    say "    brew install codex"
    say "    npm install -g @openai/codex"
    say "Then sign in with:  codex login"
fi

if [ "$(uname -s)" = "Darwin" ] && [ "${QUBOTU_NO_SERVICE:-0}" != "1" ]; then
    step "Starting Qubotu at login"
    "$QUBOTU" service install
else
    step "Done"
    say "Start Qubotu with:  qubotu serve --open"
fi

if [ "$CODEX_OK" = "0" ]; then
    say ""
    say "Install and sign in to the Codex CLI before creating agents. If Qubotu is running as a"
    say "login service, reinstall it afterwards so it can find codex:  qubotu service install"
fi
say ""
say "Open Qubotu in your browser with:  qubotu open"
