#!/bin/sh
# Takes the app through every screen and saves what each looks like: `scripts/tour.sh [dir]` (default build/tour).
# Needs the dev server (scripts/dev_server.py). For each screen, <dir> gets NN-name.png (the window) and NN-name.txt
# (what VoiceOver reads there, with frames); ALL.txt has every screen's text in order.
#
# The app asks for each accessibility dump by writing NN-name.want; this script answers with NN-name.ax from
# scripts/ax-dump.swift, run from here: macOS trusts an accessibility reader started from the shell, not one the app
# starts itself. The Mac must be unlocked with its display awake: otherwise macOS shows no windows to accessibility
# readers, and the dumps say "(no window)".
set -eu
cd "$(dirname "$0")/.."
DIR="${1:-build/tour}"
APP=build/Sammy.app/Contents/MacOS/Sammy
[ -x "$APP" ] || scripts/build-app.sh >/dev/null
DUMP=build/ax-dump
[ "$DUMP" -nt scripts/ax-dump.swift ] || swiftc -O scripts/ax-dump.swift -o "$DUMP"
rm -rf "$DIR"
mkdir -p "$DIR"

# The app is the one users get; the tour points it at the dev server for this run only (SAMMY_TOUR_SERVER).
"$APP" --tour "$DIR" --server "${SAMMY_TOUR_SERVER:-http://127.0.0.1:8000}" &
TOUR=$!
while kill -0 "$TOUR" 2>/dev/null; do
    for want in "$DIR"/*.want; do
        [ -e "$want" ] || continue
        name="${want%.want}"
        [ -e "$name.ax" ] && continue
        "$DUMP" "$(cat "$want")" > "$name.ax.tmp" && mv "$name.ax.tmp" "$name.ax"
    done
    sleep 0.2
done

for text in "$DIR"/[0-9]*.txt; do grep -v 'zoom the window' "$text"; echo; done > "$DIR/ALL.txt"
echo "$(ls "$DIR"/[0-9]*.png | wc -l | tr -d ' ') screens in $DIR"
