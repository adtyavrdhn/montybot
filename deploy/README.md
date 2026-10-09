# Deploying Sammy on one server

Everything runs on one Linux machine with Docker Compose (`deploy/compose.yaml`):

| Service | What it is |
|---|---|
| `caddy` | HTTPS for `$DOMAIN`, with a Let's Encrypt certificate (Caddy's own CA for `localhost`), and a basic-auth login in front of everything but `/healthz` |
| `app` | the web app, the DBOS workflows and the browsers: one headed Chromium per run, each on its own Xvfb screen, inside bwrap, driven over our own CDP pipe (`sammy.engines:chromium_cdp_server`; `BROWSER_BACKEND=sammy.engines:chromium_server` in `.env` rolls back to Playwright) |
| `postgres` | our tables and DBOS's |
| `backup` | `pg_dump` every night at `BACKUP_AT` (03:00 UTC) into `/opt/sammy/backups`, keeping `BACKUP_KEEP_DAYS` (14) days |
| `browser-egress` | public-only SOCKS proxy for jailed browsers, on a separate bridge without Postgres or app credentials; the app mounts only its socket volume |

The app allows at most `BROWSER_MAX_OPEN` live browsers (default 8 on the server: about 430 MB and 1.3% of a core
each when idle, per `sammy/browser/CHROMIUM.md`). A user's runs share one browser, each in its own tab, so they
run side by side and count once. A user's browser stays open between their runs (`BROWSER_KEEP_OPEN`) until it has
been idle for `BROWSER_IDLE_TIMEOUT_SECONDS` (a day). When full it first closes the browser parked longest, then
saves and closes the least-recently-used browser that is neither busy nor in a hand-off; that run reopens from saved
state on its next call.
If all browsers are busy, a new call fails quickly rather than starting an unbounded number of Chromium processes.
Tune this against measured VM memory: it is a concurrency guard, **not** a per-browser memory limit. Do not give
bwrap writable cgroups or Docker privileged mode to impose one.

Secrets live only in `/opt/sammy/.env` on the server, which `bootstrap.sh` writes once. Nothing secret is in the
images or the repository.

## A new server

Any Ubuntu 24.04 machine with a public IP (GCP, Hetzner, ...), amd64 or arm64, with ports 80 and 443 open and a user
with sudo you can reach over SSH.

1. **DNS** (optional): point an A record for your name at the server. Without one, the server is
   `https://A-B-C-D.sslip.io` for its IP `A.B.C.D`.
2. **Deploy** from a checkout of this repository:

   ```bash
   DOMAIN=bot.example.com deploy/deploy.sh root@SERVER   # DOMAIN only on the first deploy; later ones read .env
   ```

   `deploy.sh` copies the current commit over SSH and runs `bootstrap.sh`, which does each step only once:
   installs Docker, installs the AppArmor profile for bwrap (below), and writes `/opt/sammy/.env` with new secrets
   (`POSTGRES_PASSWORD`, `SESSION_SECRET`, `ENCRYPTION_KEY`, the basic-auth login). Then it builds the app image on
   the server, runs `docker compose up -d --wait`, and checks `https://$DOMAIN/healthz`. Caddy gets the certificate
   on the first request.
3. **The model:** with the default `MODEL=claude-code:...`, sign the server in once, as `deploy.sh` prints. For an API
   model, put `MODEL=anthropic:claude-sonnet-4-5` and `ANTHROPIC_API_KEY=...` in `/opt/sammy/.env` and deploy again.
4. **The login** is `BASIC_AUTH_USER` and `BASIC_AUTH_PASSWORD` in `/opt/sammy/.env`.

By hand on the server, from `/opt/sammy/src/deploy`, `compose` meaning
`sudo docker compose --env-file /opt/sammy/.env`:

```bash
compose ps                         # what runs
compose logs -f app                # the app's log
compose restart app                # unfinished runs carry on after the restart (DBOS, EXECUTOR_ID=vm-1)
compose exec backup backup now     # a backup now, into /opt/sammy/backups
```

