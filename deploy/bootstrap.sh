#!/bin/sh
# Prepare an Ubuntu 24.04 VM for Sammy. Safe to run on every deploy: each step does nothing once done.
# Runs on the VM as a user with sudo; deploy/deploy.sh calls it.
set -eu

ROOT=/opt/sammy
ENV_FILE=$ROOT/.env

if ! command -v docker >/dev/null 2>&1; then
    echo "Installing Docker"
    curl -fsSL https://get.docker.com | sudo sh
fi

# bwrap needs unprivileged user namespaces. Ubuntu 24.04 lets a program make them only if its AppArmor profile says
# `userns` (kernel.apparmor_restrict_unprivileged_userns=1). deploy/apparmor-bwrap allows it for /usr/bin/bwrap and
# nothing else, the bwrap inside the app container included, since that container runs without a profile of its own.
PROFILE=/etc/apparmor.d/bwrap
if [ -d /etc/apparmor.d ] && ! cmp -s "$(dirname "$0")/apparmor-bwrap" "$PROFILE"; then
    echo "Allowing bwrap to make user namespaces"
    sudo cp "$(dirname "$0")/apparmor-bwrap" "$PROFILE"
    sudo apparmor_parser -r "$PROFILE"
fi
# Earlier deploys turned the restriction off for the whole host instead; turn it back on.
if [ -f /etc/sysctl.d/60-montybot-userns.conf ]; then
    sudo rm /etc/sysctl.d/60-montybot-userns.conf
    sudo sysctl -q kernel.apparmor_restrict_unprivileged_userns=1
fi

# Once, on a VM deployed before the rename from montybot: stop the old stack and take over its settings (secrets,
# domain, login), Claude Code sign-in, TLS certificates and Full Monty images. Its database and files are not carried
# over: the app starts empty. The old volumes are left in place; `docker volume ls -q -f name=montybot_` lists them.
if [ -d /opt/montybot ] && [ ! -d "$ROOT" ]; then
    echo "Moving /opt/montybot to $ROOT"
    sudo docker compose -p montybot down --remove-orphans
    for volume in claude-code caddy-data; do
        if sudo docker volume inspect "montybot_$volume" >/dev/null 2>&1; then
            sudo docker volume create --label com.docker.compose.project=sammy \
                --label com.docker.compose.volume="$volume" "sammy_$volume" >/dev/null
            sudo docker run --rm -v "montybot_$volume:/from:ro" -v "sammy_$volume:/to" caddy:2 cp -a /from/. /to/
        fi
    done
    sudo docker image ls --format '{{.Repository}}:{{.Tag}}' | grep '^montybot-monty-' | while read -r image; do
        sudo docker image tag "$image" "sammy-${image#montybot-}"
    done
    sudo mv /opt/montybot "$ROOT"
    # Settings that name the old package or path, such as BROWSER_BACKEND=montybot.engines:...
    for file in "$ENV_FILE" "$ROOT/compose.local.yaml"; do
        [ ! -f "$file" ] || sed -i 's#/opt/montybot#/opt/sammy#g; s#montybot\.#sammy.#g' "$file"
    done
fi

sudo mkdir -p "$ROOT"
sudo chown "$(id -u):$(id -g)" "$ROOT"

# Secrets are made here, once, and never leave the VM.
if [ ! -f "$ENV_FILE" ]; then
    echo "Writing $ENV_FILE"
    if [ -z "${DOMAIN:-}" ]; then
        # The public IP from GCP's metadata server, or elsewhere from a public service; sslip.io names it.
        metadata=http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0
        ip=$(curl -fsS -H 'Metadata-Flavor: Google' "$metadata/external-ip" || curl -fsS https://api.ipify.org)
        DOMAIN=$(echo "$ip" | tr . -).sslip.io
    fi
    password=$(openssl rand -hex 16)
    hash=$(sudo docker run --rm caddy:2 caddy hash-password --plaintext "$password")
    umask 077
    cat >"$ENV_FILE" <<EOF
DOMAIN=$DOMAIN
POSTGRES_PASSWORD=$(openssl rand -hex 24)
SESSION_SECRET=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
BASIC_AUTH_USER=sammy
BASIC_AUTH_PASSWORD=$password
BASIC_AUTH_HASH='$hash'
MODEL=claude-code:claude-opus-5-5
EOF
fi
