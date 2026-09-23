# Highly available PostgreSQL with Patroni

The external PostgreSQL the backend expects (`DATABASE_URL` in the Swarm stack file):
a 3-node Patroni cluster that promotes a standby automatically if a host dies.

## 1. Overview

Each of the three hosts runs two containers:

- **etcd**: consensus on who is primary. Needs a majority (2 of 3), which is why
  there are three hosts.
- **Patroni**: runs PostgreSQL. One node is primary, two are streaming standbys. If
  the primary disappears, Patroni promotes a standby within seconds.

```
                 API Swarm
                 DATABASE_URL -> all three DB hosts (libpq finds the primary)
                          │
        ┌─────────────────┼──────────────────────────────┐
   ┌────┴─────┐      ┌─────┴────┐                    ┌─────┴────┐
   │  pg-1     │      │  pg-2    │                    │  pg-3    │
   │ PRIMARY   │◀────▶│ standby  │                    │ standby  │   streaming replication
   │ Patroni   │      │ Patroni  │                    │ Patroni  │
   │ etcd      │◀────▶│ etcd     │◀──────────────────▶│ etcd     │   consensus
   │ RAID1     │      │ RAID1    │                    │ RAID1    │
   └──────────┘      └──────────┘                     └──────────┘
```

| Layer | Protects against | Provided by |
|---|---|---|
| RAID1 | one disk dying | host hardware |
| Replication | a whole host dying | Patroni standbys and automatic promotion |
| Backups | a bad `DROP`/`DELETE`, or losing the site | pgBackRest (§8), shipped off-site |

Replication copies mistakes instantly, so it is not a backup. You need all three.

## 2. Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | etcd + Patroni stack, one copy per host |
| `Dockerfile.patroni` | PostgreSQL 17 + Patroni + pgBackRest image |
| `patroni.yml` | cluster-wide Patroni config (identical on every host) |
| `init-app-db.sh` | creates the `crimson` role and database on first bootstrap |
| `on-role-change.sh` | Patroni callback that makes the co-located PgBouncer re-read this node's role (§6e) |
| `.env.example` | per-host settings, copy to `.env` |
| `pgbackrest/pgbackrest.conf` | backup and WAL archive config |
| `pgbackrest/backup-if-leader.sh` | cron wrapper that backs up only on the primary |
| `migrate-dev-to-prod.ps1` | copies the user tables (including donors) from dev to prod |

## 3. Ports

Open between the three DB hosts, plus `5432` from the Swarm nodes. Nothing public.

| Port | Service | Reached by |
|---|---|---|
| `5432` | PostgreSQL | API (Swarm nodes), other DB hosts |
| `8008` | Patroni REST API | other DB hosts, `patronictl` |
| `2379` | etcd client | Patroni on all hosts |
| `2380` | etcd peer | other DB hosts |

## 4. Prerequisites (each host)

1. Linux with Docker Engine and Docker Compose v2.
2. RAID1 array mounted at a stable path (e.g. `/srv`); check with `df -h`.
3. Time sync (`chronyd` or `systemd-timesyncd`); etcd is sensitive to clock skew.
4. Static LAN IPs (examples here use `10.0.0.11/12/13`).
5. This repo cloned, e.g. to `/srv/crimson`.

Create the data and backup directories, owned by the container's `postgres` user
(uid 999):

```bash
sudo mkdir -p /srv/crimson-pgdata /srv/crimson-pgbackrest
sudo chown -R 999:999 /srv/crimson-pgdata /srv/crimson-pgbackrest
chmod +x /srv/crimson/deploy/postgres-ha/init-app-db.sh \
         /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh
```

## 5. Configure `.env` (each host)

```bash
cp .env.example .env
```

Per-host values:

| Variable | pg-1 | pg-2 | pg-3 |
|---|---|---|---|
| `HOST_IP` | `10.0.0.11` | `10.0.0.12` | `10.0.0.13` |
| `PATRONI_NAME` | `pg-1` | `pg-2` | `pg-3` |
| `ETCD_NAME` | `etcd-1` | `etcd-2` | `etcd-3` |

