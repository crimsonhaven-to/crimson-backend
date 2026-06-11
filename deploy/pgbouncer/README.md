# Crimsonhaven — PgBouncer connection pooling (co-located with Patroni)

This adds a **connection pooler** in front of your PostgreSQL so you can scale the
API far past today's ~8-replica ceiling. It runs **one PgBouncer on each of your
three database hosts**, right next to Patroni.

**Read this first — why you can relax:** this change is **additive and fully
reversible**, and it **never touches your data, your schema, or your PostgreSQL
configuration**. You stand PgBouncer up *next to* Postgres, prove it works with a
throwaway `psql` test, and only *then* point the app at it. If anything looks
wrong at any point, you change one URL back and you're exactly where you started.
Nothing here can hurt the database. Follow the steps top to bottom and you're done.

---

## 1. The problem this solves, in plain terms

Every API container opens its own little batch of PostgreSQL connections (up to
`DB_POOL_MAX`, default 10). All of them land on the **one** Patroni leader. Postgres
allows ~100 connections total, so once you run more than ~8 API replicas, new ones
start getting *"sorry, too many clients already"*. Connections are the bottleneck.

**PgBouncer fixes this by sharing a small set of real connections among many
clients.** It keeps, say, 25 real connections to Postgres and lets *hundreds* of
API clients take turns using them — each only for the split-second of a single
query. The API tier can then grow as large as your CPUs allow; Postgres still only
ever sees ~25–30 connections.

```
        your stateless API Swarm  (DATABASE_URL -> all three DB hosts, :6432)
                          │   "give me the read-write node"
        ┌─────────────────┼──────────────────────────────┐
        │                 │                               │
   ┌────┴─────┐      ┌─────┴────┐                    ┌─────┴────┐
   │  pg-1     │     │  pg-2    │                     │  pg-3    │
   │ PgBouncer │     │ PgBouncer│                     │ PgBouncer│  :6432 (new)
   │   :6432   │     │   :6432  │                     │   :6432  │
   │    │      │     │    │     │                     │    │     │
   │ Postgres  │     │ Postgres │                     │ Postgres │  :5432
   │ PRIMARY ◀─┼─────┼─ standby │                     │ standby  │
   └──────────┘      └──────────┘                     └──────────┘
        ▲
        └── only the LEADER's bouncer carries traffic; the app finds it
            automatically via target_session_attrs=read-write, and follows
            it on failover — no VIP, no load balancer, same as today.
```

Each bouncer only ever talks to **its own** node's Postgres (`127.0.0.1:5432`). Your
app keeps the exact same multi-host URL trick it uses now — it just points at port
**6432** (the bouncers) instead of **5432** (Postgres direct). libpq still asks for
"the read-write node", so it lands on whichever bouncer fronts the current primary,
and re-finds it after a failover. **Why this is safe for *your* app specifically:**
in transaction mode PgBouncer reuses a backend between transactions, which only
breaks apps that keep *session* state (temp tables, `LISTEN`, `SET SESSION`,
session advisory locks). This backend keeps none of those — its one advisory lock
is transaction-scoped — so it's a clean fit.

---

## 2. Files in this directory

| File | What it is |
|---|---|
| `docker-compose.yml` | the PgBouncer service — run one copy per DB host |
| `Dockerfile.pgbouncer` | builds the tiny PgBouncer image (Alpine package) |
| `pgbouncer.ini` | the pooler config (transaction mode, sizing) — identical on all hosts |
| `userlist.txt.example` | template for the auth file → copy to `userlist.txt` and fill in |
| `README.md` | this guide |

Unlike the Patroni setup, **nothing here is per-host** — the three files are
byte-for-byte identical on pg-1/pg-2/pg-3. There's no `.env` to edit.

---

## 3. Before you start

- Your Patroni cluster is **already up and healthy** (`pctl list` shows one Leader,
  two Replicas, all `running`). If not, finish `../postgres-ha/README.md` first.
- You can run `docker compose` on each DB host (you already do, for Patroni).
- Decide nothing else — the defaults here are production-sane.

---

## 4. Get the files onto each host

You already have this repo on each DB host (you cloned it for Patroni). Just `cd`
into this directory on each host:

```bash
cd /srv/crimson/deploy/pgbouncer      # adjust to wherever you cloned the repo
```

---

## 5. Create the auth file (`userlist.txt`)

PgBouncer needs to know the `crimson` login. Copy the template and fill it in:

```bash
cp userlist.txt.example userlist.txt
```

**The easy way (recommended):** open `userlist.txt` and replace
`REPLACE_WITH_YOUR_CRIMSON_APP_PASSWORD` with your **crimson app password** — the
`CRIMSON_APP_PASSWORD` from `../postgres-ha/.env` (the same password that's in your
app's `DATABASE_URL`). Leave the `"crimson"` and the quotes exactly as they are:

```
"crimson" "your-actual-crimson-password"
```

That's it. (auth_type is `scram-sha-256`, so PgBouncer turns this password into a
secure SCRAM handshake for both verifying the app and logging in to Postgres. This
password already sits in `.env` on these same hosts, so the file adds no new
exposure.) Lock the file down:

```bash
chmod 600 userlist.txt
```

> **More secure (optional):** instead of the plaintext password, store the one-way
> SCRAM *verifier*. This **read-only** command prints the exact line to paste (run
> it on any DB host that's up; swap in your superuser password from `.env`):
>
> ```bash
> docker compose -f ../postgres-ha/docker-compose.yml exec patroni \
>   psql "postgresql://postgres:YOUR_SUPERUSER_PASSWORD@127.0.0.1:5432/crimson" -tAqc \
>   "select '\"'||rolname||'\" \"'||rolpassword||'\"' from pg_authid where rolname='crimson'"
> ```
>
> It only *reads* a catalog (it cannot change anything). Paste its single line of
> output over the line in `userlist.txt`.

Now copy your finished `userlist.txt` to the **same path on all three hosts** (it's
identical everywhere):

```bash
# example: from pg-1, push it to pg-2 and pg-3
scp userlist.txt pg-2:/srv/crimson/deploy/pgbouncer/userlist.txt
scp userlist.txt pg-3:/srv/crimson/deploy/pgbouncer/userlist.txt
```

---

## 6. Build and start the bouncer on each host

On **each** of the three hosts, in this directory:

```bash
docker compose build      # first time only (tiny, ~seconds)
docker compose up -d
```

Check it came up:

```bash
docker compose ps                 # State should be "running (healthy)" after ~15s
docker compose logs --tail=20     # expect a line like "process up: PgBouncer ... listening on 0.0.0.0:6432"
```

Do this on all three hosts. They don't depend on each other or on boot order.

---

## 7. Open the firewall for :6432

Same idea as Postgres' `5432` — the Swarm app nodes need to reach `6432`, nothing
public. Add it alongside your existing rule (matches §7 of the Patroni guide; use
your real Swarm subnet):

```bash
# from the Swarm nodes: the new pooler port
sudo ufw allow from 10.0.1.0/24 to any port 6432 proto tcp
```

Never expose `6432` to the internet.

---

## 8. Prove it works — BEFORE touching the app (the safety gate)

This is the important step. We test the bouncers with a throwaway `psql`, exactly
the way the app will use them, **while the app is still happily on `5432`**. If this
test passes, the cutover in §9 is trivial; if it doesn't, you've changed nothing and
can fix it calmly.

From a machine that has `psql` (any Swarm host, or your laptop), run — note the
**`:6432`** and the same `target_session_attrs=read-write` your app already uses:

```bash
psql "postgresql://crimson:YOUR_CRIMSON_PASSWORD@10.0.0.11,10.0.0.12,10.0.0.13:6432/crimson?target_session_attrs=read-write" \
  -c "select pg_is_in_recovery() as on_a_standby, current_user, inet_server_port() as backend_port"
```

You want exactly this shape:

```
 on_a_standby | current_user | backend_port
--------------+--------------+--------------
 f            | crimson      |         5432
```

- `on_a_standby = f` → you reached the **primary** through a bouncer. 🎉 The
  read-write routing works and follows the leader.
- `current_user = crimson` → auth works.

Also peek at the pool itself. The cleanest way is **on a DB host**, inside the
bouncer container (it already has `psql` and is on the host network), which avoids
SSH tunnels and shell-quoting headaches:

```bash
docker compose exec -e PGPASSWORD='YOUR_CRIMSON_PASSWORD' pgbouncer \
  psql "host=127.0.0.1 port=6432 user=crimson dbname=pgbouncer" -c "SHOW POOLS"
```

> ⚠️ For the admin console (`dbname=pgbouncer`) do **not** add
> `target_session_attrs=read-write` (or any value but `any`). libpq then sends
> `SHOW transaction_read_only` on connect, which the admin console rejects
> (`invalid command`), so the command fails or hangs. The admin console is local
> to each bouncer and has no leader/standby notion — it needs no routing hint. The
> real `crimson` database (§8, §9) is the opposite: it *wants* `read-write`.

You'll see a `crimson` pool with a few `sv_idle` server connections. **If §8 looks
right, you're 95% done and nothing has changed for production yet.**

> Troubleshooting this step? Jump to §12 — the usual culprits are the firewall or a
> typo in `userlist.txt`.

---

## 9. Flip the app over to the pooler

Now the only real change: point the app's `DATABASE_URL` at **`:6432`** instead of
**`:5432`**. Everything else stays the same — the host list, the `target_session_
attrs=read-write`, the password.

**Before** (direct to Postgres):
```
DATABASE_URL=postgresql://crimson:PASS@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write&connect_timeout=5
```
**After** (through the bouncers — just the port):
```
DATABASE_URL=postgresql://crimson:PASS@10.0.0.11,10.0.0.12,10.0.0.13:6432/crimson?target_session_attrs=read-write&connect_timeout=5
```

The app needs prepared statements off behind a transaction pooler. **You don't have
to do anything** — `db_pool.py` already defaults `DB_PREPARE_THRESHOLD` to disabled.
(It's listed in `.env.example` only so you know the knob exists.)

Redeploy the Swarm stack with the new `DATABASE_URL` the same way you normally
deploy (e.g. your `~/crimson-deploy/deploy.sh`, or `docker stack deploy -c
docker-stack.yml crimson-api`). A rolling update swaps replicas one at a time.

> Keep `RUN_DB_SYNC=true` on the one `api-sync` replica, as before — it goes through
> the bouncer too, and its wholesale resync runs as a single transaction, which
> transaction pooling handles fine.

---

## 10. Verify production is healthy

- `GET /health` on the API is green.
- The site loads, search works, sign-in / favorites / watch-progress work (those
  are the DB-backed paths).
- Watch the pool fill in under real traffic (run on the **leader** host):

  ```bash
  docker compose exec -e PGPASSWORD='PASS' pgbouncer \
    psql "host=127.0.0.1 port=6432 user=crimson dbname=pgbouncer" -c "SHOW POOLS"
  ```
  `cl_active` rises with traffic; `sv_active`/`sv_idle` stay small (≤ `default_pool_
  size`). That small, flat server number is the whole win.

- Optional proof of the ceiling lifting — count real backends on the leader; it
  should now stay low and flat no matter how many api replicas you run:

  ```bash
  psql "postgresql://crimson:PASS@127.0.0.1:5432/crimson" \
    -c "select count(*) from pg_stat_activity where usename='crimson'"
  ```

You can now raise `replicas:` for the `api` service in `docker-stack.yml` well past
8 without approaching Postgres' connection limit.

---

## 11. If anything's off — instant rollback

Because nothing about the database changed, rolling back is just the reverse of §9:
set `DATABASE_URL` back to **`:5432`** and redeploy. The app goes straight back to
talking to Postgres directly, exactly as before. You can leave the (idle) bouncers
running while you investigate — they cost nothing.

---

## 12. Day-2 & troubleshooting

**Useful commands** — run on a DB host, via the bouncer container (read-only admin
console). Note: `dbname=pgbouncer` and **no** `target_session_attrs` (see the
warning in §8):

```bash
cd /srv/crimson/deploy/pgbouncer
A() { docker compose exec -e PGPASSWORD='PASS' pgbouncer \
        psql "host=127.0.0.1 port=6432 user=crimson dbname=pgbouncer" -c "$1"; }
A "SHOW POOLS"     # per-pool client/server counts (cl_active, sv_idle, cl_waiting)
A "SHOW STATS"     # request rates, query times
A "SHOW CLIENTS"   # who's connected
A "SHOW SERVERS"   # the real Postgres backends
```

**Tuning:** the one number you might change is `default_pool_size` in
`pgbouncer.ini` (currently 25). Only raise it if `SHOW POOLS` shows `cl_waiting > 0`
under peak load (clients queueing for a backend). After editing, `docker compose up
-d` on each host to restart the bouncers (a brief reconnect, not a data event).

| Symptom | Likely cause / fix |
|---|---|
| §8 `psql` hangs or "could not connect" | firewall: open `6432` from your client (§7). Confirm the bouncer is up: `docker compose ps`. |
| §8 "password authentication failed" | `userlist.txt` doesn't match the crimson password. Re-do §5 (mind the quotes), `docker compose up -d` to reload, retry. |
| §8 shows `on_a_standby = t` | a bouncer answered but you somehow reached a standby — make sure `target_session_attrs=read-write` is in the URL (without it, libpq accepts any node). |
| `SHOW POOLS` / `SHOW *` hangs or `invalid command 'SHOW transaction_read_only'` | you added `target_session_attrs=read-write` (or anything but `any`) to a `dbname=pgbouncer` admin-console URL — drop it (see §8 warning). Easiest is the container form above, which omits it. The `\` line-continuation is a backtick (`` ` ``) in PowerShell, not `\` — a stray `\` becomes a junk arg; just put the command on one line. |
| App errors after §9 mentioning **prepared statement** | something set `DB_PREPARE_THRESHOLD` to a number. Unset it (default = disabled) and redeploy. |
| `SHOW POOLS` shows `cl_waiting` climbing | real load exceeded the pool — raise `default_pool_size` (still keep it well under Postgres' 100), restart bouncers. |
| After a failover, brief connection errors | expected and self-healing: libpq drops, reconnects, and finds the new leader's bouncer within seconds (same as the direct setup). |

That's it — you've decoupled API scaling from the Postgres connection limit, kept
your no-VIP failover, and didn't touch a single row. 🩸
