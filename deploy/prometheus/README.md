# Metrics history (private Prometheus)

Gives Admin › Metrics a time axis. Without it, the tab reads `/metrics` from
whichever replica answered: a live snapshot whose counters reset on every restart.
With it, the tab charts the whole fleet over the last hour, day, week or month.

Optional and reversible. With `PROMETHEUS_URL` unset (the default) the tab keeps its
live snapshot. Deploy Prometheus, confirm it scrapes, then set the variable; unset it
to go back.

## How it fits

```
   ┌──────────────────────────────────────────────┐
   │  crimson_net  (the API stack's overlay)      │
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
   browser, Admin › Metrics
```

- **Prometheus is never reachable from a browser.** It has no auth and its query API
  reads everything the fleet exports, so it has no published port. The backend,
  behind the login wall and admin check, does the querying.
- **The browser never sends PromQL.** It sends a panel id such as `resolve_success`;
  the queries live in `core/prom_query.py`, and an unknown id is a 404.

## Setup

1. **Find the overlay network name.**

   ```bash
   docker network ls --filter driver=overlay
   ```

   Production is `crimson_crimson_net` (stack `crimson`, network `crimson_net`), the
   default. If yours differs, pass it as `CRIMSON_NETWORK` in step 5.

2. **Make sure `METRICS_TOKEN` is set on the backend.** Without it `/metrics` needs
   an admin session and every scrape gets a 401. To create one, run
   `openssl rand -hex 32`, add `METRICS_TOKEN=...` to `crimson.env` on the manager,
   and redeploy the API stack. Check:

   ```bash
   curl -sSf -H "Authorization: Bearer $METRICS_TOKEN" \
        http://127.0.0.1:8000/metrics | head -5
   ```

   Expect `# HELP ...` lines. 401: token mismatch. 503: image built without
   `prometheus-client`.

3. **Create the secret and config.** The token goes in a Swarm secret, never in the
   config or stack file. Use `printf`, not `echo`, to avoid a trailing newline.

   ```bash
   printf '%s' 'PASTE_YOUR_METRICS_TOKEN_HERE' | docker secret create crimson_metrics_token -
   docker config create crimson_prometheus_yml deploy/prometheus/prometheus.yml
   ```

   Both are immutable. To change one, create it under a new name
   (`crimson_prometheus_yml_v2`), point the stack file at it and redeploy.

   Before creating the config, check the service names in `prometheus.yml`
   (`tasks.crimson_api` and so on, for stack `crimson`). A wrong prefix is not an
   error; it silently discovers nothing.

   ```bash
   docker service ls --format '{{.Name}}'
   ```

4. **Pick the node that keeps the data.** The TSDB is a local volume, so Prometheus
   is pinned to one host. Choose it from `docker node ls`.

5. **Deploy.**

   ```bash
   PROMETHEUS_NODE=crimsonswarm01 \
   docker stack deploy -c deploy/prometheus/docker-stack.prometheus.yml crimson-metrics
   ```

   Prefix `CRIMSON_NETWORK=...` if the overlay is not `crimson_crimson_net`. Watch:

   ```bash
   docker service ps crimson-metrics_prometheus
   docker service logs -f crimson-metrics_prometheus
   ```

6. **Confirm it scrapes** before wiring up the backend. The image has no curl, so
   use a throwaway container on the same network:

   ```bash
   docker run --rm --network crimson_crimson_net curlimages/curl:latest \
     -s 'http://prometheus:9090/api/v1/targets?state=any' \
     | grep -o '"health":"[a-z]*"' | sort | uniq -c
   ```

   Expect one `"health":"up"` per backend container. If targets are `down`, the
   `lastError` field in the same response says why:

   | `lastError` | Meaning |
   |---|---|
   | `401 Unauthorized` | Secret and `METRICS_TOKEN` differ. |
   | `503 Service Unavailable` | Image built without `prometheus-client`. |
   | `connection refused`, or no targets | DNS names in `prometheus.yml` do not match the stack name. |

7. **Point the backend at it.** In `crimson.env` on the manager, then redeploy the
   API stack:

   ```bash
   PROMETHEUS_URL=http://prometheus:9090
   ```

   Admin › Metrics now shows a History section above the live snapshot. If the short
   name does not resolve across stacks, use
   `PROMETHEUS_URL=http://crimson-metrics_prometheus:9090`.

   `PROMETHEUS_URL` is the only backend setting. The scrape job name (`crimson-api`)
   and the 12s per-query timeout are fixed in `core/prom_query.py`; the job name
   must match `job_name` in `prometheus.yml`.

## Reading the charts

- **Rates, not totals.** Counter panels use `rate()`, so a deploy that restarts
  containers leaves no cliff.
- **Gaps are gaps.** Ratio panels show nothing when there was no traffic (0/0 is not
  0%). A flat line at 0 means real failures.
- **Two metrics are cluster-wide.** `crimson_download_jobs` and
  `crimson_source_success_ratio` come from the shared database, so every replica
  reports the same value. Panels use `max()`; a custom panel must not `sum()` them.
- **Schema version across replicas** plots the highest and lowest migration version.
  The lines split during a rolling deploy and should converge within a minute or
  two. If they stay apart, a replica is stuck on the old image.

## Turning it off

Unset `PROMETHEUS_URL` and redeploy the API stack. To remove Prometheus itself:

```bash
docker stack rm crimson-metrics
docker secret rm crimson_metrics_token
docker config rm crimson_prometheus_yml
# History survives stack removal; this deletes it.
docker volume rm crimson-metrics_prometheus_data
```

## Browsing Prometheus directly

Its web UI is useful for ad-hoc queries. Publishing the port in the stack file would
put it on the ingress mesh (every node), so instead run a throwaway forwarder bound
to localhost on the Prometheus node:

```bash
docker run --rm -it --network crimson_crimson_net -p 127.0.0.1:9090:9090 \
  alpine/socat TCP-LISTEN:9090,fork TCP:prometheus:9090
```

Then tunnel from your machine and stop the container when done:

```bash
ssh -L 9090:127.0.0.1:9090 you@the-prometheus-node
```