**The VM's own overrides:** `/opt/sammy/compose.local.yaml`, when it exists, goes on top of `compose.yaml` at every
deploy, so a change only this server should have (such as trying another browser engine) survives deploys. It lives
outside `src/`, which each deploy replaces. By hand, give compose both files:
`COMPOSE_FILE=compose.yaml:/opt/sammy/compose.local.yaml` before `compose` (and `--preserve-env=COMPOSE_FILE` for
`sudo`), or `-f compose.yaml -f /opt/sammy/compose.local.yaml`. A plain `compose up` without it drops the overrides
until the next deploy. Delete the file and deploy again to go back.

**Restore a backup** into a new database, check it, then point the app at it or rename it:

```bash
compose exec backup createdb sammy_restored
compose exec backup pg_restore --no-owner -d sammy_restored /backups/sammy-YYYYMMDDTHHMMSSZ.dump
```

Copy `/opt/sammy/backups` off the server (rsync, a bucket) for backups that survive losing the disk; that is not
automated yet.

## From montybot to Sammy

The app was called montybot. On a VM that still has `/opt/montybot`, the first deploy after the rename stops the old
`montybot` Compose project and moves `/opt/montybot` to `/opt/sammy`, keeping `.env` (secrets, domain, basic-auth
login) and backups. It copies the Claude Code sign-in and Caddy's certificates into the new volumes and tags the Full
Monty images as `sammy-monty-*`. The database, workspaces and parked Monty sessions start empty. The old volumes stay
until removed by hand: `sudo docker volume rm $(sudo docker volume ls -q -f name=montybot_)`.

## Hosted Monty sandboxes

The app takes its Monty sessions from the hosted service instead of running monty-server and monty-worker on the
VM. Set the key as the `MONTY_EXECUTION_KEY` Actions secret (copied into `/opt/sammy/.env` on deploy, like the
other secrets). Whenever `.env` has a key, `deploy.sh` points the app at
`wss://monty-sdk-test-hqjw53u6ua-uk.a.run.app/monty-ws/` (or `MONTY_URL` from `.env`) and does not start our own
Full Monty, even with `MONTY_PRIVATE_COMMIT` set; its images and session volume are left in place.

The key goes in each connection's `Authorization: Bearer` header. Sessions are parked in the service's store
between `run_code` calls, as with Full Monty. Each `run_code` call costs about a second there (opening the sandbox,
loading and saving the session), against milliseconds locally. Remove `MONTY_EXECUTION_KEY` from `.env` to go back to
our own Full Monty (with `MONTY_PRIVATE_COMMIT`) or local Monty. Runs that are waiting when the backend changes
lose their code variables once (the agent is told to set them again).

## Full Monty: manual private release

This is an operator plan; installing images and enabling them are separate steps. The Dockerfiles and options
were checked read-only against local `monty-private` commit `db45c98f1e42d6ade8ff7ad3f8635d70a2207261`;
review them again when selecting a different source revision. No private registry, registry
token, GitHub source token, or automated private checkout is needed. Normal app CD only builds the app; when
`MONTY_PRIVATE_COMMIT` is configured it checks the installed private image revision labels, enables the
`full-monty` profile and sets `MONTY_URL=ws://monty-server:8000`. With that setting absent, local Monty remains
the default. The production Compose file has no private build contexts and uses `pull_policy: never`.

### 1. Review and pin the authorized source

From a machine with an existing authorized `monty-private` checkout (not a GitHub runner):

```bash
cd /path/to/sammy
PRIVATE="$HOME/pydantic_repos/monty-private"
COMMIT=$(git -C "$PRIVATE" rev-parse --verify 'HEAD^{commit}')
git -C "$PRIVATE" show --stat "$COMMIT"  # review the intended committed revision
# Only when approved to build on the existing VM:
SSH_KEY="$HOME/.ssh/vm" sh deploy/build-monty.sh USER@SERVER "$PRIVATE" "$COMMIT"
```

