#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"

find_java_home() {
  local candidate
  for candidate in \
    "${ACCELA_JAVA_HOME:-}" \
    "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home" \
    "${JAVA_HOME:-}"; do
    if [ -n "$candidate" ] && [ -x "$candidate/bin/java" ]; then
      local version
      version="$($candidate/bin/java -version 2>&1 | sed -n '1s/.*version "\([0-9]*\).*/\1/p')"
      if [ "$version" -ge 17 ] 2>/dev/null && [ "$version" -le 24 ] 2>/dev/null; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

java_home="$(find_java_home || true)"
if [ -z "$java_home" ]; then
  echo "ACCELA requires JDK 17-24 (JDK 21 is recommended)." >&2
  echo "On macOS: brew install openjdk@21" >&2
  exit 1
fi

export JAVA_HOME="$java_home"
export PATH="$JAVA_HOME/bin:$PATH"

cd "$root"
bash ./gradlew clean test --no-daemon

mkdir -p build/src
launcher=build/src/main
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf 'exec %q -XX:-UsePerfData -cp %q Compiler "$@"\n' \
    "$JAVA_HOME/bin/java" "$root/build/classes/java/main"
} > "$launcher"
chmod +x "$launcher"

echo "Built ACCELA with $($JAVA_HOME/bin/java -version 2>&1 | head -n1)."
echo "Compiler launcher: $root/$launcher"
