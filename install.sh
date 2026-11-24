#!/bin/sh
# Installs uv if missing, then the newest TFS release wheel, then runs `tfs setup`.
set -eu

fail() { echo "$1" >&2; exit 1; }

echo "1/4 Checking uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh || fail "Could not install uv."
  PATH="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}:$PATH"
fi

echo "2/4 Finding the newest TFS release"
# releases/latest skips pre-releases, so read the list instead.
wheel=$(curl -fsSL "https://api.github.com/repos/PrabhjotSodhi/training-for-something/releases?per_page=1" 2>/dev/null \
  | grep -o '"browser_download_url": *"[^"]*\.whl"' | head -n 1 | cut -d'"' -f4 || true)
[ -n "$wheel" ] || fail "Could not find the newest TFS release."

echo "3/4 Installing TFS"
uv tool install --force "$wheel" || fail "Could not install TFS."
uv tool update-shell || fail "Could not add TFS to PATH."

echo "4/4 Starting tfs setup"
tfs="$(uv tool dir --bin)/tfs"
# When piped from curl, stdin is this script, so setup reads the keyboard from the terminal.
if ( : </dev/tty ) 2>/dev/null; then exec "$tfs" setup </dev/tty; fi
exec "$tfs" setup