The script makes `git archive` of the full commit, sends it encrypted over SSH with normal host-key verification,
and builds the workspace-root server and worker Dockerfiles on the VM's native Linux amd64/arm64 daemon.
`MONTY_COMMIT` stamps the revision into the images (and server binary); `CARGO_PROFILE=release` is explicit.
Images stay in that daemon as `sammy-monty-server:<full-commit>` and `sammy-monty-worker:<full-commit>`.
There is no push, image export, registry login, stack restart or automatic activation. Existing tags are not
overwritten. A partial build may leave only the server tag: inspect it before removing that unused tag and
retrying. Do not remove tags used by running containers or retained for rollback.

The first build compiles Rust workspace crates and the worker's matching `monty-runtime`, downloads public
base images and Cargo dependencies, and can take many minutes with substantial CPU, memory and disk usage.
There is no measured VM capacity estimate yet; schedule it outside peak traffic and check free RAM/disk first.
Later builds have coarse Docker layer caching: a source change can invalidate compilation. Source pinning is
not fully reproducible pinning of toolchains/base-image digests. The native daemon must be on this VM (not a
remote Docker context); the script checks its OS/architecture but not physical locality.

### 2. Configure and activate, only after VM verification

On the VM, inspect both local image revision labels, non-root users and architecture without printing `.env`:

```bash
COMMIT=<full-reviewed-source-commit>
for service in server worker; do
  sudo docker image inspect --format '{{.Architecture}} {{.Config.User}} {{ index .Config.Labels "org.opencontainers.image.revision" }}' "sammy-monty-$service:$COMMIT"
done
# Edit privately; add MONTY_PRIVATE_COMMIT=<the same full commit> (do not duplicate the key).
${EDITOR:-vi} /opt/sammy/.env
```

The next normal app deploy enables Full Monty using those installed tags, never rebuilding them. To activate
without waiting for CD, on the VM with the updated sammy source installed:

```bash
cd /opt/sammy/src/deploy
set -a; . /opt/sammy/.env; set +a
export COMPOSE_PROFILES=full-monty MONTY_URL=ws://monty-server:8000
sudo --preserve-env=COMPOSE_PROFILES,MONTY_URL docker compose --env-file /opt/sammy/.env up -d --no-build --wait
sudo --preserve-env=COMPOSE_PROFILES,MONTY_URL docker compose --env-file /opt/sammy/.env ps
```

`monty-server` shares `edge` with the app and has no published port. It relays to the worker on the separate
internal `monty` network. The worker is **only** on `monty`, with no app/db/Internet access or published port.
The app's browser egress proxy still blocks private addresses. `edge` also contains Caddy; this is shared-network
isolation, not per-client authentication of the relay. Neither private service has a Compose healthcheck yet:
`up --wait` confirms containers are running, not websocket readiness. Verify service `/health` responses and a
real app `run_code` call, network isolation, and a paused session surviving a relay restart before declaring ready.

Sessions use `MONTY_SERVER_DEFAULT_PERSISTENCE=stored` and `file:///var/lib/monty-server` on
`sammy_monty-sessions`. The current private Dockerfile creates that directory owned by `65532:65532`, so a
**fresh named volume** inherits writable ownership for the nonroot server. Inspect existing volume ownership
before reusing it; old root-owned volumes require an explicit operator repair while the server is stopped.
Do not use `down -v`. The worker limits are 32 sessions and 256 MiB per session, not a total container/VM memory
limit. The relay's current defaults also cap one caller at 10 concurrent sessions (the app is one caller), limit each
connection to one hour of total age (even when active), and park stored sessions after 60 seconds. These defaults still apply; persistence
is not unlimited retention or a promise that every active session survives a crash. Long-running workflows
must reconnect/resume rather than assume activity extends that connection deadline; verify that path before
activation. Compose allows 60 seconds to stop each private service, exceeding its default 30-second drain
window; abrupt VM loss still bypasses graceful shutdown. CPU, wall time and aggregate
capacity still need measurement and tuning. Session data is not part of the
Postgres backup and has no automated backup/retention here.

