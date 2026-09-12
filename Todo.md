# Crimson Backend: Improvement Backlog

Five improvements identified during a full read of the backend (routes, engines,
pipeline, deploy config), ordered by impact. Each entry lists the concrete
evidence, the proposed change, and the risk of doing it.

Ground rule for every item: **nothing may break.** These are additive or
behaviour-preserving changes. Where a change could alter behaviour, the safe
variant is described instead of the "clean" one.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done.

Later rounds are appended as dated batches rather than renumbered into this list,
so an item's number never moves once something references it:

* [Batch 12092026](#batch-12092026): seven items.

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

**Status:** `[x]` done, as item 3 of [Batch 12092026](#batch-12092026). See there
for what was built and how it differs from the plan below.

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
`.gitlab-ci.yml`, which already runs `pytest`.

**Risk:** low. Test-only, no production code changes. The main cost is stubbing
the DB cleanly enough that the suite keeps its "no network, no database"
property, which is the reason the suite is fast and runs anywhere today.

---

## 5. Operational metrics and correlatable logs

**Status:** `[x]` done

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

### Landmines (checked, must be respected)

1. **Label cardinality.** Labelling HTTP metrics by `scope["path"]` would mint a
   timeseries per episode of per show, which is unbounded in exactly the dimension
   this backend is largest in. `observability.route_label` uses the route
   *template* (`scope["route"].path_format`) and collapses everything unrouted into
   a single `__unmatched__` bucket, so a scanner probing random URLs cannot grow
   the export. The same applies to the per-source telemetry gauge: `source` there
   is client-supplied text from the beacon, not a closed vocabulary, so the
   collector exports only the top 25 by volume (`_TELEMETRY_TOP_N`).
2. **The middleware must not buffer.** `BaseHTTPMiddleware` collects the response
   body, which would hold every `/watch` NDJSON line until the slowest scraper
   finished. `RequestContextMiddleware` is pure-ASGI and touches only
   `http.response.start`.
3. **`/metrics` must not be public.** It is whitelisted on the login wall only so a
   token-carrying scrape can reach the handler; the route then enforces its own
   check and denies by default when no `METRICS_TOKEN` is set.

### What was built

* `core/observability.py`: the request-id ContextVar, the metric definitions in a
  private registry, the recording helpers, and a scrape-time collector that reads
  the DB pool, worker queues and per-source resolve health from the modules that
  already own them. `prometheus_client` is an **optional import**: absent it, every
  helper is a no-op and `/metrics` answers 503, so the dependency can never stop
  the backend from booting.
* `core/logging_setup.py`: replaces the `logging.basicConfig` call. Default output
  is byte-compatible with the old format, plus `[req=<id>]` when a request is in
  scope. `LOG_FORMAT=json` switches to one JSON object per line.
* `RequestContextMiddleware` (`api.py`), added last so it is outermost: mints or
  adopts `X-Request-ID`, binds it for logging, echoes it back, and records the HTTP
  metrics.
* Instrumentation in `web/pipeline.py` (per-scraper and per-resolver duration plus
  outcome, `/watch` time-to-first-stream and fan-out duration) and
  `core/response_cache.py` (L1/L2 hit ratio).
* `worker_stats()` on both download managers, and `METRICS_TOKEN` / `LOG_FORMAT` /
  the prometheus build flag added to the startup config report.

**Risk as built:** low. Additive; the only pre-existing behaviour touched is the
log format (unchanged by default) and the login-wall whitelist (one entry, and the
route behind it is closed by default).

### Phase 1: the time axis

Phase 0 above gave the dashboard real numbers but no history: `/metrics` is a live
snapshot of whichever replica answered, so counters reset on every deploy and
"only now is visible, never a trend" was only half solved. Phase 1 adds a private
Prometheus that scrapes every Swarm task, plus a named-panel proxy the admin
dashboard reads instead of talking to Prometheus itself.

* `core/prom_query.py`: a catalogue of 20 panels across four groups (Traffic,
  Playback, Sources, Fleet), each one a fixed PromQL expression, plus five range
  presets and the query client.
* `/admin/metrics/panels`, `/admin/metrics/series`, `/admin/metrics/targets` in
  `account_engine/admin_routes.py`, all behind `require_admin`.
* `deploy/prometheus/`: scrape config, its own Swarm stack, and a walkthrough.
  Deployed as a **separate stack** joining the API's existing overlay, so a backend
  rollout never redeploys the monitoring that watches it.
* Client (`crimson-client`): `chartFormat.js` (geometry + formatting, pure),
  `TimeChart.jsx` (hand-rolled SVG, no charting dependency) and
  `MetricsHistory.jsx`, above the Phase 0 snapshot, which is kept rather than
  replaced.

### Phase 1 landmines (checked, must be respected)

1. **The browser must never send PromQL.** It sends a panel id, which is a dict
   key; anything else is a 404. Prometheus has no auth and no read-only mode, so a
   passthrough would hand any admin session the whole TSDB.
2. **Three metrics are cluster-wide and must never be summed.**
   `crimson_download_jobs` and `crimson_source_success_ratio` are read out of the
   shared database, so every replica reports the same value and `sum()` silently
   multiplies it by the replica count. They use `max()`. `crimson_schema_version`
   uses `max()` and `min()` side by side so a rolling deploy is visible.
   `tests/test_prom_query.py` asserts this structurally.
3. **NaN is the normal case, not the edge case.** Every ratio panel divides by a
   rate that is zero whenever traffic is zero, and Prometheus reports that as the
   string `"NaN"`. Passing it through emits a body that is not valid JSON, so
   `res.json()` throws in the browser and the whole tab blanks over a quiet
   minute. `_finite()` turns it into `null`, which the chart draws as a gap.
4. **Scrape the tasks, not the service VIP.** `dns_sd_configs` on
   `tasks.<service>` gives every replica its own timeseries. Scraping the load
   balanced VIP would land consecutive scrapes on different replicas and make
   every per-process counter appear to sawtooth.
5. **Series do not share timestamps.** `sum by (status) (rate(...))` emits nothing
   at all for a status nobody hit during part of the window, so zipping two series
   by array index slides one of them sideways in time. `alignSeries` rebuilds a
   shared grid from the reported start/end/step and places each sample by its own
   timestamp.

**Risk as built:** low, and gated. With `PROMETHEUS_URL` unset (the default)
`/admin/metrics/panels` reports `available: false` and the dashboard renders
exactly what it rendered before. Nothing in the serving path is touched.

---

## Smaller items, not scheduled

* **Rate limit storage is per replica.** `core/rate_limit.py:30` uses in-memory
  storage, so with `replicas: 3` the `30/minute` limit on `/watch` is really
  90/minute for anyone whose requests spread across replicas. The docstring
  already flags it. Pointing `RATE_LIMIT_STORAGE_URI` at Redis closes it, and the
  same Redis would fix the per-replica session cache at `api.py:556`.
* **`fetch_trending_anime` ignores `limit` in its cache key.** `/trending` takes
  `limit` as a 1..50 query parameter (`web/routes/discovery.py:71`) but the key is
  the bare string `tmdb:trending` (`metadata_engine/tmdb.py`), so whoever misses
  first decides the list length everyone else is served until it expires. Noticed
  while wrapping the fetcher in single-flight (batch 12092026 item 1) and
  deliberately left alone: coalescing does not make it worse, since a joiner gets
  the leader's length exactly as the cache would have handed it over a moment
  later. Fixing it means either keying on `limit` or slicing the cached list at
  the call site; the second is almost certainly what was meant.
* **106 env vars read via bare `os.getenv`** across 30+ files, with only
  `TMDB_API_KEY` validated. A typo in `METADATA_REFRESH_HOUR` silently falls back
  to the default. `core/config_report.py` mitigates the discoverability half.
  `pydantic-settings` would make the whole thing fail fast at boot, but it is a
  bigger refactor than it looks given how scattered the reads are.

---
---

# Batch 12092026

Seven items picked on 2026-09-12, ordered so each one lands on ground the
previous one already cleared. Same ground rule as everything above:
**nothing may break.** Every item is additive or behaviour-preserving.

Item 3 of the backlog above (extend the wire contract past `/watch`) stays
unscheduled: a `response_model` silently filters undeclared keys, and that risk
is not one to take in the same batch as six other changes.

Ordering rationale:

| # | Item | Why here |
| --- | --- | --- |
| 1 | Cache stampede protection | Self-contained, no schema, no new surface |
| 2 | Thin out `api.py` | Do it *before* item 5 adds a scheduled job, not after |
| 3 | HTTP-level tests (backlog item 4) | Gives items 4-7 a harness to be tested in |
| 4 | Local search for autocomplete | First new surface, read-only, one migration |
| 5 | Airing calendar + subscriptions | The largest item; needs 2 and 3 in place |
| 6 | User-facing security surface | Mostly reuses tables that already exist |
| 7 | Crimson Wrapped | Depends on the write added in 6's neighbourhood |

---

## 1. Cache stampede protection (single-flight)

**Status:** `[x]` done

### The problem

`core/response_cache.py` is a clean two-tier cache, but the check-then-fetch is
at the **call site**, not inside the cache:

```python
cached = await get_cached_response(cache_key)   # miss
if cached: return cached
... upstream fetch ...                          # every caller does this
await set_cached_response(cache_key, data)
```

So on a cold or just-expired key every concurrent request misses L1
(`_local_get`, `response_cache.py:41`), misses L2 (`get_cached_response`,
`response_cache.py:64`) and hits the upstream simultaneously. The hot keys are
exactly the ones with the widest fan-in:

| Call site | Key | Upstream |
| --- | --- | --- |
| `metadata_engine/anilist.py:111` | `anilist:meta:{id}` | AniList GraphQL |
| `metadata_engine/tmdb.py:250` | `tmdb:search:{query}` | TMDB `/search/tv` |
| `metadata_engine/tmdb.py:286` | `tmdb:trending` | TMDB `/discover/tv` |
| `web/pipeline.py:261` | `anilist:meta:{id}` | AniList, on the `/watch` path |

AniList is the painful one. It rate limits hard enough that
`_retry_after_seconds` (`anilist.py:36`) and a full 429 retry ladder already
exist, and `nextAiringEpisode` means a popular title's entry expires while that
title is at peak traffic. The retry ladder then turns one stampede into a
multi-second stall for every request in it.

### The change

Add `core/single_flight.py`: a keyed map of in-flight futures, so the first
caller for a key runs the fetch and every other caller awaits the same result.

```python
result = await single_flight.run(cache_key, lambda: _fetch_from_anilist(...))
```

Adopt it at the four call sites in the table, wrapping the miss path only. The
cache modules themselves are untouched.

### Landmines (checked, must be respected)

1. **An exception must not be cached.** The future is removed from the map in a
   `finally`, so a failed fetch leaves the key clean for the next caller rather
   than pinning a rejected future that every subsequent request re-raises. All
   current waiters still see that one failure, which is correct: they would each
   have failed anyway.
2. **Waiters share one result object.** Every awaiter gets the *same* dict, so a
   caller that mutates it mutates it for the others. This is not new
   (`_local_get` at `response_cache.py:41` already hands the same object to
   every L1 hit) but it is now true for the L2/upstream path too. The four call
   sites are read-only on the result; that must stay true.
3. **The map is per-process, not per-cluster.** With `replicas: 3` a stampede
   collapses to 3 upstream calls rather than 1. That is the same per-replica
   caveat the rate limiter carries (`core/rate_limit.py:30`) and is a large
   reduction regardless; a cross-replica lock would need the Redis that is
   already noted as unscheduled.
4. **No event-loop capture at import.** The map is a plain dict keyed by string;
   futures are created inside the running loop on first use. Creating anything
   loop-bound at import time would break `scripts/export_openapi.py`, which
   imports the app with no loop running.

**Risk:** low. Additive module, four wrapped call sites, no schema, no response
shape change.

### What was built

`core/single_flight.py` plus **three** wrapped fetchers, not four. `web/pipeline.py:261`
needed no change: it reaches AniList through `fetch_anilist_metadata`, so it
inherits the coalescing from the fetcher itself.

Each call site keeps its cache read exactly as it was and moves only the miss
path into a nested `_load()`:

* `metadata_engine/anilist.py` `fetch_anilist_metadata`
* `metadata_engine/tmdb.py` `fetch_tmdb_search_results`
* `metadata_engine/tmdb.py` `fetch_trending_anime`

Two implementation notes beyond the plan:

* **A shared `Task`, not a shared `Future`.** Awaiting a task propagates the
  awaiter's cancellation into it, so one client disconnecting mid-request would
  abort the fetch every other waiter is relying on. `run()` awaits through
  `asyncio.shield`, which also means the fetch runs to completion and populates
  the cache even when every caller has walked away.
* **The task's exception is retrieved in the done-callback.** If every waiter is
  cancelled before a failing fetch settles, nothing consumes the exception and
  asyncio reports it as never retrieved at collection time.

The other ~20 `get_cached_response` call sites (the rest of `metadata_engine/tmdb.py`,
`web/routes/discovery.py`, `manga_engine/routes.py`) are deliberately untouched.
Adopting one is a one-line change if a specific key ever proves hot.

`tests/test_single_flight.py` covers the primitive (coalescing, key release,
failure reaching every waiter without being cached, a cancelled waiter not
aborting the shared fetch) and the wiring, by driving the real AniList fetcher
with a permanently-cold cache and asserting six concurrent misses produce one
POST. Suite: 274 to 281 passing, ruff and mypy clean.

---

## 2. Thin out `api.py`

**Status:** `[x]` done

### The problem

`api.py:102-462` is a single 360-line `lifespan`. It holds schema init for nine
stores, the migration runner, admin bootstrap, nine `scheduler.add_job` calls,
five `asyncio.create_task` warm-ups, the replica-role branching for
`RUN_DB_SYNC` / `RUN_CACHE_WORKER` / `RUN_DOWNLOAD_WORKER`, and the shutdown
drain.

This is the project's own global rule, broken in one file: *`main.py` stays thin:
create the app, register routers and middleware, nothing else.*

The practical cost is that "which replica runs this job" is the single most
load-bearing fact in the file and it is spread across 350 lines of prose. There
are three different pinning rules in play (every replica, `RUN_DB_SYNC` only,
idempotent-so-pinning-is-optional) and reading them off requires reading the
whole function.

### The change

Move the body into `core/startup.py`, leaving `lifespan` as a readable sequence:

```python
async def lifespan(app: FastAPI):
    startup.report_config(logger)
    open_http_client()
    startup.init_schema(logger)
    startup.bootstrap_admins(logger)
    app.state.scheduler = startup.start_scheduler(logger)
    await startup.start_workers(logger)
    yield
    await startup.shutdown(app, logger)
```

`core/startup.py` keeps the job bodies as plain nested functions exactly as they
are today, grouped under three headers that name the pinning rule:
`_register_every_replica_jobs`, `_register_sync_replica_jobs`,
`_register_optional_service_jobs`.

Deliberately **not** done: a per-engine `register_jobs(scheduler)` hook. That is
an abstraction with nine single-use implementations and it would scatter the
pinning rules back across nine files, which is the problem being fixed. One
module that names the three rules once is the smaller and more obvious thing.

### Landmines (checked, must be respected)

1. **Order is load-bearing and must not change.** `init_db()` for all nine
   stores runs *before* `migrations.apply_pending`, because the init functions
   still own the pre-migration baseline (`core/migrations.py:10-15`). And
   `observability.install_state_collector()` must stay inside lifespan, not at
   import, or importing the app wires up something that reads the DB
   (`api.py:134`).
2. **`asyncio.create_task` must stay on the event loop.** The five warm-ups
   (`_warm_changelog`, `_warm_iptv`, `_warm_proxy_health`, `_initial_sync`,
   `_run_backfill`) are created from async context. Moving them into a `def`
   helper called from the scheduler thread would raise
   `RuntimeError: no running event loop`, the same landmine backlog item 1
   records for `upsert_progress`.
3. **`BackgroundScheduler` jobs run in a worker thread with no loop.** That is
   why `_scheduled_sync`, `_refresh_proxy_health` and `_nightly_metadata_refresh`
   call `asyncio.run(...)` rather than awaiting. Preserve that exactly; a job
   body that awaits would silently never run.
4. **`scheduler.start()` happens before the workers start**, and
   `app.state.scheduler` is set immediately after, because shutdown reads it back
   with a `getattr` default (`api.py:454`). Keep both.
5. **This must be a pure move.** No renamed job ids (`replace_existing=True`
   keys on them), no changed intervals, no "while I'm here" fixes. A behaviour
   change hidden in a 350-line move is unreviewable.

**Risk:** low, conditional on landmine 5 being respected. Verified by the import
smoke test and by diffing the startup log line-for-line before and after.

### What was built

`startup.py` at the **repo root**, not `core/startup.py` as planned. `core/` is
the layer everything else imports, and the one place it needs engine state
(`core/observability.py:357,448,467`) it reaches for it with deferred,
function-local imports specifically to keep that property. A `core/` module with
twenty module-level engine imports would invert that. `startup.py` assembles the
app's runtime the way `api.py` assembles its routes, so it belongs beside it and
imports downward. Checked first: nothing in the repo imports `api`, so there is
no cycle. It needed one `COPY startup.py .` in the Dockerfile, which
`tests/test_dockerfile_copies.py` would have caught anyway.

`api.py` went from **990 to 632 lines**, and the lifespan from **359 to 23**:

```python
startup.report_config(logger)
open_http_client()
startup.init_schema(logger)
startup.install_observability(logger)
startup.bootstrap_admins(logger)
app.state.scheduler = startup.start_scheduler(logger)
await startup.start_workers(logger)
yield
await startup.shutdown(app, logger)
```

Twenty-five imports left `api.py` with the body. `logger` is passed in rather
than `startup.py` owning one, so the startup log still reads as one unbroken
sequence from one source, which is what `migrations.apply_pending(logger)` was
already doing in this same code path.

`start_scheduler` is deliberately **sync**, not `async`. It does not await, and
an `async def` that never awaits would be misleading; the requirement that it run
on the loop thread (its warm-ups use `asyncio.create_task`) is stated in its
docstring and at the call site instead.

### How it was verified

The test suite never runs lifespan, so passing tests prove nothing here. A
harness drove both the old inline lifespan and the new one in separate
processes, with every side effect stubbed on the shared singletons, capturing the
ordered log lines and the registered job table:

| Configuration | Log lines | Jobs | Result |
| --- | --- | --- | --- |
| Every service on, sync replica, both workers, demo on | 56 | 8 | identical |
| Everything off, non-sync replica, no workers | 46 | 2 | identical |
| Proxy enabled, demo on a non-sync replica | 50 | 3 | identical |

Identical in content **and order**, with identical job ids and triggers. Three
runs because one configuration cannot reach both sides of nine feature guards;
between them every branch in the moved code executes.

Two differences were normalized away, both harness artifacts rather than
behaviour: the old file is imported under a different module name so `%(name)s`
differs, and APScheduler's own DEBUG line names each job function's qualname,
which moved from `lifespan.<locals>._purge_expired` to
`_register_every_replica_jobs.<locals>._purge_expired`. Job **ids** are what
`replace_existing=True` keys on and those are unchanged.

Suite still 281 passing; ruff clean; `startup.py` is mypy-clean (the one
`api.py` mypy error is pre-existing, on the slowapi handler registration).

---

## 3. HTTP-level tests, starting with the login wall

**Status:** `[x]` done. This is **backlog item 4 above**, scheduled into this
batch. The analysis there stands; what follows is only what was verified since.

### What was checked

* `httpx` is already a runtime dependency, so `ASGITransport` needs **no new dev
  dependency**. `requirements-dev.txt` stays as it is.
* Importing `api.py` runs `Config.validate()` and nothing else; every `init_db()`
  is inside `lifespan`. So driving the app through `ASGITransport` **without**
  running lifespan touches no database, which is what preserves the suite's
  "no network, no database" property (`tests/conftest.py`, `pyproject.toml`).
* The wall reads `Config.REQUIRE_LOGIN` **per request** (`api.py:625`), not at
  import, so a test can toggle it with `monkeypatch.setattr` and does not need a
  separate app instance.
* `_session_is_valid` and `_apikey_is_valid` (`api.py:569`, `api.py:596`) are
  module-level functions the middleware calls by name, so monkeypatching them is
  enough to test the wall without an account store.

### The change

`tests/test_login_wall.py`, table-driven over the real whitelist tuples imported
from `api`, asserting:

* every path in `_PUBLIC_EXACT` and every prefix in `_PUBLIC_PREFIXES` is
  reachable without a bearer,
* a representative gated path (`/trending`, `/watch/...`, `/account/me`) returns
  401 with the `{detail, message, success}` body shape,
* `OPTIONS` is never walled, since CORS preflight carries no `Authorization`,
* a valid `X-API-Key` unlocks `/mw` and `/mw/...` and returns 401 on everything
  else,
* `REQUIRE_LOGIN=false` disables the wall wholesale.

Plus `tests/test_routes_smoke.py`: every router mounts, and no two routes share a
(path, method) pair.

### Landmines (checked, must be respected)

1. **`startswith` on a tuple is a prefix match, not a path-segment match.**
   `/local_proxy` in `_PUBLIC_PREFIXES` also opens `/local_proxy_anything`, and
   `/player` also opens `/players`. Today nothing is mounted at those names, so
   this is latent rather than live. The test must **assert the current
   behaviour**, with the surprise written down next to it. Tightening the match
   to a segment boundary is a behaviour change and does not belong in a
   test-only item; it gets its own entry if it is ever wanted.
2. **`_DYNAMIC_PUBLIC_PREFIXES` is populated at import by the private overlay**
   (`api.py:558`) and is empty in a base build, which is what CI runs. The test
   must read it from the module rather than hard-coding `()`, or it fails on an
   operator build for a reason that has nothing to do with the wall.
3. **Reaching a public route is not the same as it returning 200.** `/changelog`
   answers 503 without `GITHUB_TOKEN`, `/metrics` 503 without
   `prometheus_client`. The assertion is "not 401", never "== 200".
4. **Do not run lifespan.** `ASGITransport(app=app)` alone does not; wrapping the
   app in `LifespanManager` or `TestClient` as a context manager **does**, and
   would try to reach Postgres from CI.

**Risk:** low. Test-only.

### What was built

`tests/test_login_wall.py` (46 tests) and `tests/test_routes_smoke.py` (22), both
in the existing `gate` job, and no new dev dependency: `httpx` is already a
runtime dependency, so `ASGITransport` came for free.

**The whitelist tests drive a stub ASGI app, not the real one.** This was not the
plan and it is better than the plan. Landmine 3 above worried that a public route
may legitimately answer 404 or 503, so the assertion could only ever be "not
401". Against a stub that returns 200 on anything the wall passes through, the
assertion becomes exact: 200 means *the wall let it through*, 401 means it
stopped it, and nothing else can muddy the result. It also removes the reason the
real app was risky here at all, which is that `/health` queries Postgres.

A handful of tests still drive `api.app` itself, on paths the wall denies before
any handler runs: that the middleware is genuinely mounted, and that a 401
carries its CORS headers. The second is the one worth having. `api.py` explains
at length that the wall is added before CORS so CORS ends up outermost and a
browser can read the 401 instead of reporting an opaque CORS failure; that
reasoning was previously enforced by nothing but the comment.

Beyond the planned list:

* **A pinned inventory of every public route.** The whitelist is written as
  prefixes, but what matters is the set of real routes those prefixes open, and
  that set was written down nowhere. `PUBLIC_ROUTES` now pins all 31 (method,
  path) pairs out of 152, each grouped under the reason it is public. Adding a
  route under a whitelisted prefix now fails a test instead of quietly shipping.
  This is the direct mitigation for landmine 1: the prefix match stays as loose
  as it was, but its *consequences* are now enumerated.
* Bearer scheme case-insensitivity and malformed `Authorization` headers.
* `/admin` is never public, asserted separately from the inventory, because it is
  the one surface gated twice and neither gate may become the only one.

### Verified, not assumed

* **Not vacuous.** Removing `/lumi` from `_PUBLIC_EXACT` at runtime flips it from
  200 to 401, and appending `/trending` to `_PUBLIC_PREFIXES` flips it from 401
  to 200. The tests read the live collections, so both mutations fail them. The
  public and gated families also constrain each other: a wall stuck open fails
  one, a wall stuck shut fails the other.
* **Still no database.** The whole suite passes with `DATABASE_URL` pointed at a
  closed port, which is the property `tests/conftest.py` exists to protect.
* **It caught something immediately.** The inventory test failed on its first run
  with `newly public: [('POST', '/kofi/webhook')]`: the route was described in
  the reasons comment but missing from the set. That is exactly the class of
  oversight it is meant to catch, on a mistake made while writing it.

Suite 281 to 349 passing, ruff clean.

---

## 4. Local search for autocomplete

**Status:** `[x]` done

### The problem

`/search/anime`, `/search/shows` and `/search/movies` are live TMDB proxies
(`web/routes/discovery.py:50`, `:92`, `:132`). The client fires **five** of these
per search (`crimson-client/src/hooks/shows.js:29-38`), debounced at 300ms from
3 characters, so a typed title is several rounds of five parallel requests, three
of them crossing the network to TMDB.

For anime this is also *worse recall than the data already on disk*.
`fetch_tmdb_search_results` (`metadata_engine/tmdb.py:248`) takes TMDB's first 10
results and then **drops every one without a local AniList mapping**
(`tmdb.py:270`). So the result set is already constrained to the local mapping
universe, but the round trip to TMDB is paid first, and a title that maps
perfectly well can be pushed out of TMDB's top 10 by things that do not map at
all.

Meanwhile `anime_entries` (`metadata_engine/db_handler.py:156`) holds the whole
Fribb-derived catalogue with `title_romaji`, `title_english` and `title_native`
on local disk.

### The change

Local-first for **anime only**, falling back to TMDB:

1. New `web/queries.py` function: `ILIKE` across the three title columns of
   `anime_entries`, joined to the season map and `tmdb_movie_id` exactly as
   `get_catalogue_items` (`web/queries.py:224-270`) already does, emitting the
   byte-identical item shape `{title, tmdb_id, anilist_id, poster, year,
   vote_average}`.
2. Rank in Python: exact title match, then prefix match, then substring, then
   `start_year` descending. No SQL ranking function, so the query is the same
   with or without an index.
3. If the local result count is under a floor (3), also run the existing TMDB
   search and merge, deduped on `anilist_id`. So a brand-new title absent from
   the last Fribb sync still resolves.
4. `migrations/003_search_trgm.sql`: `pg_trgm` plus GIN trigram indexes on the
   three title columns, which is what makes the leading-wildcard `ILIKE` fast.

### Landmines (checked, must be respected)

1. **`CREATE EXTENSION` can fail, and one failure rolls back every pending
   migration.** `core/migrations.py` applies the whole pending batch in a single
   transaction (its docstring, lines 16-22). The app role is a plain `LOGIN` role,
   not a superuser (`deploy/postgres-ha/init-app-db.sh:28`). `pg_trgm` is a
   *trusted* extension and the role **owns** the database (`init-app-db.sh:35`),
   so on Postgres 17 this is expected to succeed, but "expected to" is not good
   enough when the blast radius is every future migration. The `CREATE EXTENSION`
   goes inside a `DO` block that catches `insufficient_privilege`, and the index
   creation is guarded on `pg_extension` actually containing it.
2. **The index must be optional to the query, not required by it.** This is why
   ranking is in Python and the SQL is plain `ILIKE`: with the index the query is
   fast, without it the query is *identical and slower*. There is no runtime
   extension detection anywhere in the code path, and no second code path to keep
   in sync.
3. **`CREATE INDEX CONCURRENTLY` cannot be used** (same single-transaction
   constraint). A plain `CREATE INDEX` takes a brief ACCESS EXCLUSIVE lock on
   `anime_entries`; at catalogue scale that is sub-second, and it happens during
   boot before the replica serves traffic.
4. **`vote_average` does not exist in `anime_entries`.** Local rows emit `null`
   for it. Checked: the client uses `vote_average` only for hub sorting
   (`crimson-client/src/hubHelpers.js:39`), never on a search suggestion, so a
   null is safe. The key must still be **present** and null, not absent.
5. **Local-first is anime-only.** `tmdb_shows` and `tmdb_movies` are sparse by
   design, populated lazily and holding "only shows that have been opened once"
   (`web/queries.py:215`). Local-first there would return almost nothing for
   anything nobody has opened yet. `/search/shows` and `/search/movies` are not
   touched by this item.
6. **`title_native` matches are mostly noise for a Latin-script query** but cost
   nothing and make a Japanese-script query work. Include the column, keep it
   last in the ranking.

**Risk:** low-medium. The response shape is unchanged and the TMDB path remains
as a fallback, so the worst realistic failure is different result *ordering* than
before. Verified with a before/after payload diff on a set of representative
queries.

### The bug found before writing any of it

**A new migration file would have been silently dropped from the commit.**
`.gitignore` ignores `*.sql` for ad-hoc dumps, and the negation that backlog item
2 said to add had been written as `!migrations/*.sql.trivycache`, which
re-includes nothing. The three existing migrations were unaffected because git
ignores nothing already tracked, so this was invisible until the next migration,
which is this one. `git add migrations/003_search_trgm.sql` would have reported
success and staged nothing, the file would have been absent from the image, and
production would have reported itself up to date while being unmigrated. Exactly
the failure mode item 2 predicted.

`tests/test_migrations.py::test_migrations_are_not_gitignored` existed to catch
this and **passed the whole time**: it asserted `"!migrations/*.sql" in gitignore`
as a substring, and the broken rule starts with those characters. It now matches
a whole stripped line.

### What was built

`migrations/003_search_trgm.sql`, `search_anime_entries` in `web/queries.py`, and
`/search/anime` reworked to call it before TMDB. `tests/test_local_search.py`
adds 21 tests.

**Ranking is a SQL `CASE`, not Python.** The plan said Python so the query would
not depend on the trigram extension; a `CASE` expression does not depend on it
either, and doing it in SQL keeps the ranking in one place instead of splitting
it between an `ORDER BY` and a sort key. It also removes the fetch-many-then-rank
step, so `LIMIT` can do its job. Four buckets: exact title, prefix, substring,
and a fourth for rows that matched on `title_native` alone, which for a
Latin-script query is usually incidental.

**One query, not the catalogue-wide load `get_catalogue_items` does.** That
function pulls every mapping row and every poster into memory, which is fine once
per cache fill and absurd per keystroke. The search uses two `LEFT JOIN LATERAL`
subqueries to pick one season row and one extras row per entry (an AniList id can
map to several seasons, and a plain join would multiply the result), then joins
the two poster tables. One round trip.

**LIKE wildcards are escaped**, which the plan did not mention and needed to.
Unescaped, a query of `%` matches the entire catalogue and `_` matches every
single-character title: a denial of service dressed as a typo.

### Verified against a real Postgres 17

The fake connection in the unit tests does not parse SQL, so a throwaway
`postgres:17-alpine` container was used to check the parts only a real server can
answer:

| Check | Result |
| --- | --- |
| The real runner applies 000 through 003 | version 3, no error |
| `pg_trgm` and all three GIN indexes created | yes |
| Ranking: exact, then prefix, then native-only | correct |
| An entry with no tmdb mapping at all | excluded, as the catalogue excludes it |
| A film keyed by `tmdb_movie_id` | `tmdb_id` null, poster from `tmdb_movies` |
| `%` and `_` as queries | match literally, not wildcards |
| Index actually used at 60k rows | Bitmap Index Scan, 4.1x faster than the seq scan |
| Same results with the index dropped | identical, only slower |

The last two matter together: the index has to earn its place at catalogue scale
(it does) while the query has to be correct without it (it is).

**The landmine was tested, not just reasoned about.** A second database was built
whose role genuinely lacks `CREATE` on the database, which is what Postgres 13+
requires even for a trusted extension, confirmed by the role being refused
directly. Against it the batch still applied cleanly to version 3 with no error,
created no extension and no indexes, and `/search/anime` still returned correct
results. That is the whole point of the `DO` block: had the refusal propagated,
it would have rolled back every pending migration on that database, on that boot
and every future one.

---

## 5. Airing calendar, subscriptions and email notifications

**Status:** `[ ]` not started

### The problem

`metadata_engine/anilist.py:150` already asks AniList for `nextAiringEpisode`
on every title fetch, and `:209` puts it in the returned dict. Nothing persists
it and nothing acts on it. Searching the repo for "notification" returns nothing.

So the backend knows, for every title in the catalogue, when the next episode
airs, and does nothing with it. There is no calendar surface, no way to follow a
title, and no outbound notification of any kind, despite a working SMTP mailer
(`account_engine/mailer.py`) and a bulk sender that already shares one connection
across recipients (`mailer.py:174`).

### The change

Three tables, one poller, one router.

`migrations/004_airing.sql`:

| Table | Key | Holds |
| --- | --- | --- |
| `airing_schedule` | `(anilist_id, episode)` | `airing_at`, `fetched_at` |
| `anime_subscriptions` | `(user_id, anilist_id)` | `created_at`, `notify_email` |
| `airing_notifications` | `(user_id, anilist_id, episode)` | `claimed_at`, `sent_at`, `status` |

`notify_engine/` (routes, store, poller):

* `GET/POST/DELETE /account/subscriptions` behind `require_user`.
* `GET /calendar?days=7`, the airing window joined to the caller's
  subscriptions so the client can show "yours" against "everything".
* A poller job, pinned to the `RUN_DB_SYNC` replica, that refreshes
  `airing_schedule` for subscribed ids from AniList's `Page.airingSchedules`
  (a window query, **not** one request per subscription), then sends for every
  subscription-by-schedule row whose `airing_at` has passed and which has no
  `airing_notifications` row.

### Landmines (checked, must be respected)

1. **Claim before send, never after.** The send is not transactional with the
   ledger write, so the two orderings trade different failures: claim-then-send
   can drop a notification on a crash, send-then-claim can send the same email
   on every poll tick forever. Claim first, with
   `INSERT ... ON CONFLICT DO NOTHING` and a `rowcount` check as the claim. A
   `status` column records sent or failed so a failure is visible and bounded
   retries stay possible, without ever risking an unbounded resend loop.
2. **Pin the poller to one replica.** The claim-by-insert makes concurrent
   replicas *correct*, but three replicas each opening an SMTP connection and
   racing for claims is wasteful and makes the logs unreadable. Pin it to
   `Config.RUN_DB_SYNC` like the metadata jobs (`api.py:380`). Keep the
   claim-by-insert anyway: it is what makes a misconfigured second replica
   harmless rather than a duplicate-mail incident.
3. **`airingAt` is the Japanese broadcast time, not availability on Crimson.**
   The backend cannot know when a third-party source has an episode: those
   resolve client-side, in the viewer's browser, by design. The email copy must
   say "aired in Japan", not "available now". Promising the latter is a support
   burden the architecture cannot pay off.
4. **A burst is the normal case.** A popular seasonal title notifies every one of
   its subscribers within one poll tick. Reuse the `send_broadcast` shape
   (`mailer.py:174`): one SMTP connection, fail soft per recipient, and pace the
   loop, because transactional SMTP providers rate limit per connection and per
   minute. A failed send marks the row failed and does not abort the rest.
5. **Not every account has an email.** Mnemonic Ed25519 accounts have
   `public_key` with a null `email` (`account_engine/db.py:90-92`), and an
   unverified email must not be mailed either (`email_verified`, `db.py:97`).
   Subscribing must still be allowed (the calendar surface is useful on its own),
   so `notify_email` is stored per subscription and the sender skips rows whose
   account has no verified address. The API response must tell the user that, or
   they will silently never receive anything.
6. **`fetch_anilist_metadata` is the wrong tool for the poller.** It is
   one-title-per-request and writes the shared response cache. The poller uses
   its own windowed `airingSchedules` query through the existing `anilist_post`
   helper (`anilist.py:56`) so it inherits the 429 ladder without churning the
   metadata cache.
7. **A schedule slips.** AniList revises `airingAt` when a broadcast is delayed.
   The refresh upserts on `(anilist_id, episode)`, so a slip moves the row. A
   notification already claimed for that episode is **not** re-sent; that is
   correct, and it is also why the claim is keyed on episode rather than on time.

**Risk:** medium, and the reason it is item 5 rather than item 1. It is the only
item in this batch that sends something to a human. Everything else is
recoverable by a redeploy; a bad send is not. Ship it with the poller behind a
config flag, default off, and a dry-run mode that logs recipients without
connecting to SMTP.

---

## 6. User-facing security surface

**Status:** `[ ]` not started

### The problem

The backend keeps a full security ledger and shows the user none of it.
`security_events` (`account_engine/audit.py:93`) carries `user_id`, `ip`,
`user_agent` and `outcome` per event, and `sessions` (`account_engine/db.py:80`)
carries one row per active login. Both are readable only by an admin:
`/security/events`, `/security/stats` and `/users/{id}/revoke-sessions` all live
in `admin_routes.py` (`:255`, `:244`, and the route table).

So a user cannot see where they are signed in, cannot sign out one device, cannot
see that someone has been failing logins against their address, and cannot delete
their own account. `delete_account` exists but only at `admin_routes.py:371`.

### The change

* `GET /account/sessions`: the caller's active sessions, with the current one
  flagged.
* `DELETE /account/sessions/{id}` and `DELETE /account/sessions` (all but
  current).
* `GET /account/security-events`: the caller's own recent events, from a
  **whitelist** of types.
* `DELETE /account`: self-service deletion, password or signature confirmed,
  reusing the existing `store.delete_account`.
* `GET /account/export`: every row this account owns, as one JSON download.
  `/account/favorites/export` (`account_engine/routes.py:765`) already
  establishes the shape for part of it.

`migrations/005_session_devices.sql` adds `user_agent`, `ip` and `last_seen_at`
to `sessions`.

### Landmines (checked, must be respected)

1. **`sessions` has no device columns today.** The table is
   `(token_hash, user_id, created_at, expires_at)` and nothing else
   (`account_engine/db.py:80-85`). Without the migration a "devices" list can
   only say "a session, created at some time", which is not worth shipping.
   Existing rows get NULLs and must render as "unknown device", not be hidden.
2. **Never expose `token_hash`.** It is the primary key, so the obvious
   per-session identifier is the one thing that must not leave the server: it is
   the SHA-256 of a live bearer token and publishing it hands an attacker the
   lookup key for the session table. Revocation addresses a session by an opaque
   id derived per response, or by a short non-reversible prefix, never by the
   hash itself.
3. **`last_seen_at` must not add a write per request.** The login wall already
   avoids a DB round trip per request with a 60s validity cache
   (`api.py:564-589`); a naive `last_seen_at` update would undo exactly that.
   Reuse the same TTL shape the API-key path already uses, which touches
   `last_used_at` at most once per key per TTL (`api.py:591-595`).
4. **`security_events.detail` is an internal blob and must not be echoed.**
   It is written by admin actions and auth choke points and can carry operator
   context. The user-facing endpoint returns `ts`, a whitelisted `event_type`,
   `outcome`, `ip` and a coarse user-agent, and never `detail` or `identity`.
5. **Events with a null `user_id` are not "yours".** A failed login against an
   unknown address writes `identity` with no `user_id`. Matching on `identity`
   to show "someone tried your email" would turn the endpoint into an account
   enumeration oracle for anyone who can register. Filter on `user_id` only.
6. **Deleting the account must not delete the trail.** `security_events.user_id`
   has deliberately **no** foreign key so the ledger survives the account
   (`audit.py:85-87`). A self-service delete must not "tidy that up"; the
   `ON DELETE CASCADE` on favorites, progress and sessions is correct, the
   absent one on the audit log is also correct.
7. **Self-delete needs a real confirmation and a rate limit.** It is reachable
   with a stolen bearer token otherwise. Password for an email account, a signed
   challenge for a mnemonic one, both of which already exist
   (`_verify_signed_challenge`, `routes.py:141`), plus a `@limiter.limit` and an
   audit row in the same shape an admin action writes.

**Risk:** low-medium. Mostly reads over existing tables. The two things that
carry real weight are the irreversible delete and not leaking `token_hash`.

---

## 7. Crimson Wrapped

**Status:** `[ ]` not started

### The problem, and the honest version of it

`watch_progress` (`account_engine/db.py:174`) looks like a history table and is
not one. Its primary key is `(user_id, item_key)` and `upsert_progress`
(`account_engine/routes.py:1012`) overwrites the row on a timer during playback.
So:

* `updated_at` is the **last touch**, not when the episode was watched. An
  episode watched in January and resumed in December reads as December.
* A rewatch overwrites rather than appends, so it is invisible.
* `position_seconds` is the last known position, so "hours watched" derived from
  it undercounts a rewatch and overcounts an abandoned episode.

What *is* reliable: one row per episode (`_progress_item_key`, `routes.py:204`),
so distinct-episode and distinct-show counts are sound, and `status` plus
`media_type` are accurate as of the last touch.

This is the item most likely to quietly ship a lie, so the framing matters more
than the SQL.

### The change

Both halves, in one go:

1. `migrations/006_watch_events.sql` adds an append-only `watch_events`
   (`user_id`, `item_key`, `watched_on` DATE, `anilist_id`, `tmdb_id`,
   `media_type`, `seconds`, `first_seen_at`) with a unique key on
   `(user_id, item_key, watched_on)`. Written from the existing `upsert_progress`
   handler as `INSERT ... ON CONFLICT DO UPDATE`, so a progress ping every 30s
   writes **one row per episode per day**, not 120 rows an hour.
2. `GET /account/wrapped?year=YYYY` computes from `watch_events` for the span the
   table covers and from `watch_progress` for the span before it, unions on
   `(item_key, date)`, and returns `approximate: true` with `events_since` when
   any part of the year came from the approximate source.

Stats: episodes and distinct shows, hours, busiest day, longest streak, top
genres (decoded from `anime_entries.genres` / `tmdb_shows.genres`, both already
JSON-in-TEXT), first and last title of the year, and the split across the
anime / show / movie / manga / local surfaces.

### Landmines (checked, must be respected)

1. **Say "approximate" in the payload, not only in a comment.** The first
   Wrapped covers a year that mostly predates `watch_events`. A number derived
   from last-touch timestamps presented as exact is the failure mode here, and
   the client cannot know unless the response says so.
2. **The new write is on the hottest authenticated path.** `POST /account/progress`
   fires on a timer for every viewer in every playback session; backlog item 1
   converted it off the event loop for exactly that reason. The `watch_events`
   write is a second statement on that path: it goes inside the **same**
   `run_in_threadpool` call as the existing upsert, not a second one, and it must
   be a single `INSERT ... ON CONFLICT`, never a read-then-write.
3. **`watched_on` is whose day?** Server-local is wrong for "busiest day" and
   "longest streak" on a global audience. Store UTC, and let the endpoint take a
   UTC offset so the client asks in the viewer's own timezone. Deciding this at
   read time is the only way it stays correct when a viewer moves.
4. **Manga rows key per title, not per chapter** (`routes.py:224`), so a manga
   row's contribution to an "episodes" count is not comparable to an anime row's.
   Count the surfaces separately and never sum them into one headline number.
5. **`local_id` rows carry no `anilist_id` or `tmdb_id`** (`db.py:196`) and
   therefore no genres. They count toward episodes and hours, and are excluded
   from the genre breakdown rather than bucketed as "unknown", which would make
   an operator's own library dominate the chart.
6. **Bound the growth.** One row per episode per day is small (a heavy viewer is
   a few thousand rows a year) but it is unbounded in time, and this is the first
   table in the schema whose whole purpose is to be read years later. Prune at
   three years in the existing `_purge_expired` sweep, and say so where the
   retention is configured.
7. **`status` values come from `_resolve_status`** (`routes.py:991`), not from a
   database constraint. Read the vocabulary from there rather than hard-coding
   strings in the aggregate query.

**Risk:** low for the read endpoint, low-medium for the write. The write is the
part that touches a hot path; the read is pure aggregation over tables nothing
else mutates.
