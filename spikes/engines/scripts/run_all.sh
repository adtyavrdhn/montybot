#!/bin/bash
# Runs the `runs` and `checks` phases for every Playwright engine and Lightpanda, one engine at a time.
# Needs scripts/server.py listening on 127.0.0.1:8766.
cd "$(dirname "$0")/.."
export PLAYWRIGHT_BROWSERS_PATH=$PWD/bin/ms-playwright
for e in ${ENGINES:-chromium-headless-shell chromium-new-headless firefox webkit lightpanda}; do
  for phase in runs checks; do
    uv run --no-project --with playwright==1.63.0 --with psutil python scripts/measure.py --engine $e --phase $phase \
      > logs/$phase-$e.log 2>&1
    echo "$e $phase exit $?"
  done
done
