#!/bin/sh
# Deploy the current commit to a VM: deploy/deploy.sh USER@HOST (deploy/README.md has the whole procedure)
# Used by .github/workflows/ci-cd.yml, and works from a laptop too. SSH_KEY picks the key (default: ssh's own).
# Ships `git archive HEAD` (the repo is private, so the VM has no clone), prepares the VM, then rebuilds and restarts.
set -eu

target=${1:?usage: deploy/deploy.sh USER@HOST}
ssh_opts="-o BatchMode=yes -o ConnectTimeout=20 ${SSH_KEY:+-i $SSH_KEY}"
commit=$(git rev-parse --short HEAD)

echo "Deploying $commit to $target"
# shellcheck disable=SC2086
git archive --format=tar HEAD | ssh $ssh_opts "$target" 'rm -rf ~/sammy-release && mkdir ~/sammy-release && tar -x -C ~/sammy-release'

python3 deploy/update-secrets.py "$target"

# shellcheck disable=SC2086
ssh $ssh_opts "$target" COMMIT="$commit" DOMAIN="${DOMAIN:-}" sh -s <<'REMOTE'
set -eu
DOMAIN=$DOMAIN sh ~/sammy-release/deploy/bootstrap.sh
rm -rf /opt/sammy/src && mv ~/sammy-release /opt/sammy/src
echo "$COMMIT" > /opt/sammy/src/COMMIT
cd /opt/sammy/src/deploy
# VM-only overrides, such as a browser engine being tried out, live outside src/ so a deploy keeps them (README).
export COMPOSE_FILE=compose.yaml
if [ -f /opt/sammy/compose.local.yaml ]; then
    COMPOSE_FILE=compose.yaml:/opt/sammy/compose.local.yaml
    echo "With the VM's own overrides, /opt/sammy/compose.local.yaml"
fi
compose() { sudo --preserve-env=COMPOSE_PROFILES,COMPOSE_FILE,MONTY_URL,COMMIT docker compose --env-file /opt/sammy/.env "$@"; }

. /opt/sammy/.env
# With MONTY_EXECUTION_KEY, the agent's code runs on the hosted Monty sandboxes (MONTY_URL in .env overrides the URL),
# and our own Full Monty below is not started even if MONTY_PRIVATE_COMMIT is set. Without either, local Monty.
hosted_monty_url=${MONTY_URL:-wss://monty-sdk-test-hqjw53u6ua-uk.a.run.app/monty-ws/}
export COMPOSE_PROFILES= MONTY_URL= COMMIT
if [ -n "${MONTY_EXECUTION_KEY:-}" ]; then
    export MONTY_URL="$hosted_monty_url"
elif [ -n "${MONTY_PRIVATE_COMMIT:-}" ]; then
    # Only the manual private release installs these images. Normal CD reuses them, failing closed if missing.
    case "$MONTY_PRIVATE_COMMIT" in *[!0-9a-f]*) echo "Invalid MONTY_PRIVATE_COMMIT" >&2; exit 1 ;; esac
    [ "${#MONTY_PRIVATE_COMMIT}" -eq 40 ] || { echo "Expected full source commit" >&2; exit 1; }
    for service in server worker; do
        image="sammy-monty-$service:$MONTY_PRIVATE_COMMIT"
        revision=$(sudo docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")
        [ "$revision" = "$MONTY_PRIVATE_COMMIT" ] || { echo "Source revision mismatch: $image" >&2; exit 1; }
    done
    export MONTY_PRIVATE_COMMIT COMPOSE_PROFILES=full-monty MONTY_URL=ws://monty-server:8000
fi
compose build --pull app
# MODEL, or a model of its fallback chain (MODEL_CHAINS), on Claude Code needs the server signed in. The app decides
# (`sammy uses-claude-code`), so the chain is read as the app reads it.
if ! compose run --rm --no-deps -T app sh -c '! sammy uses-claude-code || test -s /data/claude-code/auth.json'; then
    echo "The server has no Claude Code sign-in yet. Sign in once, then deploy again:"
    echo "  ssh -t $(whoami)@$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip) \\"
    echo "    'cd /opt/sammy/src/deploy && sudo docker compose --env-file /opt/sammy/.env run --rm app sammy claude-code-login'"
    exit 1
fi

compose up -d --remove-orphans --wait --wait-timeout 300
# They mount files from src/, which was just replaced; recreate them so they see the new ones.
compose up -d --no-deps --force-recreate --wait backup caddy
compose ps
curl -fsS --retry 30 --retry-delay 2 --retry-all-errors "https://$DOMAIN/healthz"
echo
echo "Deployed $COMMIT to https://$DOMAIN"
REMOTE