### Private data and rollback

The archive contains all tracked committed source, not `.git` or uncommitted files. Review that no credentials
are committed; `git archive` honors export-ignore and does not include submodule contents. Local temporary tar
and remote tar/extraction directory are removed on normal exit and trapped failures. A dropped SSH connection,
failed transfer or killed process can leave `~/monty-private-<commit>.tar` or `~/monty-private-build.*`: remove
those specific paths after checking no build is active. Source is briefly plaintext on both disks; deleting it
is not secure erasure and snapshots/backups may retain it.

**Docker build cache/intermediate layers retain private source** because the builder uses `COPY . .`.
Runtime images/binaries, daemon storage, build logs and session volumes are private artifacts too. Restrict VM,
Docker socket and backup access; do not export caches, push images or publish logs. `sudo docker builder prune`
is an optional operator cleanup after builds (it affects shared cache and makes future builds expensive); also
inspect other builders/caches and VM snapshots under your storage policy. It does not remove installed runtime
images. Keep previous runtime tags for rollback, but do not assume they contain no sensitive information.

For rollback, privately restore the previous `MONTY_PRIVATE_COMMIT` in `.env` and activate with the commands
above, or run normal app deployment. Verify both previous tags still exist first. Stored-session compatibility
across versions is not guaranteed: drain active runs and back up the session volume before an upgrade; use a
compatible volume backup if needed. To disable Full Monty, remove that setting, unset `COMPOSE_PROFILES` and
`MONTY_URL`, and run normal `deploy.sh`; it reconfigures the app for local Monty and removes profile-service
orphans. Parked remote sessions will not automatically migrate to local Monty. Never delete the session volume
as part of rollback.

## The browser jail

Each Chrome runs inside bwrap with its own user, PID, IPC and network namespaces, its own profile folder and `/tmp`,
an empty environment, and only its own X screen (`sammy/browser/chromium_linux.py`, `CHROMIUM.md`). Chrome's own
sandbox stays on inside. Docker's defaults refuse all of that, so the `app` service needs these settings, and no more:

| Setting | Why |
|---|---|
| `seccomp=./seccomp.json` | Docker's default profile ([moby/profiles](https://github.com/moby/profiles/blob/6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef/seccomp/default.json) at `6fe7deb`) plus one rule at the end allowing `clone`, `unshare`, `setns`, `mount`, `umount2` and `pivot_root`, which the default allows only with `CAP_SYS_ADMIN`. bwrap makes its namespaces with these, without the capability. |
| `apparmor=unconfined` | Docker's `docker-default` AppArmor profile refuses every mount, so bwrap stops at `Failed to make / slave: Permission denied`. |
| `systempaths=unconfined` | Docker hides paths under `/proc`; while they are covered, the kernel refuses a new `/proc` (`Can't mount proc on /newroot/proc: Operation not permitted`). |
| `cap_drop: [ALL]`, `cap_add: [SYS_CHROOT]` | The app runs as a normal user and needs no capabilities. Chrome's sandbox calls `chroot` inside its own user namespace, which needs `SYS_CHROOT` left in the container's bounding set. |
| `no-new-privileges` | Works with all of the above; kept. |

