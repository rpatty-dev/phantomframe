#!/bin/bash
# PhantomFrame installer for macOS:
#   curl -fsSL https://raw.githubusercontent.com/rpatty-dev/phantomframe/main/install.sh | bash
# Needs nothing installed first: no git, Homebrew or Python. Re-run any time to update; settings.json is kept.
set -euo pipefail

REPO="rpatty-dev/phantomframe"
DIR="${PHANTOMFRAME_DIR:-$HOME/PhantomFrame}"

say() { printf '\033[1;35m==>\033[0m %s\n' "$1"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Windows run: irm https://raw.githubusercontent.com/$REPO/main/install.ps1 | iex"

say "Downloading PhantomFrame to $DIR"
mkdir -p "$DIR"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 -C "$DIR"

# uv (a single-file Python manager) downloads its own Python 3.12, so no system Python is needed
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  UV="$DIR/.uv/uv"
  if [ ! -x "$UV" ]; then
    say "Downloading uv (Python installer)"
    case "$(uname -m)" in arm64) ARCH=aarch64 ;; *) ARCH=x86_64 ;; esac
    mkdir -p "$DIR/.uv"
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$ARCH-apple-darwin.tar.gz" \
      | tar xz --strip-components=1 -C "$DIR/.uv"
  fi
fi

say "Installing Python 3.12 and packages"
[ -x "$DIR/.venv/bin/python" ] || "$UV" venv -q --python 3.12 "$DIR/.venv"
"$UV" pip install -q --python "$DIR/.venv/bin/python" -r "$DIR/requirements.txt"

# Optional: ffmpeg finds the capture card by name. Only installed when Homebrew is already there.
if ! command -v ffmpeg >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
  say "Installing ffmpeg"
  brew install ffmpeg || say "ffmpeg install failed, continuing without it"
fi

chmod +x "$DIR/run.command"
ln -sf "$DIR/run.command" "$HOME/Desktop/PhantomFrame.command"

say "Installed. Double-click PhantomFrame on your Desktop to run it later."
say "Allow camera access for Terminal when macOS asks: the capture card counts as a camera."
[ "${PHANTOMFRAME_NO_LAUNCH:-}" = "1" ] || exec "$DIR/run.command"
