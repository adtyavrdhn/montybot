#!/bin/sh
# Run pytest on Linux with bwrap, for the tests that need it (the CPython tier, #6), from a Mac or any Docker host:
#
#   tests/linux/run.sh tests/test_cpython.py tests/e2e/test_files.py
#
# The checkout is copied into the container (without .venv), so nothing is written back. Postgres and Full Monty are
# reached on the Docker host's network: MONTYBOT_TEST_POSTGRES and MONTYBOT_TEST_MONTY_URL pass through as they are,
# so they must name ports published on the Docker host (on colima, the VM). bwrap needs new namespaces and a fresh
# /proc, which Docker's default profiles forbid, hence the three `security-opt`s, as in deploy/compose.yaml.
#
# For the Servo tests, set MONTYBOT_SERVO_BINARY to servoshell from the Linux release for the Docker host's
# architecture (unpacked on this machine); its folder is mounted read-only at the same path. The same goes for
# MONTYBOT_LIGHTPANDA_BINARY and the Lightpanda Linux release binary.
set -eu
cd "$(dirname "$0")/../.."
docker build --load --quiet --tag montybot-linux-tests tests/linux >/dev/null  # --load: a buildx container driver
mounts=''
if [ -n "${MONTYBOT_SERVO_BINARY:-}" ]; then
    servo_dir=$(dirname "$MONTYBOT_SERVO_BINARY")
    mounts="--volume $servo_dir:$servo_dir:ro --env MONTYBOT_SERVO_BINARY"
fi
if [ -n "${MONTYBOT_LIGHTPANDA_BINARY:-}" ]; then
    lightpanda_dir=$(dirname "$MONTYBOT_LIGHTPANDA_BINARY")
    mounts="$mounts --volume $lightpanda_dir:$lightpanda_dir:ro --env MONTYBOT_LIGHTPANDA_BINARY"
fi
# shellcheck disable=SC2086  # $mounts is several words on purpose
exec docker run --rm --network host $mounts \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
    --volume "$PWD:/checkout:ro" --volume montybot-linux-tests-uv:/home/tester/.cache/uv \
    --env MONTYBOT_TEST_POSTGRES --env MONTYBOT_TEST_MONTY_URL \
    montybot-linux-tests sh -c '
        tar -C /checkout --warning=no-file-changed --exclude=./.venv --exclude=./data --exclude=__pycache__ -cf - . | tar -xf -
        uv sync --frozen --quiet && exec uv run pytest -p no:cacheprovider "$@"' sh "$@"