Everything else is identical on all three, in particular `ETCD_INITIAL_CLUSTER`,
`ETCD_CLIENT_HOSTS` and every password. Generate each secret once with
`openssl rand -hex 24` and paste the same value on all hosts.

Edit the `pg_hba` CIDRs in `patroni.yml` to match your networks: the `all` rules let
the API connect, the `replication` rules let the DB hosts replicate.

## 6. Bring up the cluster

First bring-up order matters: etcd on all hosts, then Patroni on all hosts.

**6a. Build** on each host (or build once and `docker save`/`load`):

```bash
docker compose build
```

**6b. Start etcd** on each host, then verify all three members:

```bash
docker compose up -d etcd
docker compose exec etcd etcdctl member list
docker compose exec etcd etcdctl endpoint health --cluster
```

Do not continue until all three are listed. Usual causes: firewall on `2379/2380`
or a typo in `ETCD_INITIAL_CLUSTER`.

**6c. Start Patroni** on each host:

```bash
docker compose up -d patroni
```

The first node initializes the database, becomes primary and runs `init-app-db.sh`.
The others clone from it as standbys.

**6d. Check status:**

```bash
alias pctl='docker compose exec patroni patronictl -c /etc/patroni/patroni.yml'
pctl list
```

```
+ Cluster: crimson-cluster ------+---------+-----------+----+-----------+
| Member | Host        | Role    | State   | TL | Lag in MB |
+--------+-------------+---------+---------+----+-----------+
| pg-1   | 10.0.0.11   | Leader  | running |  1 |           |
| pg-2   | 10.0.0.12   | Replica | running |  1 |         0 |
| pg-3   | 10.0.0.13   | Replica | running |  1 |         0 |
+--------+-------------+---------+---------+----+-----------+
```

One Leader, two Replicas, all `running`, lag ~0.

