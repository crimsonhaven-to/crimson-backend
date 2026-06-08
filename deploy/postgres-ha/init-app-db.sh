#!/usr/bin/env bash
# Patroni post_init hook — runs ONCE, on the leader, immediately after the
# cluster is first bootstrapped. Creates the application role + database that the
# Crimsonhaven API connects to via DATABASE_URL.
#
# Patroni invokes this with a single argument: a libpq connection string to the
# freshly-created primary, authenticated as the superuser. The CRIMSON_APP_*
# values come from the container environment (see docker-compose.yml).
#
# This is NOT where the app's tables are created — the API itself runs the
# CREATE TABLE statements (AccountStore/SupporterStore/metadata init_db) on its
# first boot. Here we only provision the empty database and its owner.
set -euo pipefail

CONN="$1"
APP_USER="${CRIMSON_APP_USER:-crimson}"
APP_DB="${CRIMSON_APP_DB:-crimson}"
: "${CRIMSON_APP_PASSWORD:?CRIMSON_APP_PASSWORD must be set}"

# Create the login role if it doesn't already exist (idempotent).
psql "$CONN" -v ON_ERROR_STOP=1 \
  -v app_user="$APP_USER" -v app_password="$CRIMSON_APP_PASSWORD" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password');
  END IF;
END
$$;
SQL

# CREATE DATABASE can't run inside a transaction/DO block, so guard it separately.
if ! psql "$CONN" -tAc "SELECT 1 FROM pg_database WHERE datname = '${APP_DB}'" | grep -q 1; then
  psql "$CONN" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${APP_DB}\" OWNER \"${APP_USER}\""
fi

echo "[init-app-db] application role '${APP_USER}' and database '${APP_DB}' are ready."
