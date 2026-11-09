#!/bin/bash
set -u

here="$(cd "$(dirname "$0")" && pwd)"
failures=0

new_sandbox() {
  sandbox="$(mktemp -d)"
  log="$sandbox/log"
  : > "$log"
  stubs="$sandbox/bin"
  mkdir -p "$stubs"
  for tool in brew uv; do
    printf '#!/bin/bash\necho "%s $*" >> "%s"\n' "$tool" "$log" > "$stubs/$tool"
    chmod +x "$stubs/$tool"
  done
  printf '#!/bin/bash\necho "Darwin"\n' > "$stubs/uname"
  chmod +x "$stubs/uname"
}

run_installer() {
  PATH="$stubs:/usr/bin:/bin" HOME="$sandbox" TRACKLIST_BREW_PREFIXES="$sandbox/no-homebrew" LC_ALL="${1:-en_US.UTF-8}" \
    /bin/bash "$here/install.sh" > "$sandbox/out" 2>&1
}

check() {
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1"
    echo "  expected: $2"
    echo "  actual:   $3"
    failures=$((failures + 1))
  fi
}

new_sandbox
run_installer
check "installs the tools, tracklist and the shell path in order" \
  "brew install yt-dlp ffmpeg uv|uv tool install --force git+https://github.com/vigosan/tracklist|uv tool update-shell" \
  "$(paste -sd'|' "$log")"

new_sandbox
printf '#!/bin/bash\necho "Linux"\n' > "$stubs/uname"
run_installer
status=$?
check "refuses to run outside macOS because the tools come from Homebrew" "1 0" "$status $(wc -l < "$log" | tr -d ' ')"

new_sandbox
mv "$stubs/brew" "$sandbox/brew.after"
printf '#!/bin/bash\necho "curl $*" >> "%s"\necho "mv %s %s/brew"\n' "$log" "$sandbox/brew.after" "$stubs" > "$stubs/curl"
chmod +x "$stubs/curl"
run_installer
check "installs Homebrew first when it is missing, so a first-time user needs no prior setup" \
  "curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh" \
  "$(head -1 "$log")"

new_sandbox
run_installer es_ES.UTF-8
check "speaks Spanish when the system is in Spanish, since the audience is Spanish-speaking" \
  "1" "$(grep -c "Abre una ventana nueva" "$sandbox/out")"

new_sandbox
run_installer
check "speaks English otherwise" "1" "$(grep -c "Open a new Terminal window" "$sandbox/out")"

exit "$failures"
