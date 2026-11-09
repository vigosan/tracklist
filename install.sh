#!/bin/bash
set -euo pipefail

case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
  es*) ui=es ;;
  *) ui=en ;;
esac

say() {
  if [ "$ui" = es ]; then echo "$2"; else echo "$1"; fi
}

if [ "$(uname)" != "Darwin" ]; then
  say "tracklist installs on macOS only." "tracklist solo se instala en macOS."
  exit 1
fi

load_homebrew() {
  for prefix in ${TRACKLIST_BREW_PREFIXES:-/opt/homebrew /usr/local}; do
    if [ -x "$prefix/bin/brew" ]; then
      eval "$("$prefix/bin/brew" shellenv)"
    fi
  done
}

install_homebrew() {
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
}

load_homebrew
if ! command -v brew > /dev/null; then
  say "Installing Homebrew (it may ask for your Mac password)..." \
      "Instalando Homebrew (puede pedir la contraseña de tu Mac)..."
  if { : < /dev/tty; } 2> /dev/null; then
    install_homebrew < /dev/tty
  else
    install_homebrew
  fi
  load_homebrew
fi

say "Installing yt-dlp, ffmpeg and uv..." "Instalando yt-dlp, ffmpeg y uv..."
brew install yt-dlp ffmpeg uv

say "Installing tracklist..." "Instalando tracklist..."
uv tool install --force git+https://github.com/vigosan/tracklist
uv tool update-shell

echo
say "Done. Open a new Terminal window and type: tracklist" \
    "Listo. Abre una ventana nueva de Terminal y escribe: tracklist"
