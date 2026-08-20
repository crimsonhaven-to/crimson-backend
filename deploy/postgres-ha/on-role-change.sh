#!/usr/bin/env bash
# Patroni callback: refresh the local PgBouncer whenever this node's role changes.
# Stack: crimson (PgBouncer on 127.0.0.1:6432).
#
# Why this exists
# ---------------
# The application connects with a multi-host DATABASE_URL and
# target_session_attrs=read-write, so libpq is responsible for finding the primary.
# On PostgreSQL 14+ libpq decides that from the startup parameters the server reports
# rather than by asking every time. PgBouncer caches those parameters from the server
# connection it happened to open first and replays them to new clients, so after a
# role change the pooler on a node keeps advertising the role that node used to have:
#
#   * on the DEMOTED node it still claims to be writable, so libpq picks it and every
#     write fails with "cannot execute ... in a read-only transaction";
#   * on the PROMOTED node it still claims to be read-only, so libpq skips the actual
#     primary and the whole URL fails with "session is read-only".
#
# Both clear the moment PgBouncer opens fresh server connections. RECONNECT does
# exactly that. See pgbouncer/pgbouncer#859 and nekomini-api/docs/07-deployment.md,
# where this was first diagnosed and fixed on the nekominidb cluster.
#
# Patroni invokes this as: on-role-change.sh <action> <role> <cluster>. It runs on the
# node whose role changed, which is precisely the node whose pooler is now stale.
#
# It lives in the pgdata bind mount rather than a bind mount of its own so it could be
# retrofitted onto the running cluster with `patronictl reload` alone -- no container
# recreate and no failover. nekominidb mounts it at /etc/patroni/on-role-change.sh
# instead; both work, that stack simply had it from day one.
#
# A failure here must never take the database down: the cluster is healthy either way,
# the pooler self-heals within server_lifetime (600s), and Patroni is not the right
# place to escalate. So this logs and exits 0.
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
