#!/bin/sh
# The `backup` service in deploy/compose.yaml: pg_dump of the whole database (our tables and DBOS's) every night at
# BACKUP_AT (UTC, HH:MM) into /backups, deleting dumps older than BACKUP_KEEP_DAYS. `backup now` takes one at once.
#
# Restore into a new database (pg_restore reads the custom format pg_dump writes here):
#   docker compose exec backup createdb montybot_restored
#   docker compose exec backup pg_restore --no-owner -d montybot_restored /backups/montybot-YYYYMMDDTHHMMSSZ.dump
set -eu
umask 077  # the dumps hold everything; only root on the host reads them

# Each step checked by hand: `set -e` does not apply inside a function called from `dump || ...`. A failed dump
# leaves no file and deletes no old ones.
dump() {
    file=/backups/montybot-$(date -u +%Y%m%dT%H%M%SZ).dump
    if ! pg_dump --format=custom --file="$file.part"; then
        rm -f "$file.part"
        return 1
    fi
    mv "$file.part" "$file" || return 1
    find /backups -name 'montybot-*.dump' -mtime +"$BACKUP_KEEP_DAYS" -delete
    echo "backup: wrote $file ($(du -h "$file" | cut -f1))"
}

if [ "${1:-}" = now ]; then
    dump
    exit
fi

trap 'exit 0' TERM INT  # PID 1 in the container: stop at once on `docker compose stop`
echo "backup: every day at $BACKUP_AT UTC, keeping $BACKUP_KEEP_DAYS days"
while true; do
    now=$(date -u +%s)
    next=$(date -u -d "today $BACKUP_AT" +%s)
    [ "$next" -gt "$now" ] || next=$(date -u -d "tomorrow $BACKUP_AT" +%s)
    sleep $((next - now)) &
    wait $!
    dump || echo 'backup: pg_dump failed; trying again tomorrow' >&2
done
