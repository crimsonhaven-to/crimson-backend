#!/usr/bin/env bash
# Back up only if this host is the Patroni leader. The same cron entry goes on all
# three hosts, so exactly one runs each night and it follows failovers.
#
# Cron example (daily incremental 02:30, weekly full Sunday 03:30):
#   30 2 * * 1-6  /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh incr
#   30 3 * * 0    /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh full
set -euo pipefail

TYPE="${1:-incr}"   # incr | diff | full
cd "$(dirname "$0")/.."   # the directory that holds docker-compose.yml

# Patroni answers 200 on /primary only on the current leader.
if docker compose exec -T patroni curl -fs http://127.0.0.1:8008/primary >/dev/null 2>&1; then
  echo "[backup] this host is the leader, running ${TYPE} backup"
  docker compose exec -T patroni pgbackrest --stanza=crimson --type="${TYPE}" backup
else
  echo "[backup] not the leader, skipping"
fi
