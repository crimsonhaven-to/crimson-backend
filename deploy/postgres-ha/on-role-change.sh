#!/usr/bin/env bash
# Patroni callback: make the local PgBouncer (127.0.0.1:6432) drop its server
# connections whenever this node's role changes.
#
# The app relies on target_session_attrs=read-write to find the primary. Since
# PostgreSQL 14, libpq judges writability from the server's startup parameters, and
# PgBouncer caches those from its first server connection and replays them. After a
# role change the pooler therefore advertises the node's previous role:
#
#   * a demoted node still claims writable: libpq picks it and writes fail with
#     "cannot execute ... in a read-only transaction";
#   * a promoted node still claims read-only: libpq skips it and the URL fails
#     with "session is read-only".
#
# Fresh server connections fix both, and RECONNECT opens them. See pgbouncer#859.
#
# Patroni calls this as: on-role-change.sh <action> <role> <cluster>, on the node
# whose role changed, which is exactly the node whose pooler is stale.
#
# It lives in the pgdata bind mount so it can be added to a running cluster with
# `patronictl reload` alone, with no container recreate and no failover.
#
# Failure must never take the database down: the cluster is healthy either way and
# the pooler self-heals within server_lifetime (600s). So this logs and exits 0.
set -uo pipefail

PGBOUNCER_PORT=6432

ACTION="${1:-unknown}"
ROLE="${2:-unknown}"

log() { echo "[on-role-change] $*"; }

if [ -z "${CRIMSON_APP_PASSWORD:-}" ]; then
  log "no CRIMSON_APP_PASSWORD in the environment, cannot reach pgbouncer :${PGBOUNCER_PORT}"
  exit 0
fi

CONNINFO="host=127.0.0.1 port=${PGBOUNCER_PORT} user=${CRIMSON_APP_USER:-crimson} dbname=pgbouncer connect_timeout=5"

for attempt in 1 2 3 4 5; do
  if PGPASSWORD="$CRIMSON_APP_PASSWORD" psql "$CONNINFO" -tAc "RECONNECT" >/dev/null 2>&1; then
    log "action=${ACTION} role=${ROLE}: pgbouncer :${PGBOUNCER_PORT} server connections recycled (attempt ${attempt})"
    exit 0
  fi
  sleep 2
done

log "action=${ACTION} role=${ROLE}: could not reach pgbouncer :${PGBOUNCER_PORT} after 5 attempts; it will self-heal within server_lifetime"
exit 0
