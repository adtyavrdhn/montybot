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
git archive --format=tar HEAD | ssh $ssh_opts "$target" 'rm -rf ~/montybot-release && mkdir ~/montybot-release && tar -x -C ~/montybot-release'

# shellcheck disable=SC2086
ssh $ssh_opts "$target" COMMIT="$commit" DOMAIN="${DOMAIN:-}" sh -s <<'REMOTE'
set -eu
DOMAIN=$DOMAIN sh ~/montybot-release/deploy/bootstrap.sh
rm -rf /opt/montybot/src && mv ~/montybot-release /opt/montybot/src
echo "$COMMIT" > /opt/montybot/src/COMMIT
cd /opt/montybot/src/deploy
compose() { sudo docker compose --env-file /opt/montybot/.env "$@"; }

compose build --pull app
. /opt/montybot/.env
case "${MODEL:-}" in claude-code:*)
    if ! compose run --rm --no-deps -T app test -s /data/claude-code/auth.json; then
        echo "The server has no Claude Code sign-in yet. Sign in once, then deploy again:"
        echo "  ssh -t $(whoami)@$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip) \\"
        echo "    'cd /opt/montybot/src/deploy && sudo docker compose --env-file /opt/montybot/.env run --rm app montybot claude-code-login'"
        exit 1
    fi
esac

compose up -d --remove-orphans --wait --wait-timeout 300
compose ps
curl -fsS --retry 30 --retry-delay 2 --retry-all-errors "https://$DOMAIN/healthz"
echo
echo "Deployed $COMMIT to https://$DOMAIN"
REMOTE
