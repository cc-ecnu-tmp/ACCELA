#!/bin/sh
set -eu

mode=${1:-native}
case "$mode" in
  jar|native) ;;
  *) echo "usage: $0 [jar|native]" >&2; exit 2 ;;
esac

command -v javac >/dev/null 2>&1 || { echo "javac 21 is required" >&2; exit 2; }
command -v jar >/dev/null 2>&1 || { echo "jar is required" >&2; exit 2; }
java_version=$(javac -version 2>&1)
case "$java_version" in
  "javac 21"*) ;;
  *) echo "JDK 21 is required, found: $java_version" >&2; exit 2 ;;
esac

output=build/offline
classes=$output/classes
rm -rf "$output"
mkdir -p "$classes"
find src/main/java -type f -name '*.java' -print | LC_ALL=C sort > "$output/sources.txt"
test -s "$output/sources.txt" || { echo "no compiler sources found" >&2; exit 2; }
javac --release 21 -encoding UTF-8 -d "$classes" @"$output/sources.txt"
jar --create --file "$output/accela.jar" --main-class Compiler -C "$classes" .

if [ "$mode" = native ]; then
  command -v native-image >/dev/null 2>&1 || {
    echo "native-image is required for a self-contained compiler" >&2
    exit 2
  }
  native-image --no-fallback -cp "$output/accela.jar" Compiler -o "$output/compiler"
fi

echo "offline compiler build completed: $output"
