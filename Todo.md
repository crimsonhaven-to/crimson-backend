# Crimson Backend: Improvement Backlog

Five improvements identified during a full read of the backend (routes, engines,
pipeline, deploy config), ordered by impact. Each entry lists the concrete
evidence, the proposed change, and the risk of doing it.

Ground rule for every item: **nothing may break.** These are additive or
behaviour-preserving changes. Where a change could alter behaviour, the safe
variant is described instead of the "clean" one.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done.

---

## 1. Stop blocking the event loop in `account_engine/routes.py`

**Status:** `[x]` done

### The problem

44 synchronous psycopg calls run directly inside `async def` handlers. Every one
of them parks the single event loop for a full Postgres round trip through
PgBouncer, which stalls in-flight `/watch` NDJSON streams served by the same
worker.

Evidence:

| Location | Call | Handler |
| --- | --- | --- |
| `account_engine/routes.py:1065` | `store.upsert_progress(...)` | `upsert_progress` |
| `account_engine/routes.py:1051` | `store.list_progress(...)` | `get_progress` |
| `account_engine/routes.py:978` | `store.upsert_favorite(...)` | `add_favorite` |
| `account_engine/routes.py:692` | `store.list_favorites(...)` | `account_me` |
| `web/routes/system.py:93` | `SELECT COUNT(*) FROM anime_entries` | `/health` |

`POST /account/progress` is the worst case: it fires on a timer for every viewer
during every playback session.

The pattern is already established elsewhere in the codebase. `account_engine/admin_routes.py`
uses `run_in_threadpool` 74 times and `recommend_engine/routes.py` wraps
everything. The account router is simply the one that never got converted.

### The change

Two techniques, chosen per handler:

* **Handlers with no `await`:** change `async def` to `def`. FastAPI then runs the
  whole handler in the threadpool, which fixes the nested sync helpers
  (`audit.log_event`, `_check_invite_code`, `_verify_signed_challenge`,
  `_session_payload`) in one edit rather than 44.
* **Handlers that do `await`:** keep them `async` and wrap each sync call in
  `run_in_threadpool`.

### Landmine (checked, must be respected)

`upsert_progress` calls `_warmup_handler`, which is `web.warmup.schedule_warmup`,
which calls `asyncio.create_task` at `web/warmup.py:221`. `create_task` requires
a running event loop in the calling thread, so **this handler must stay `async`**.
Converting it to `def` would raise `RuntimeError: no running event loop` on every
progress save. It gets wrapped calls instead.

Verified safe for the `def` conversion:

* slowapi's `@limiter.limit` supports sync endpoints (`sync_wrapper`,
  `slowapi/extension.py:752`), so the rate limits are unaffected.
* `account_engine/audit.py`, `account_engine/db.py` and `account_engine/mailer.py`
  contain no `asyncio` usage at all, so nothing in the call chain needs a loop.
* `require_user` is already a sync `Depends`, which FastAPI already threadpools.

### Also in scope

* `web/routes/system.py` `/health`: move the query off the loop. The response
  shape stays byte-identical (nothing in `crimson-client` reads `entries_count`,
  but it is kept anyway).
* `supporters_engine/routes.py:83,138`: the TTL-cached supporters read and the
  Ko-fi webhook insert.
* `scrapers/local_scraper.py:168`: `_store.enabled_roots()` on the `/watch`
  fan-out path, immediately above an existing `asyncio.to_thread` call.

**Risk:** low. No response shape changes, no logic changes, no new dependencies.

---

## 2. Give the schema a real migration story

**Status:** `[x]` done

### The problem

Nine hand-rolled `init_db()` functions carry 53 DDL statements between them, and
the accumulated `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` list has become the
schema's actual version history:

* `account_engine/db.py:104-122` (6 columns bolted onto `accounts`)
* `metadata_engine/db_handler.py:141-215` (9 across `tmdb_shows` / `tmdb_movies`)
* `cache_engine/db.py:128,136`, `local_engine/db.py:69,78`

There is no `schema_migrations` table anywhere in the repo. Consequences:

* Nothing can answer "does production's schema match this image?"
* There is no down path and no way to add a `NOT NULL` or backfill data.
* Every boot re-runs all 53 statements under the advisory lock.
* With Patroni HA and rolling Swarm deploys, a version-skewed replica is
  invisible until it throws at request time.

### The change (deliberately the safe variant)

Do **not** rewrite the existing 53 statements into migration files. That would
risk a production schema divergence, which violates the ground rule. Instead:

1. Add `core/migrations.py`: a `schema_migrations(version, name, checksum, applied_at)`
   table plus a runner that applies numbered `.sql` files from `migrations/` in
   order, one transaction each, under the existing `SCHEMA_INIT_LOCK` advisory
   lock so concurrent replica boots serialize exactly as `init_db()` already does.
2. Keep every existing `init_db()` exactly as it is. They remain the idempotent
   baseline and continue to run first. `migrations/000_baseline.sql` records that
   fact and is a no-op on both fresh and existing databases.
3. Detect **checksum drift**: if an already-applied file's contents changed since
   it was applied, log loudly. That is the "does prod match code" answer.
4. Surface `schema_version` on `/health` and in the admin system panel so a
   version-skewed replica is visible without shell access.
5. New schema changes from here on go in `migrations/NNN_name.sql` rather than
   into another `ADD COLUMN IF NOT EXISTS` line.

### Landmine (checked, must be respected)

`.gitignore` currently ends with:

```
# db migration
*.sql
```

That would silently ignore the entire `migrations/` directory. The ignore rule
must be narrowed with a negation before any `.sql` file is committed, otherwise
the migrations exist locally, are absent from the image, and the schema version
reads as unmigrated in production.

