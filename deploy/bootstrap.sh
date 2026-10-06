#!/bin/sh
# Prepare an Ubuntu 24.04 VM for montybot. Safe to run on every deploy: each step does nothing once done.
# Runs on the VM as a user with sudo; deploy/deploy.sh calls it.
set -eu

ROOT=/opt/montybot
ENV_FILE=$ROOT/.env

if ! command -v docker >/dev/null 2>&1; then
    echo "Installing Docker"
    curl -fsSL https://get.docker.com | sudo sh
fi

# bwrap needs unprivileged user namespaces. Ubuntu 24.04 lets only AppArmor-profiled programs make them; this turns
# that restriction off for the whole host. A bwrap AppArmor profile would be narrower (#7).
SYSCTL=/etc/sysctl.d/60-montybot-userns.conf
if [ ! -f "$SYSCTL" ]; then
    echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee "$SYSCTL" >/dev/null
    sudo sysctl -q -p "$SYSCTL"
fi

sudo mkdir -p "$ROOT"
sudo chown "$(id -u):$(id -g)" "$ROOT"

# Secrets are made here, once, and never leave the VM.
if [ ! -f "$ENV_FILE" ]; then
    echo "Writing $ENV_FILE"
    metadata=http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0
    ip=$(curl -fsS -H 'Metadata-Flavor: Google' "$metadata/external-ip")
    password=$(openssl rand -hex 16)
    hash=$(sudo docker run --rm caddy:2 caddy hash-password --plaintext "$password")
    umask 077
    cat >"$ENV_FILE" <<EOF
DOMAIN=$(echo "$ip" | tr . -).sslip.io
POSTGRES_PASSWORD=$(openssl rand -hex 24)
SESSION_SECRET=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
BASIC_AUTH_USER=montybot
BASIC_AUTH_PASSWORD=$password
BASIC_AUTH_HASH='$hash'
MODEL=claude-code:claude-opus-5-5
EOF
fi
