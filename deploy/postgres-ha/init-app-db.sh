#!/usr/bin/env bash
# Patroni post_init hook: runs once, on the leader, after first bootstrap.
# $1 is a superuser libpq connection string to the new primary.
#
# Only the empty database and its owner are created here; the API creates its own
# tables (init_db() calls and migrations/) on first boot.
set -euo pipefail

CONN="$1"
APP_USER="${CRIMSON_APP_USER:-crimson}"
APP_DB="${CRIMSON_APP_DB:-crimson}"
: "${CRIMSON_APP_PASSWORD:?CRIMSON_APP_PASSWORD must be set}"

# psql does not interpolate :'var' inside a $$ block, so the existence check is
# done in the shell and the CREATE uses psql's quoted :"ident" and :'literal'.
if ! psql "$CONN" -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${APP_USER}'" | grep -q 1; then
  psql "$CONN" -v ON_ERROR_STOP=1 \
    -v app_user="$APP_USER" -v app_password="$CRIMSON_APP_PASSWORD" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password';
SQL
fi

# CREATE DATABASE cannot run inside a DO block, so it is guarded in the shell.
if ! psql "$CONN" -tAc "SELECT 1 FROM pg_database WHERE datname = '${APP_DB}'" | grep -q 1; then
  psql "$CONN" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${APP_DB}\" OWNER \"${APP_USER}\""
fi

echo "[init-app-db] application role '${APP_USER}' and database '${APP_DB}' are ready."