**Risk:** low. Purely additive. An existing database gains one small table and
one baseline row on first boot; no existing DDL is moved or removed.

---

## 3. Extend the wire contract beyond `/watch`

**Status:** `[ ]` not started

### The problem

`core/contracts.py` is the right idea, done well: typed builders, a JSON Schema,
a test asserting they agree, exported for the client to vendor. But it covers
exactly one endpoint. Across roughly 100 others, `response_model=` appears three
times, all in `account_engine/routes.py:352,365,414`. The 24 `BaseModel` classes
are almost entirely request-only.

So `/seasons`, `/info`, `/trending`, `/search` and `/movie-overview`, the ones
`crimson-client` parses field by field, are untyped dicts assembled inline.
Rename a key in `web/queries.py` and nothing fails until a hub renders blank in
production. The 211KB checked-in `openapi.json` documents paths but not a single
response body, so it cannot generate client types either.

### The change

1. Add `response_model` to the roughly 15 endpoints `crimson-client` actually
   consumes. Start from the frontend's fetch call sites, not from the route list.
2. Regenerate `openapi.json` via the existing `scripts/export_openapi.py`.
3. Generate TypeScript types from it into `crimson-client`, so shape drift
   becomes a `tsc` failure in the client's existing CI gate rather than a blank
   page in production.

Extending `core/contracts.py`'s builder pattern to those endpoints instead would
work equally well and matches what is already there. Pick one and be consistent.

**Risk:** medium, and the reason this is item 3 rather than item 1. A
`response_model` **filters** fields not declared on the model, so an incomplete
model silently drops keys the frontend needs. Every model must be derived from
the actual observed response, and each converted endpoint needs a before/after
payload diff. Do these one endpoint at a time, never in bulk.

---

## 4. Add HTTP-level tests, starting with the login wall

**Status:** `[ ]` not started

### The problem

The 18 test files are all pure logic: parsing, crypto, signing, SSRF
classification. `tests/conftest.py` says so explicitly. No test constructs the
app or issues a request. `TestClient` and `ASGITransport` appear nowhere in the
suite.

That leaves the security-critical surface untested. `api.py:507-530` is a
hand-maintained whitelist of paths that bypass authentication: `_PUBLIC_EXACT`,
`_PUBLIC_PREFIXES`, and `_DYNAMIC_PUBLIC_PREFIXES` populated at import time by
the private overlay. It is matched with `path.startswith(...)`, so a path like
`/local_proxy_backdoor` passes the prefix check. Nothing verifies that a gated
path returns 401, that a public one does not, or that the API-key scoping to
`/mw` actually holds.

### The change

One fixture: an ASGI transport plus a monkeypatched
`account_store.get_user_by_session`, which allows asserting the whole wall table
in roughly 30 lines. Then:

* a table-driven test over `_PUBLIC_EXACT` and `_PUBLIC_PREFIXES` (public paths
  reachable, gated paths 401),
* a test that a valid `X-API-Key` unlocks `/mw` and nothing else,
* a smoke test that every router still mounts and every route has a unique path
  and method pair.

It slots straight into the existing `gate` job in
`.github/workflows/build-image.yml`, which already runs `pytest`.

**Risk:** low. Test-only, no production code changes. The main cost is stubbing
the DB cleanly enough that the suite keeps its "no network, no database"
property, which is the reason the suite is fast and runs anywhere today.

---

## 5. Operational metrics and correlatable logs

**Status:** `[ ]` not started

### The problem

`api.py:93` is `logging.basicConfig` with a plain format, and log calls use
f-strings throughout. There is no request ID, so a user reporting "playback
failed at 20:15" cannot be traced across the `/watch` fan-out, the resolver and
the proxy. Grepping for `prometheus` or `opentelemetry` returns nothing.

For a system with six surfaces, a scraper fan-out, three background workers and
a documented top failure mode of "a source went dark", this is the largest
operational gap. The raw material is already collected: `telemetry_engine` has
resolve telemetry, `core/source_health.py` has the canary probe, and
`core/db_pool.pool_stats()` has live pool utilisation. It is all trapped behind
the admin UI with no time series, so only "now" is visible and never a trend.

### The change

1. A `prometheus-client` `/metrics` endpoint, admin-gated or bound to an internal
   interface, exporting: per-source resolve success rate and latency, `/watch`
   time to first stream, cache hit ratio, pool saturation, worker queue depth.
2. A request-ID middleware. It can follow the existing `LumiHeaderMiddleware`
   pattern at `api.py:677`, which already touches `http.response.start` without
   buffering, so the NDJSON stream stays unbuffered.
3. JSON log formatting so the Swarm logs are queryable.

**Risk:** low to medium. The middleware must stay pure-ASGI and non-buffering, or
it will break progressive playback. `/metrics` must not be public: source success
rates and pool internals are operational intelligence and should sit behind the
same gate as the admin dashboard.

---

## Smaller items, not scheduled

* **Rate limit storage is per replica.** `core/rate_limit.py:30` uses in-memory
  storage, so with `replicas: 3` the `30/minute` limit on `/watch` is really
  90/minute for anyone whose requests spread across replicas. The docstring
  already flags it. Pointing `RATE_LIMIT_STORAGE_URI` at Redis closes it, and the
  same Redis would fix the per-replica session cache at `api.py:556`.
* **106 env vars read via bare `os.getenv`** across 30+ files, with only
  `TMDB_API_KEY` validated. A typo in `METADATA_REFRESH_HOUR` silently falls back
  to the default. `core/config_report.py` mitigates the discoverability half.
  `pydantic-settings` would make the whole thing fail fast at boot, but it is a
  bigger refactor than it looks given how scattered the reads are.