**6e. Install the role-change callback** on each host. Without it, after a failover
the co-located PgBouncer keeps advertising the node's previous role, so
`target_session_attrs=read-write` picks a demoted node or skips the real primary
(pgbouncer#859, see `../pgbouncer/README.md`). The script issues `RECONNECT` when the
role changes.

It lives in the pgdata bind mount rather than its own mount so it can be added to a
running cluster with a reload: no container recreate, no failover.

```bash
sudo install -o 999 -g 999 -m 0755 on-role-change.sh /srv/crimson-pgdata/
docker exec crimson-patroni-1 patronictl reload crimson-cluster "$(hostname)" --force
```

Safe to install before PgBouncer exists: it logs and exits 0 if it cannot reach the
pooler. After the first switchover, the promoted node's Patroni log should show:

```
[on-role-change] action=on_role_change role=primary: pgbouncer :6432 server connections recycled (attempt 1)
```

## 7. Firewall

The cluster speaks plaintext, so the LAN must be trusted. On each DB host:

```bash
# between DB hosts: postgres, patroni API, etcd
sudo ufw allow from 10.0.0.0/24 to any port 5432,8008,2379,2380 proto tcp
# from the Swarm nodes: postgres only (use your Swarm subnet)
sudo ufw allow from 10.0.1.0/24 to any port 5432 proto tcp
```

Never expose `5432`, `8008`, `2379` or `2380` to the internet.

## 8. Backups

WAL archiving is on in `patroni.yml`, but the stanza must be created once, on the
current leader. Until then archive pushes queue up.

```bash
docker compose exec patroni pgbackrest --stanza=crimson stanza-create
docker compose exec patroni pgbackrest --stanza=crimson check     # verifies archiving
docker compose exec patroni pgbackrest --stanza=crimson backup    # first full backup
```

Put the same cron entries on all three hosts. `backup-if-leader.sh` runs only on the
current primary, so it follows failovers:

```cron
30 2 * * 1-6  /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh incr
30 3 * * 0    /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh full
```

Strongly recommended: switch the repo to S3-compatible off-site storage (commented
block in `pgbackrest/pgbackrest.conf`). Backups on the same RAID arrays do not
survive a site loss.

## 9. Point the API at the cluster

psycopg 3 takes a multi-host URL and finds the writable primary, so no load
balancer or VIP is needed. Set `DATABASE_URL` in the Swarm deploy (app password
from `.env`):

```
postgresql://crimson:APP_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write&connect_timeout=5
```

`target_session_attrs=read-write` skips standbys. On failover, pool connections
drop and reconnect to the new primary. Keep `RUN_DB_SYNC=true` on exactly one API
replica.

Sanity check from a Swarm host:

```bash
psql "postgresql://crimson:APP_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write" -c "select 1"
```

Past ~8 API replicas the per-replica pools hit `max_connections`. Add the
co-located PgBouncer and switch `DATABASE_URL` to port `6432`; see
[`../pgbouncer/README.md`](../pgbouncer/README.md).

## 10. Migrate dev data into prod

Copies only the user tables: `accounts`, `favorites`, `watch_progress` and
`kofi_transactions` (donors). Mapping tables are skipped; prod rebuilds them from
Fribb on first boot.

1. Boot the API once against the cluster so its `init_db()` calls and migrations
   create the empty schema. Wait for `/health` to be green and the mapping sync to
   finish.
2. Run the migration from a Windows box with `pg_dump`/`psql` on PATH
   (`winget install PostgreSQL.PostgreSQL`):

   ```powershell
   $dev  = "postgresql://crimson:crimson@dev-host:5432/crimson"
   $prod = "postgresql://crimson:APP_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write"
   .\migrate-dev-to-prod.ps1 -DevUrl $dev -ProdUrl $prod
   ```

   It prints a dev vs prod row count comparison. `pg_dump` carries the `accounts`
   identity sequence, so `user_id`s are preserved and new signups do not collide.
3. Check the public supporters page renders, then cut the frontend over.

The loader expects empty tables. To re-run, first
`TRUNCATE accounts, favorites, watch_progress, kofi_transactions CASCADE;`.

## 11. Operations

Using the `pctl` alias from §6d:

| Task | Command |
|---|---|
| Cluster state | `pctl list` |
| Planned maintenance on the primary (no data loss) | `pctl switchover` |
| Force failover (primary already broken) | `pctl failover` |
| Restart one node's Postgres | `pctl restart crimson-cluster pg-2` |
| Change a Postgres parameter cluster-wide | `pctl edit-config` |

`bootstrap:` in `patroni.yml` is read only when the cluster is first created and then
lives in etcd, so use `pctl edit-config` afterwards; editing the file does nothing.

**Host failure:** Patroni promotes a standby within `ttl` (30s, usually faster) and
libpq reconnects to it. When the dead host returns, Patroni `pg_rewind`s it and it
rejoins as a standby on its own.

**Point-in-time restore:** stop Patroni on the target node, run
`pgbackrest --stanza=crimson --type=time "--target=2026-06-08 14:00:00" restore`,
then start Patroni. See the [pgBackRest user guide](https://pgbackrest.org/user-guide.html).

`ETCD_INITIAL_CLUSTER_STATE` is read only when an etcd member first creates its data
dir, so leaving it `new` is harmless on restarts. Setting it to `existing` once the
cluster is healthy is optional.

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `etcdctl member list` missing nodes | Firewall on `2379/2380`, or `ETCD_INITIAL_CLUSTER` differs between hosts. |
| Patroni will not start, logs mention etcd | etcd not healthy yet (6b before 6c), or wrong `ETCD_CLIENT_HOSTS`. |
| `patronictl list` shows no Leader | Check clock sync and that all hosts reach each other on `8008`. |
| Permission denied on data dir | Bind mount not `chown 999:999` (§4). |
| `pgbackrest check` fails | Run `stanza-create` first; `pg1-path` must match `data_dir` in `patroni.yml`. |
| App gets "the database system is read-only" | Connected to a standby: add `target_session_attrs=read-write` to `DATABASE_URL`. |

Logs: `docker compose logs -f patroni` and `docker compose logs -f etcd`.
