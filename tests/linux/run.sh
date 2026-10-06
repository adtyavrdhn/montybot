#!/bin/sh
# Run pytest on Linux with bwrap, for the tests that need it (the CPython tier, #6), from a Mac or any Docker host:
#
#   tests/linux/run.sh tests/test_cpython.py tests/e2e/test_files.py
#
# The checkout is copied into the container (without .venv), so nothing is written back. Postgres and Full Monty are
# reached on the Docker host's network: MONTYBOT_TEST_POSTGRES and MONTYBOT_TEST_MONTY_URL pass through as they are,
# so they must name ports published on the Docker host (on colima, the VM). bwrap needs new namespaces and a fresh
# /proc, which Docker's default profiles forbid, hence the three `security-opt`s, as in deploy/compose.yaml.
set -eu
cd "$(dirname "$0")/../.."
docker build --quiet --tag montybot-linux-tests tests/linux >/dev/null
exec docker run --rm --network host \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
    --volume "$PWD:/checkout:ro" --volume montybot-linux-tests-uv:/home/tester/.cache/uv \
    --env MONTYBOT_TEST_POSTGRES --env MONTYBOT_TEST_MONTY_URL \
    montybot-linux-tests sh -c '
        tar -C /checkout --warning=no-file-changed --exclude=./.venv --exclude=./data --exclude=__pycache__ -cf - . | tar -xf -
        uv sync --frozen --quiet && exec uv run pytest -p no:cacheprovider "$@"' sh "$@"
