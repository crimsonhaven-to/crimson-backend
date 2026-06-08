# Crimsonhaven — Highly-Available PostgreSQL with Patroni

This directory stands up the **external, redundant PostgreSQL** that the
Crimsonhaven backend expects (`DATABASE_URL` in `docker-stack.yml`). It's a
3-node cluster managed by **Patroni**, so if a whole database host dies the
cluster promotes a standby automatically and the API keeps serving.

You've never used Patroni before — that's fine. This README walks you from three
bare Linux hosts to a working cluster, then migrates your dev data (keeping the
donors) into it. Follow the steps top to bottom.

---

## 1. What you're building, in plain terms

Three identical database hosts. On each one, two containers:

- **etcd** — a tiny shared notepad the three hosts use to *agree on who is the
  primary*. It needs a majority (2 of 3) alive to make decisions. This is why
  you need **three** hosts, not two: two can't form a majority if one dies.
- **Patroni** — runs PostgreSQL and watches etcd. Exactly one node is the
  **primary** (accepts writes); the other two are **standbys** that stream a live
  copy of the data. If the primary disappears, Patroni promotes a standby within
  seconds. No human needed.

```
                 your stateless API Swarm
                 DATABASE_URL -> all three DB hosts (it finds the primary itself)
                          │
        ┌─────────────────┼──────────────────────────────┐
        │                 │                               │
   ┌────┴─────┐      ┌─────┴────┐                    ┌─────┴────┐
   │  pg-1     │      │  pg-2    │                    │  pg-3    │
   │ PRIMARY   │◀────▶│ standby  │                    │ standby  │   ← streaming replication
   │ Patroni   │      │ Patroni  │                    │ Patroni  │
   │ etcd      │◀────▶│ etcd     │◀──────────────────▶│ etcd     │   ← consensus (majority rules)
   │ RAID1     │      │ RAID1    │                    │ RAID1    │
   └──────────┘      └──────────┘                     └──────────┘
```

**Three layers of safety, and why you need all three:**

| Layer | Protects against | In this setup |
|---|---|---|
| **RAID1** (your disks) | one disk dying in a host | your hardware — no failover even needed |
| **Replication** (Patroni) | a whole host dying | the standbys + automatic promotion |
| **Backups** (pgBackRest) | a bad `DROP`/`DELETE`, or losing the whole site | §8, shipped off-site |

Replication is **not** a backup: it copies your mistakes instantly. That's what
backups are for. RAID1 is **not** redundancy: a dead host is still a dead host.
You want all three.

---

## 2. Files in this directory

| File | What it is |
|---|---|
| `docker-compose.yml` | the etcd + Patroni stack — run one copy per host |
| `Dockerfile.patroni` | builds the PostgreSQL 17 + Patroni + pgBackRest image |
| `patroni.yml` | cluster-wide Patroni config (identical on every host) |
| `init-app-db.sh` | creates the `crimson` app role + database on first bootstrap |
| `.env.example` | per-host settings — copy to `.env` and edit on each host |
| `pgbackrest/pgbackrest.conf` | backup + WAL-archive configuration |
| `pgbackrest/backup-if-leader.sh` | cron-friendly "back up only if I'm the primary" |
| `migrate-dev-to-prod.ps1` | copies the precious tables (incl. donors) dev → prod |

---

## 3. Ports each host uses

Open these **between the three DB hosts**, and open `5432` **from your Swarm
nodes**. Nothing here should face the public internet.

| Port | Used by | Who needs to reach it |
|---|---|---|
| `5432` | PostgreSQL | the API (Swarm nodes) + the other DB hosts |
| `8008` | Patroni REST API | the other DB hosts (and you, for `patronictl`) |
| `2379` | etcd client | Patroni on all hosts |
| `2380` | etcd peer | the other DB hosts |

---

## 4. Prerequisites on each of the three hosts

1. A modern Linux server with **Docker Engine + Docker Compose v2** installed.
2. The **RAID1 array mounted** at a stable path (e.g. `/srv`). Confirm with `df -h`.
3. **Time synchronized** (`chronyd`/`systemd-timesyncd`) — consensus systems hate
   clock skew.
4. Static LAN IPs (the example uses `10.0.0.11/12/13`).
5. This repo present on each host (e.g. `git clone` to `/srv/crimson`).

Create the data + backup directories on the RAID1 mount and hand them to the
container's `postgres` user (uid **999**):

```bash
sudo mkdir -p /srv/crimson-pgdata /srv/crimson-pgbackrest
sudo chown -R 999:999 /srv/crimson-pgdata /srv/crimson-pgbackrest
chmod +x /srv/crimson/deploy/postgres-ha/init-app-db.sh \
         /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh
```

