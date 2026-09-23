# PgBouncer connection pooling (co-located with Patroni)

One PgBouncer runs on each of the three database hosts, next to Patroni, so the API
can scale past the ~8 replica ceiling set by Postgres' connection limit.

The change is additive and reversible: it does not touch data, schema or PostgreSQL
config. You stand the bouncers up, test them with `psql`, then switch the app's
`DATABASE_URL` port. Rollback is switching the port back.

## Why

Each API replica opens up to `DB_POOL_MAX` (default 10) connections, all to the
Patroni leader. Postgres allows ~100, so past ~8 replicas new connections fail with
`too many clients already`. PgBouncer in transaction mode lends ~25 real backends to
hundreds of clients, one transaction at a time, so Postgres sees ~25 to 30
connections regardless of replica count.

```
        API Swarm  (DATABASE_URL -> all three DB hosts, :6432)
                          │   target_session_attrs=read-write
        ┌─────────────────┼──────────────────────────────┐
   ┌────┴─────┐      ┌─────┴────┐                    ┌─────┴────┐
   │  pg-1     │     │  pg-2    │                     │  pg-3    │
   │ PgBouncer │     │ PgBouncer│                     │ PgBouncer│  :6432
   │    │      │     │    │     │                     │    │     │
   │ Postgres  │     │ Postgres │                     │ Postgres │  :5432
   │ PRIMARY   │     │ standby  │                     │ standby  │
   └──────────┘      └──────────┘                     └──────────┘
```

Each bouncer talks only to its own node's Postgres (`127.0.0.1:5432`). The app keeps
its multi-host URL with `target_session_attrs=read-write`, only the port changes, so
libpq still finds the leader's bouncer and follows it on failover. No VIP.

Transaction pooling breaks apps that keep session state (temp tables, `LISTEN`,
`SET SESSION`, session advisory locks). This backend keeps none; its one advisory
lock is transaction-scoped.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | the PgBouncer service, one copy per DB host |
| `Dockerfile.pgbouncer` | PgBouncer image from the Alpine package |
| `pgbouncer.ini` | pooler config (transaction mode, sizing) |
| `userlist.txt.example` | template for the auth file `userlist.txt` |

All files are identical on pg-1, pg-2 and pg-3. There is no `.env`.

## Prerequisites

- Patroni is healthy: `pctl list` shows one Leader and two Replicas, all `running`.
  If not, finish `../postgres-ha/README.md` first.
- The repo is cloned on each DB host (e.g. `/srv/crimson/deploy/pgbouncer`).

## Setup