**Ubuntu 24.04 (and 23.10 on).** The kernel setting `kernel.apparmor_restrict_unprivileged_userns=1` lets a program
make user namespaces only if its AppArmor profile says `userns`. An unconfined container is no exception: bwrap
fails with `loopback: Failed RTM_NEWADDR: Operation not permitted`. `bootstrap.sh` installs `apparmor-bwrap` as
`/etc/apparmor.d/bwrap`, which allows it for `/usr/bin/bwrap` only. AppArmor matches the path inside the container
too, so the app's bwrap gets it and nothing else in the container does. This is the profile Claude Code documents
for its own bwrap sandbox. The blunt alternative, `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, turns the
restriction off for every program on the host; earlier deploys did that, and `bootstrap.sh` turns it back on.

Hosts without AppArmor (Debian, most other distributions) need only the container settings. On such a host, Chrome
started without bwrap (the headless test engine) cannot start its own sandbox; the server never does that.

**Network.** bwrap's `--unshare-net` leaves Chrome only loopback. Its connections leave through the app's egress
proxy (`sammy/browser/egress.py`): SOCKS5 over a Unix socket, which resolves each host name itself and refuses
any private, loopback or link-local answer. So pages cannot reach Postgres, the app itself, the Docker host, other
servers on the provider's private network or the cloud metadata address (`169.254.169.254`), by name or by number,
redirects and subresources included. Only TCP leaves, so there is no QUIC and no WebRTC UDP.

What it does not do: the app process itself (not the browser) can reach anything the container can, and Postgres is
on the same Docker network as the app, as it must be. Caddy and the app share one network, the app, Postgres and the
backup another.

## Checking a deploy

`tests/test_deploy.py` builds and runs this compose file on the local Docker (with `compose.smoke.yaml` on top: the
scripted model, and HTTPS on `localhost:8443`), then checks over HTTPS what a user would see:

```bash
SAMMY_TEST_DEPLOY=1 uv run pytest tests/test_deploy.py -v
```

- the web app behind the login, over TLS;
- Chromium in bwrap with Chrome's own sandbox on, opening example.com and refused `postgres`, `10.0.0.1`,
  `169.254.169.254` and the app's own port;
- a scripted run that reads example.com through the app;
- a run waiting on a question survives `docker compose restart app` and finishes when the question is answered;
- `backup now` writes a dump that restores into a new database with the same users.

It ran on colima's Ubuntu 24.04 arm64 VM with the AppArmor profile installed.

## Not done

- Full Monty private builds, VM capacity, runtime readiness, network isolation and persisted-session restart
  behavior still require the operator verification above; no VM build or activation was performed for this change.
- Backups stay on the server's disk, and cover Postgres only: the users' files (the `workspaces` volume) and the
  Claude Code sign-in (`claude-code`) are not in them.
- One app process (`EXECUTOR_ID=vm-1`); more would each need their own id.


## Jev and Logfire keys

Add `TYPESAFE_API_KEY`, `LOGFIRE_TOKEN` and `COMPOSIO_API_KEY` (one-click apps for Integrations) in the repository's
Actions secrets. Only the trusted main deploy
receives them. `deploy/update-secrets.py` sends nonempty values over SSH stdin and atomically updates the VM's
owner-only `.env`; unset repo secrets do not erase existing VM values. Tokens must use letters, digits or
`_.:/+=-`; malformed updates fail rather than interpolating shell syntax. No secrets enter image layers.

Each deploy bakes its commit into the image; Logfire shows it as `service.version`, so traces can be compared across
deploys. `ENVIRONMENT` (default `production`) and `LOGFIRE_INCLUDE_CONTENT` (default `true`: messages, the agent's
code and page snapshots are exported; see the README's Observability section) can be set in the VM's `.env`.

Voice (dictation for browsers without speech recognition, and replies in the provider's voice) is off until
`VOICE_PROVIDER` (`openai` or `elevenlabs`) and `VOICE_API_KEY` are set in the VM's `.env`; `VOICE_NAME` picks the
voice. See the README's Voice section.

Jev advice is disabled in production. Setting `TYPESAFE_API_KEY` does not add tools or make Jev requests.
The experimental helpers and their tests remain in the repository for later evaluation. Existing credentials
and Jev settings can remain stored, but normal agent runs ignore them.