---

## 5. Configure each host (`.env`)

On **each** host, in this directory:

```bash
cp .env.example .env
```

Edit `.env`. Three values differ **per host** — set them to that machine's
identity:

| Variable | pg-1 | pg-2 | pg-3 |
|---|---|---|---|
| `HOST_IP` | `10.0.0.11` | `10.0.0.12` | `10.0.0.13` |
| `PATRONI_NAME` | `pg-1` | `pg-2` | `pg-3` |
| `ETCD_NAME` | `etcd-1` | `etcd-2` | `etcd-3` |

Everything else is **identical on all three** — most importantly the same
`ETCD_INITIAL_CLUSTER`, `ETCD_CLIENT_HOSTS`, and the **same passwords** on every
host. Generate each secret once with `openssl rand -hex 24` and paste the same
value into all three `.env` files.

> Edit the CIDRs in `patroni.yml` (`pg_hba`) if your networks aren't inside
> `10.0.0.0/8`. The `all` rules let the API connect; the `replication` rule lets
> the DB hosts copy data to each other.

---

## 6. Bring up the cluster

Order matters the first time: **all etcd first, then all Patroni.**

**6a. Build the image on each host** (or build once and `docker save`/`load`):

```bash
docker compose build
```

**6b. Start etcd on all three hosts.** Run on each, then verify the cluster
formed (a healthy list shows all three members):

```bash
docker compose up -d etcd
# verify (run on any host):
docker compose exec etcd etcdctl member list
docker compose exec etcd etcdctl endpoint health --cluster
```

If `member list` doesn't show all three, fix that before going further — Patroni
depends on it. (Almost always a firewall on `2379/2380` or a typo in
`ETCD_INITIAL_CLUSTER`.)

**6c. Start Patroni on all three hosts:**

```bash
docker compose up -d patroni
```

The first node to come up initializes the database, becomes **primary**, and
runs `init-app-db.sh` to create the `crimson` role + database. The other two
clone from it and become **standbys**.

**6d. Check cluster status** (from any host):

```bash
docker compose exec patroni patronictl -c /etc/patroni/patroni.yml list
```

You want something like:

```
+ Cluster: crimson-cluster ------+---------+-----------+----+-----------+
| Member | Host        | Role    | State   | TL | Lag in MB |
+--------+-------------+---------+---------+----+-----------+
| pg-1   | 10.0.0.11   | Leader  | running |  1 |           |
| pg-2   | 10.0.0.12   | Replica | running |  1 |         0 |
| pg-3   | 10.0.0.13   | Replica | running |  1 |         0 |
+--------+-------------+---------+---------+----+-----------+
```

One `Leader`, two `Replica`, all `running`, lag ~0. **That's a working HA
cluster.** Tip: `alias pctl='docker compose exec patroni patronictl -c /etc/patroni/patroni.yml'`.

---

## 7. Lock it down (firewall)

The cluster speaks plaintext on the LAN, so the LAN must be trusted. With `ufw`,
on each DB host, allow only the other DB hosts and the Swarm nodes:

```bash
# between DB hosts: postgres, patroni API, etcd
sudo ufw allow from 10.0.0.0/24 to any port 5432,8008,2379,2380 proto tcp
# from the Swarm nodes: postgres only  (replace with your Swarm subnet)
sudo ufw allow from 10.0.1.0/24 to any port 5432 proto tcp
```

Never publish `5432`/`8008`/`2379`/`2380` to the internet.

---

## 8. Backups (do this right after the cluster is up)

WAL archiving is already switched on in `patroni.yml`, but pgBackRest needs its
**stanza** created once. Do it on the **current leader** (`pg-1` above):

```bash
docker compose exec patroni pgbackrest --stanza=crimson stanza-create
docker compose exec patroni pgbackrest --stanza=crimson check     # verifies archiving works
docker compose exec patroni pgbackrest --stanza=crimson backup    # first full backup
```

Then schedule it. Put the **same** cron entry on **all three** hosts —
`backup-if-leader.sh` makes only the current primary actually run, and it follows
the primary automatically after a failover:

```cron
30 2 * * 1-6  /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh incr
30 3 * * 0    /srv/crimson/deploy/postgres-ha/pgbackrest/backup-if-leader.sh full
```

**Strongly recommended:** switch the repo to S3-compatible off-site storage (see
the commented block in `pgbackrest/pgbackrest.conf`). A backup that lives only on
the same RAID arrays you're protecting won't survive a site loss.

---

## 9. Point the API at the cluster

