# Deploying monty-bot on one server

Everything runs on one Linux machine with Docker Compose (`deploy/compose.yaml`):

| Service | What it is |
|---|---|
| `caddy` | HTTPS for `$DOMAIN`, with a Let's Encrypt certificate (Caddy's own CA for `localhost`), and a basic-auth login in front of everything but `/healthz` |
| `app` | the web app, the DBOS workflows and the browsers: one headed Chromium per run, each on its own Xvfb screen, inside bwrap (`montybot.engines:chromium_server`) |
| `postgres` | our tables and DBOS's |
| `backup` | `pg_dump` every night at `BACKUP_AT` (03:00 UTC) into `/opt/montybot/backups`, keeping `BACKUP_KEEP_DAYS` (14) days |

Secrets live only in `/opt/montybot/.env` on the server, which `bootstrap.sh` writes once. Nothing secret is in the
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
   installs Docker, installs the AppArmor profile for bwrap (below), and writes `/opt/montybot/.env` with new secrets
   (`POSTGRES_PASSWORD`, `SESSION_SECRET`, `ENCRYPTION_KEY`, the basic-auth login). Then it builds the app image on
   the server, runs `docker compose up -d --wait`, and checks `https://$DOMAIN/healthz`. Caddy gets the certificate
   on the first request.
3. **The model:** with the default `MODEL=claude-code:...`, sign the server in once, as `deploy.sh` prints. For an API
   model, put `MODEL=anthropic:claude-sonnet-4-5` and `ANTHROPIC_API_KEY=...` in `/opt/montybot/.env` and deploy again.
4. **The login** is `BASIC_AUTH_USER` and `BASIC_AUTH_PASSWORD` in `/opt/montybot/.env`.

By hand on the server, from `/opt/montybot/src/deploy`, `compose` meaning
`sudo docker compose --env-file /opt/montybot/.env`:

```bash
compose ps                         # what runs
compose logs -f app                # the app's log
compose restart app                # unfinished runs carry on after the restart (DBOS, EXECUTOR_ID=vm-1)
compose exec backup backup now     # a backup now, into /opt/montybot/backups
```

**Restore a backup** into a new database, check it, then point the app at it or rename it:

```bash
compose exec backup createdb montybot_restored
compose exec backup pg_restore --no-owner -d montybot_restored /backups/montybot-YYYYMMDDTHHMMSSZ.dump
```

Copy `/opt/montybot/backups` off the server (rsync, a bucket) for backups that survive losing the disk; that is not
automated yet.

## The browser jail

Each Chrome runs inside bwrap with its own user, PID, IPC and network namespaces, its own profile folder and `/tmp`,
an empty environment, and only its own X screen (`montybot/browser/chromium_linux.py`, `CHROMIUM.md`). Chrome's own
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

Hosts without AppArmor (Debian, most other distributions) need only the container settings.

**Network.** bwrap's `--unshare-net` leaves Chrome only loopback. Its connections leave through the app's egress
proxy (`montybot/browser/egress.py`): SOCKS5 over a Unix socket, which resolves each host name itself and refuses
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
MONTYBOT_TEST_DEPLOY=1 uv run pytest tests/test_deploy.py -v
```

- the web app behind the login, over TLS;
- Chromium in bwrap with Chrome's own sandbox on, opening example.com and refused `postgres`, `10.0.0.1`,
  `169.254.169.254` and the app's own port;
- a scripted run that reads example.com through the app;
- a run waiting on a question survives `docker compose restart app` and finishes when the question is answered;
- `backup now` writes a dump that restores into a new database with the same users.

It ran on colima's Ubuntu 24.04 arm64 VM with the AppArmor profile installed.

## Not done

- Full Monty (`monty-server`, `monty-worker`) is not in this compose file: the server has no checkout of
  monty-private to build them from. The app runs Monty in local subprocesses (`MONTY_URL` unset). The root
  `compose.yaml` has them for local development.
- Backups stay on the server's disk.
- One app process (`EXECUTOR_ID=vm-1`); more would each need their own id.
