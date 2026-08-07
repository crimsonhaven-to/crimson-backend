# Crimsonhaven, metrics history (private Prometheus)

This gives **Admin › Metrics a time axis**. Today that tab reads `/metrics` off
whichever replica answered the request: real numbers, but a live snapshot, with
counters that reset every time a container restarts. Stand this up and the same
tab gains charts over the last hour, day, week or month, across the whole fleet.

**Read this first, so you can relax:** this is **additive, optional and fully
reversible**. The backend does not change behaviour, the API does not gain a
public surface, and no data is touched. With `PROMETHEUS_URL` unset (which is the
default, and what you have right now) the Metrics tab behaves exactly as it does
today. You stand Prometheus up alongside everything else, prove it is scraping,
and only *then* point the backend at it. If you dislike it, unset one variable and
you are back where you started.

---

## 1. What you are building

```
   ┌──────────────────────────────────────────────┐
   │  crimson_net  (the overlay you already have) │
   │                                              │
   │   api ×3 ──┐                                 │
   │   api-sync ┼── /metrics ──▶ prometheus :9090 │
   │   workers ─┘   (bearer:        │  (no port   │
   │                METRICS_TOKEN)  │   published)│
   │                                │             │
   │   api ◀── /api/v1/query_range ─┘             │
   │    │                                         │
   └────┼─────────────────────────────────────────┘
        │  /admin/metrics/series   (require_admin)
        ▼
   the browser, in Admin › Metrics
```

Two things are worth noticing in that picture.

**Prometheus is never reached from a browser.** It has no login of its own and its
query API can read everything the fleet exports, so it stays inside the overlay
network with no published port. The dashboard talks to the *backend*, which is
already behind the login wall and the admin check, and the backend does the
querying.

**The browser never sends a query.** It sends a panel id like `resolve_success`.
The PromQL behind each panel lives in `core/prom_query.py` and is server-owned;
an id that is not in that dictionary is a 404. Same principle as the signed proxies
and the `/mw` key scoping: a closed vocabulary the server controls.

---

## 2. Prerequisites

* The API stack is deployed and its overlay network exists. Check the exact name:

  ```bash
  docker network ls --filter driver=overlay
  ```

  Production is `crimson_crimson_net` (stack `crimson` plus network `crimson_net`),
  which is the built-in default. If yours differs, pass it as `CRIMSON_NETWORK`
  in step 5.

* **`METRICS_TOKEN` is set on the backend.** Without it, `/metrics` is reachable
  only with an admin session and every scrape gets a 401. If you have not set one:

  ```bash
  openssl rand -hex 32
  ```

  Put it in `crimson.env` on the manager as `METRICS_TOKEN=...` and redeploy the
  API stack. Check it works before going further:

  ```bash
  curl -sSf -H "Authorization: Bearer $METRICS_TOKEN" \
       http://127.0.0.1:8000/metrics | head -5
  ```

  You want `# HELP ...` lines. A 401 means the token does not match; a 503 means
  the image was built without `prometheus-client`.

---

## 3. Create the secret and the config

The scrape credential goes in as a Swarm **secret** (never in the config file or
the stack file), and the scrape config as a Swarm **config**:

```bash
# printf, not echo: echo appends a newline. Prometheus trims it, but there is no
# reason to put it there.
printf '%s' 'PASTE_YOUR_METRICS_TOKEN_HERE' | docker secret create crimson_metrics_token -

docker config create crimson_prometheus_yml deploy/prometheus/prometheus.yml
```

Both are immutable in Swarm. To change either later, create a new one with a
version suffix (`crimson_prometheus_yml_v2`), point the stack file at it, and
redeploy.

**Check the service names in `prometheus.yml` first.** It looks for
`tasks.crimson_api` and friends, because production deploys the stack as
`crimson`. A wrong prefix is not an error, it silently discovers nothing.
Confirm with:

```bash
docker service ls --format '{{.Name}}'
```

---

## 4. Pick the node that keeps the data

Prometheus stores its database in a **local volume**, so it has to land on the
same host every time. Pick one and remember it:

```bash
docker node ls
```

---

## 5. Deploy