Your backend uses **psycopg 3**, which can take a multi-host URL and find the
writable primary by itself — no load balancer or virtual IP needed. Set this as
the `DATABASE_URL` in your Swarm deploy (use the `crimson` app password from
`.env`):

```
postgresql://crimson:APP_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write&connect_timeout=5
```

`target_session_attrs=read-write` tells libpq to skip standbys and connect to the
primary. On a failover, the pool's connections drop, reconnect, and libpq
re-finds the new primary automatically. Keep `RUN_DB_SYNC=true` on exactly one
API replica, as before — nothing else in `docker-stack.yml` changes.

Quick sanity check from a Swarm host:

```bash
psql "postgresql://crimson:APP_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write" -c "select 1"
```

---

## 10. Migrate your dev data into prod (keeping the donors)

Your dev DB is already PostgreSQL, so this is a small Postgres→Postgres copy of
just the **precious** tables — `accounts`, `favorites`, `watch_progress`, and
`kofi_transactions` (Lumi's Loved Mortals). The mapping tables are left behind on
purpose; prod rebuilds them from Fribb on first boot.

1. **Let the API boot once against the cluster** so its `init_db()` creates the
   (empty) schema in prod. Wait until `/health` is green and the mapping sync has
   populated, then the precious tables exist and are empty.

2. **Run the migration** from your Windows box (needs `pg_dump`/`psql` on PATH —
   `winget install PostgreSQL.PostgreSQL`):

   ```powershell
   $dev  = "postgresql://crimson:crimson@dev-host:5432/crimson"
   $prod = "postgresql://crimson:APP_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write"
   .\migrate-dev-to-prod.ps1 -DevUrl $dev -ProdUrl $prod
   ```

   It dumps the four tables, loads them into prod, and prints a dev-vs-prod row
   count comparison so you can see the donors and accounts landed. `pg_dump`
   carries over the `accounts` identity sequence, so existing `user_id`s are
   preserved and the next signup won't collide.

3. **Verify** the public supporters page renders, then cut the frontend over.

> Re-running? The loader expects empty tables. If prod already has rows,
> `TRUNCATE accounts, favorites, watch_progress, kofi_transactions CASCADE;`
> first, then run the script again.

---

## 11. Day-2 operations (the commands you'll actually use)

Run these via `docker compose exec patroni patronictl -c /etc/patroni/patroni.yml ...`
(the `pctl` alias from §6).

- **See cluster state:** `pctl list`
- **Planned maintenance on the primary** (hand leadership to a standby with zero
  data loss, e.g. before rebooting that host): `pctl switchover`
- **Force a failover** (only if the primary is already broken): `pctl failover`
- **Restart one node's Postgres:** `pctl restart crimson-cluster pg-2`
- **Change a Postgres parameter cluster-wide:** `pctl edit-config` (edits the copy
  in etcd — this is the right way; editing `patroni.yml` after bootstrap does
  nothing).

**What happens when a host dies?** Patroni promotes a standby within ~`ttl`
seconds (30s here, usually faster). The API's connections reconnect and libpq
finds the new primary. When the dead host comes back, Patroni `pg_rewind`s it and
it rejoins as a standby automatically — no action from you.

**Restore from backup (point-in-time):** stop Patroni on the target node, then
`pgbackrest --stanza=crimson --type=time "--target=2026-06-08 14:00:00" restore`,
then start Patroni. Full PITR runbook: see the
[pgBackRest user guide](https://pgbackrest.org/user-guide.html).

> About `ETCD_INITIAL_CLUSTER_STATE`: it's only read when an etcd member first
> creates its data dir, so leaving it `new` is harmless on restarts. Once the
> cluster is healthy you may set it to `existing` in `.env` to make a node's
> intent explicit when re-adding it — optional.

---

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `etcdctl member list` missing nodes | firewall on `2379/2380`, or `ETCD_INITIAL_CLUSTER` mismatch between hosts |
| Patroni won't start, logs mention etcd | etcd not healthy yet (do §6b before §6c), or wrong `ETCD_CLIENT_HOSTS` |
| `patronictl list` shows no Leader | check clock sync; check all three can reach each other on `8008` |
| Permission denied on data dir | the bind mount isn't `chown 999:999` (see §4) |
| `pgbackrest check` fails | run `stanza-create` first; confirm `pg1-path` matches `data_dir` in `patroni.yml` |
| App gets "the database system is read-only" | it connected to a standby — make sure `target_session_attrs=read-write` is in `DATABASE_URL` |

Logs: `docker compose logs -f patroni` and `docker compose logs -f etcd`.
