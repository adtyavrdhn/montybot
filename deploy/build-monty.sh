#!/bin/sh
# Manual private image release only. Does not configure or restart Compose.
# Usage: sh deploy/build-monty.sh USER@HOST /authorized/monty-private COMMIT
set -eu

target=${1:?usage: build-monty.sh USER@HOST CHECKOUT COMMIT}
source_dir=${2:?authorized local checkout required}
ref=${3:?committed source revision required}
commit=$(git -C "$source_dir" rev-parse --verify "$ref^{commit}")
case "$commit" in *[!0-9a-f]*) exit 1 ;; esac
[ "${#commit}" -eq 40 ] || { echo "Expected SHA-1 source commit" >&2; exit 1; }
ssh_opts="-o BatchMode=yes -o ConnectTimeout=20 ${SSH_KEY:+-i $SSH_KEY}"
umask 077
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT HUP INT TERM
# Archive only committed tracked files; never send .git, local credentials or uncommitted changes.
git -C "$source_dir" archive --format=tar "$commit" > "$archive"
# Remote command arguments below contain only a validated hexadecimal commit.
# shellcheck disable=SC2086,SC2029
ssh $ssh_opts "$target" "umask 077; cat > ~/monty-private-$commit.tar" < "$archive"
# shellcheck disable=SC2086
ssh $ssh_opts "$target" COMMIT="$commit" sh -s <<'REMOTE'
set -eu
umask 077
archive=$HOME/monty-private-$COMMIT.tar
source_dir=$(mktemp -d "$HOME/monty-private-build.XXXXXX")
trap 'rm -rf "$source_dir"; rm -f "$archive"' EXIT HUP INT TERM
[ "$(uname -s)" = Linux ] || { echo "Build on the Linux VM" >&2; exit 1; }
# No --platform/emulation: use the VM daemon's native Linux architecture.
case "$(sudo docker info --format '{{.OSType}}/{{.Architecture}}')" in
    linux/x86_64|linux/aarch64|linux/amd64|linux/arm64) ;;
    *) echo "Expected native Linux amd64/arm64 Docker daemon" >&2; exit 1 ;;
esac
for service in server worker; do
    if sudo docker image inspect "montybot-monty-$service:$COMMIT" >/dev/null 2>&1; then
        echo "Tag already installed: montybot-monty-$service:$COMMIT; refusing to overwrite" >&2
        exit 1
    fi
done
tar -xf "$archive" -C "$source_dir"
cd "$source_dir"
for service in server worker; do
    sudo docker build --build-arg MONTY_COMMIT="$COMMIT" --build-arg CARGO_PROFILE=release \
        -f "crates/monty-$service/Dockerfile" -t "montybot-monty-$service:$COMMIT" .
done
echo "Installed both local private images for $COMMIT. Compose has not been changed or restarted."
REMOTE
echo "Next: set MONTY_PRIVATE_COMMIT=$commit in /opt/montybot/.env when ready to enable Full Monty."