1. **Create `userlist.txt`** on one host:

   ```bash
   cd /srv/crimson/deploy/pgbouncer
   cp userlist.txt.example userlist.txt
   chmod 600 userlist.txt
   ```

   Replace `REPLACE_WITH_YOUR_CRIMSON_APP_PASSWORD` with `CRIMSON_APP_PASSWORD` from
   `../postgres-ha/.env` (the password in the app's `DATABASE_URL`). Keep the quotes:

   ```
   "crimson" "your-actual-crimson-password"
   ```

   With `auth_type = scram-sha-256`, PgBouncer uses this both to verify the app and
   to log in to Postgres. The password already sits in `.env` on these hosts.

   Optional, more secure: store the SCRAM verifier instead. This read-only query
   prints the line to paste over the one in `userlist.txt`:

   ```bash
   docker compose -f ../postgres-ha/docker-compose.yml exec patroni \
     psql "postgresql://postgres:YOUR_SUPERUSER_PASSWORD@127.0.0.1:5432/crimson" -tAqc \
     "select '\"'||rolname||'\" \"'||rolpassword||'\"' from pg_authid where rolname='crimson'"
   ```

   Copy the file to the other hosts:

   ```bash
   scp userlist.txt pg-2:/srv/crimson/deploy/pgbouncer/userlist.txt
   scp userlist.txt pg-3:/srv/crimson/deploy/pgbouncer/userlist.txt
   ```

2. **Build and start** on each host (no ordering between hosts):

   ```bash
   docker compose build
   docker compose up -d
   docker compose ps                 # "running (healthy)" after ~15s
   docker compose logs --tail=20     # "process up: PgBouncer ... listening on 0.0.0.0:6432"
   ```

3. **Open the firewall** for the Swarm subnet only (same as the Patroni guide's
   5432 rule). Never expose 6432 publicly.

   ```bash
   sudo ufw allow from 10.0.1.0/24 to any port 6432 proto tcp
   ```

4. **Test before touching the app.** From any machine with `psql`:

   ```bash
   psql "postgresql://crimson:YOUR_CRIMSON_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:6432/crimson?target_session_attrs=read-write" \
     -c "select pg_is_in_recovery() as on_a_standby, current_user, inet_server_port() as backend_port"
   ```

   Expected:

   ```
    on_a_standby | current_user | backend_port
   --------------+--------------+--------------
    f            | crimson      |         5432
   ```

   `f` means you reached the primary through a bouncer; `crimson` means auth works.
   Then check the pool on a DB host:

   ```bash
   docker compose exec -e PGPASSWORD='YOUR_CRIMSON_PASSWORD' pgbouncer \
     psql "host=127.0.0.1 port=6432 user=crimson dbname=pgbouncer" -c "SHOW POOLS"
   ```

   You should see a `crimson` pool with a few `sv_idle` connections.

   > **Warning:** never add `target_session_attrs=read-write` (or any value but
   > `any`) to an admin console (`dbname=pgbouncer`) connection. libpq then sends
   > `SHOW transaction_read_only`, which the admin console rejects as
   > `invalid command`, so the command fails or hangs. The admin console is local to
   > each bouncer and needs no routing. The `crimson` database is the opposite: it
   > requires `read-write`.

5. **Switch the app** by changing only the port in `DATABASE_URL`:

   ```
   # before
   DATABASE_URL=postgresql://crimson:PASS@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write&connect_timeout=5
   # after
   DATABASE_URL=postgresql://crimson:PASS@10.0.0.11,10.0.0.12,10.0.0.13:6432/crimson?target_session_attrs=read-write&connect_timeout=5
   ```

   Prepared statements must be off behind a transaction pooler. `core/db_pool.py`
   already defaults `DB_PREPARE_THRESHOLD` to disabled; do not set it.

   Redeploy as usual (e.g. `~/crimson-deploy/deploy.sh` or
   `docker stack deploy -c docker-stack.yml crimson`). Keep `RUN_DB_SYNC=true`
   on the single `api-sync` replica; its resync runs as one transaction, which
   transaction pooling handles.

6. **Verify.**
   - `GET /health` is green; search, sign-in, favorites and watch progress work.
   - On the leader host, `SHOW POOLS` (command above): `cl_active` rises with
     traffic while `sv_active`/`sv_idle` stay at or below `default_pool_size`.
   - Real backends on the leader stay low and flat:

     ```bash
     psql "postgresql://crimson:PASS@127.0.0.1:5432/crimson" \
       -c "select count(*) from pg_stat_activity where usename='crimson'"
     ```

   The `api` service `replicas:` in the stack file can now go well past 8.

## Rollback

Set `DATABASE_URL` back to `:5432` and redeploy. The bouncers can stay running idle.

## Operations

Admin console commands, run on a DB host (`dbname=pgbouncer`, no
`target_session_attrs`):

```bash
cd /srv/crimson/deploy/pgbouncer
A() { docker compose exec -e PGPASSWORD='PASS' pgbouncer \
        psql "host=127.0.0.1 port=6432 user=crimson dbname=pgbouncer" -c "$1"; }
A "SHOW POOLS"     # cl_active, sv_idle, cl_waiting per pool
A "SHOW STATS"     # request rates, query times
A "SHOW CLIENTS"
A "SHOW SERVERS"
```

**Tuning:** raise `default_pool_size` in `pgbouncer.ini` (currently 25) only if
`SHOW POOLS` shows `cl_waiting > 0` at peak. Keep it well under Postgres' 100. Apply
with `docker compose up -d` on each host (a brief reconnect).

| Symptom | Cause / fix |
|---|---|
| Test `psql` hangs or "could not connect" | Firewall (step 3), or the bouncer is down: `docker compose ps`. |
| "password authentication failed" | `userlist.txt` does not match the crimson password. Redo step 1 (mind the quotes), `docker compose up -d`. |
| Test shows `on_a_standby = t` | `target_session_attrs=read-write` is missing from the URL. |
| `SHOW *` hangs or `invalid command 'SHOW transaction_read_only'` | `target_session_attrs` on an admin console URL. Drop it (see the warning in step 4). In PowerShell the line continuation is a backtick, not `\`; put the command on one line. |
| App errors mention **prepared statement** | Something set `DB_PREPARE_THRESHOLD`. Unset it and redeploy. |
| `cl_waiting` climbing | Load exceeds the pool. Raise `default_pool_size`, restart the bouncers. |
| Brief connection errors right after a failover | Expected. libpq reconnects and finds the new leader's bouncer within seconds. |
| After a failover, `session is read-only` from every host, or writes fail with `cannot execute ... in a read-only transaction` | Stale role in the pooler (below). Check `on-role-change.sh` is installed (`../postgres-ha/README.md` §6e). To clear it now: `docker exec crimson-patroni-1 /var/lib/postgresql/data/on-role-change.sh manual leader crimson`, or restart the pooler. |

### Stale role after a failover

Since PostgreSQL 14, libpq decides writability from the `in_hot_standby` startup
parameter instead of querying each connection. PgBouncer caches that parameter from
its first server connection and replays it to new clients, so after a role change
each pooler advertises its node's previous role:

- the **demoted** node still claims writable; libpq picks it and writes fail with
  `cannot execute ... in a read-only transaction`;
- the **promoted** node still claims read-only; libpq skips it and the multi-host
  URL fails with `session is read-only`.

This is [pgbouncer#859](https://github.com/pgbouncer/pgbouncer/issues/859), still
open. `server_lifetime` (600s) eventually recycles the connections, but ten minutes of
failed writes is an outage. The fix is the Patroni `on_role_change` callback in
`../postgres-ha/on-role-change.sh`, which issues `RECONNECT` when the role changes.
It drops no clients and covers failovers as well as switchovers.