```bash
PROMETHEUS_NODE=crimsonswarm01 \
docker stack deploy -c deploy/prometheus/docker-stack.prometheus.yml crimson-metrics
```

Add `CRIMSON_NETWORK=...` in front if your overlay is not `crimson_crimson_net`.

Watch it come up:

```bash
docker service ps crimson-metrics_prometheus
docker service logs -f crimson-metrics_prometheus
```

---

## 6. Prove it is scraping before wiring anything up

This is the step that saves the confusion later. From the node running Prometheus:

```bash
# The container has no curl, so ask through a throwaway one on the same network.
docker run --rm --network crimson_crimson_net curlimages/curl:latest \
  -s 'http://prometheus:9090/api/v1/targets?state=any' \
  | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

You want one `"health":"up"` per backend container. If they are `down`, the
`lastError` field in that same response tells you which of the three usual causes
it is:

| `lastError` says | What it means |
| --- | --- |
| `401 Unauthorized` | The secret and `METRICS_TOKEN` do not match. |
| `503 Service Unavailable` | The image was built without `prometheus-client`. |
| `connection refused` / no targets at all | The DNS names in `prometheus.yml` do not match your stack name. |

---

## 7. Point the backend at it

Only now. In `crimson.env` on the manager:

```bash
PROMETHEUS_URL=http://prometheus:9090
```

Then redeploy the API stack as usual. Open **Admin › Metrics** and the History
section appears above the live snapshot.

If the service name does not resolve (some setups only publish the fully
qualified name across stacks), use that instead:

```bash
PROMETHEUS_URL=http://crimson-metrics_prometheus:9090
```

### Optional variables

| Variable | Default | What it does |
| --- | --- | --- |
| `PROMETHEUS_URL` | *(unset)* | Enables the History section. Unset disables it cleanly. |
| `PROMETHEUS_JOB` | `crimson-api` | The scrape JOB name, not the stack name. Must equal `job_name` in `prometheus.yml`; every panel filters on it. |
| `PROMETHEUS_TIMEOUT` | `12` | Seconds a single panel query may take. |

---

## 8. Reading the charts honestly

A few things the numbers mean, which are easy to misread:

* **Rates, not totals.** Every counter panel is a `rate()`, so a deploy that
  restarts containers leaves no artificial cliff. That is the whole reason for
  doing this rather than reading the counters directly.

* **Gaps are gaps.** A ratio panel shows nothing during a period with no traffic,
  because zero divided by zero is not zero percent. A flat line at 0 means real
  failures; a gap means nothing was attempted.

* **Three metrics are cluster-wide, not per replica.** `crimson_download_jobs` and
  `crimson_source_success_ratio` are read out of the shared database, so every
  replica reports the same value. The panels use `max()` on them. If you ever add
  a panel of your own, do not `sum()` those two or you will multiply them by your
  replica count.

* **"Schema version across replicas"** draws the highest and lowest migration
  version any replica booted at. The lines separate during a rolling deploy and
  should converge within a minute or two. If they stay apart, a replica is stuck
  on the old image.

---

## 9. Turning it off

Unset `PROMETHEUS_URL`, redeploy the API stack, and the Metrics tab returns to the
live snapshot. To remove Prometheus itself:

```bash
docker stack rm crimson-metrics
docker secret rm crimson_metrics_token
docker config rm crimson_prometheus_yml
# The history survives a stack removal. Only run this if you want it gone.
docker volume rm crimson-metrics_prometheus_data
```

---

## 10. Browsing Prometheus directly (optional)

Its own web UI is genuinely useful for ad-hoc queries the dashboard does not have
a panel for. It has no published port on purpose, so reach it over SSH:

```bash
ssh -L 9090:127.0.0.1:9090 you@the-prometheus-node
```

That only works if you also bind it on the host. Rather than publishing the port
in the stack file (which would put it on the ingress mesh and therefore on every
node), run a throwaway attached container when you want it:

```bash
docker run --rm -it --network crimson_crimson_net -p 127.0.0.1:9090:9090 \
  alpine/socat TCP-LISTEN:9090,fork TCP:prometheus:9090
```

Then tunnel to `127.0.0.1:9090` and stop the container when you are done.
