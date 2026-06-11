# Crimson Backend

Greetings from the heart of Crimson Haven! I am Luminas Crimsonveil—your curator of all things anime. You may call me Lumi ( ^ . ^ )

This is the robust, high-performance engine powering our streaming sanctuary. It handles multi-season anime mapping, automated metadata aggregation, and multi-source stream resolution with elegance and speed.

## 🩸 Core Features

- **Multi-Season Intelligence**: Automatically maps TMDB TV shows and seasons to their corresponding AniList IDs using the Fribb dataset.
- **Unified Search**: Search across TMDB with automatic suggestions and metadata enrichment.
- **Smart Metadata**: Aggregates data from TMDB and AniList for complete info (titles, posters, episode summaries).
- **Advanced Scraping**: Multi-threaded, async scraping from various sources (AniWorld, AnimeKai, AnimeSuge, GogoAnime, etc.). Sources like AniWorld even include **language labels** (English Sub, German Dub, etc.).
- **Stream Resolution**: Resolves third-party embed URLs to direct HLS/MP4 streams or ad-free proxied players.
- **Progressive Streaming**: `/watch` streams results as **NDJSON** — each source is pushed to the client the instant its scraper + resolver finish, so the fastest source plays first instead of waiting for every source.
- **Automatic Sync**: Built-in scheduler keeps the local mapping database up-to-date with upstream sources.
- **Internal Proxies**: Custom reverse proxies for providers like AnimeSuge, Movish, and Jellyfin to bypass ads and CORS.
- **Crimson Player**: A minimal, ad-free HLS/MP4 player served directly from the backend for a seamless experience.
- **Rate Limiting**: Built-in protection against scraping abuse and brute-force attempts on sensitive endpoints.

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **Database**: PostgreSQL (Metadata Mapping, API Cache & Accounts), pooled via psycopg 3
- **Networking**: HTTPX (Async)
- **Parsing**: BeautifulSoup4, Selectolax, lxml
- **Scheduling**: APScheduler
- **Rate Limiting**: slowapi (Token Bucket)
- **Containerization**: Docker & Docker Compose

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**
- **PostgreSQL**: A reachable database (run one locally, or use the bundled `postgres` service via `docker compose`).
- **TMDB API Key**: Required for metadata and search (Legacy API Key / Read Access Token).

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ramon/crimson-backend.git
   cd crimson-backend
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   Create a `.env` file in the root:
   ```env
   TMDB_API_KEY=your_tmdb_api_key_here
   DATABASE_URL=postgresql://crimson:crimson@localhost:5432/crimson
   DEBUG=False
   ```
   The database schema is created automatically on startup (idempotent); you
   only need an empty database and a user that can create tables.

### Running the API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`. You can explore the interactive docs at `/docs`.

---

## ⚙️ Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)).

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `TMDB_API_KEY` | **yes** | – | TMDB Read Access Token (v4) or legacy key. |
| `PROXY_SECRET` | prod | random | HMAC secret for the signed stream proxies (AnimeSuge/PlayIMDb/VOE/Vidmoly). **Must be stable and shared across replicas** or proxied playback 403s. `openssl rand -hex 32`. |
| `DATABASE_URL` | prod | – | Full PostgreSQL connection URL, e.g. `postgresql://crimson:crimson@postgres:5432/crimson`. Takes precedence over the discrete `POSTGRES_*` parts below. |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | no | `localhost` / `5432` / `crimson` / `crimson` / `crimson` | Used to assemble the connection when `DATABASE_URL` is unset. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | no | `1` / `10` | Connection-pool sizing. |
| `DB_CONNECT_TIMEOUT` | no | `30` | Seconds to wait at startup for the database to accept connections. |
| `RUN_DB_SYNC` | no | `true` | Whether this instance runs the periodic Fribb resync. Set `false` on all but one replica. |
| `ALLOWED_ORIGINS` | no | built-in list | Comma-separated CORS origins (e.g. `https://crimsonhaven.to`). |
| `RATE_LIMIT_STORAGE_URI` | no | `memory://` | Storage backend for rate limiting. Set to `redis://redis:6379` for shared limits across replicas. |
| `DEBUG` | no | unset | When truthy, includes exception detail in 500 responses. Leave unset in production. |
| `JELLYFIN_URL` / `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD` | no | – | Enable the optional Jellyfin source. |

All state (the TMDB↔AniList mapping, the API cache, and accounts/favorites/
progress) lives in **one PostgreSQL database**, reached through a process-wide
psycopg connection pool (`db_pool.py`). A Fribb resync only rebuilds the three
mapping tables inside a transaction, so it never touches user data — which is
why mapping and accounts can safely share one database. Because the API holds no
local state, replicas are interchangeable and can all point at the same
database.

---

## 🐳 Deployment

### Docker (single host)

```bash
docker build -t crimson-backend:1.0 .
# Provide secrets via an env file or the shell environment:
TMDB_API_KEY=... PROXY_SECRET=$(openssl rand -hex 32) docker compose up -d
```

The Compose file brings up a bundled **`postgres:17-alpine`** service (data on
the `crimson-pgdata` named volume) and the stateless API container, which runs
as a non-root user and ships a `HEALTHCHECK` that polls `/health`. The API waits
for Postgres to report healthy (`depends_on`) before starting.

> [!NOTE]
> The bundled `postgres` service is meant for **dev / single-host** use. In
> production, point `DATABASE_URL` at a managed or externally-operated PostgreSQL
> instance and drop the bundled service — the API needs no writable volume of its
> own. Override the database credentials via `POSTGRES_USER` / `POSTGRES_PASSWORD`
> / `POSTGRES_DB` (or set a full `DATABASE_URL`).

### Deploying to Docker Swarm

The provided [`docker-compose.yml`](docker-compose.yml) is Swarm-ready
(`deploy:` block with restart policy, resource limits and `stop-first` updates):

```bash
docker stack deploy -c docker-compose.yml crimson
```

> [!IMPORTANT]
> **State now lives in PostgreSQL, so the API is stateless and scales across
> nodes.** Point every replica at the same database (a managed/external Postgres
> in production; the bundled `postgres` service is fine for a single host) and
> scale `anime-api` freely. When running more than one replica:
> 1. Set `RUN_DB_SYNC=true` on **exactly one** replica, `false` on the rest, so
>    the wholesale mapping rebuild runs once.
> 2. Set the **same `PROXY_SECRET`** on every replica so signed proxy URLs
>    verify regardless of which replica serves the follow-up request.
>
> The database access layer is small and isolated in `db_pool.py` (the shared
> pool), `account_engine/db.py`, `metadata_engine/db_handler.py` and the helpers
> in `api.py`.

The stack must be reachable behind a TLS-terminating reverse proxy that sets
`X-Forwarded-Proto`/`X-Forwarded-Host` (uvicorn runs with `--proxy-headers`),
otherwise proxied iframe URLs are emitted as `http://` and blocked as mixed
content on the HTTPS frontend.

---

## 📜 API Reference

### Core Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/search/anime` | Search for anime by name (TMDB-based). |
| `GET` | `/trending` | Fetch popular shows. |
| `GET` | `/catalogue` | List all mapped anime in the local DB. |
| `GET` | `/show/{tmdb_id}` | Get full show details and season list. |
| `GET` | `/overview/{anilist_id}` | **New**: Aggregated show metadata + season list in one round-trip. |
| `GET` | `/info/{tmdb_id}?season=` | Flat merged TMDB + AniList metadata + `episodes_list` for a season. |
| `GET` | `/seasons/{anilist_id}` | All seasons of the show an AniList id belongs to. |
| `GET` | `/season/{tmdb_id}/{num}` | Get metadata for a specific season. |
| `GET` | `/watch/{tmdb_id}/{s}/{e}` | **Primary**: stream episode links progressively as **NDJSON** (see below). |
| `GET` | `/anilist/{id}` | Reverse lookup (AniList ID -> TMDB ID). |

### Progressive `/watch` (NDJSON streaming)

`GET /watch/{tmdb_id}/{season}/{episode}` does **not** return a single JSON body.
It responds with `Content-Type: application/x-ndjson` and emits **one JSON object
per line, flushed as each source resolves** — the client should read the body
incrementally (line by line) and surface each source the moment it arrives.

Lines, in order:

```jsonc
{"type":"meta","success":true,"tmdb_id":1234,"season_number":1,"episode_number":1,"anilist_id":567,"title":"..."}
{"type":"stream","source":"AniWorld","streamType":"hls","url":"https://.../playlist.m3u8","language":"English Sub"}
{"type":"stream","source":"AnimeSuge","streamType":"hls","url":"https://<backend>/animesuge_proxy?u=...&s=..."}
{"type":"done","count":2}
```

- `meta` — always first (ids + resolved title).
- `stream` — zero or more; `streamType` is `hls` | `mp4` | `iframe`. Append each to the player's source list as it arrives.
- `done` — terminal; `count` = number of `stream` lines emitted (`0` → nothing found).

Each scraper runs as its own task (scrape → resolve → emit); a shared seen-set
de-dupes embeds/URLs across sources. If the client disconnects mid-stream the
backend cancels its in-flight scraper tasks. The response carries
`X-Accel-Buffering: no` so an nginx/reverse proxy flushes lines through instead
of buffering the whole response. The legacy `GET /watch/{anilist_id}/{episode}`
301-redirects to the canonical 3-part route for TV seasons, or streams directly
for extras (specials/OVAs/movies).

> The full client-side integration spec (including an Android NDJSON consumer and
> how to play each `streamType`) lives in [`Mobile.md`](Mobile.md).

### Internal Proxies & Utilities

- **`/player`**: Renders the Crimson Player for direct HLS/MP4 streams.
- **`/animesuge_proxy`**: Signed, same-origin proxy for AnimeSuge direct streams (handles Referer and HLS rewriting).
- **`/movish_proxy`**: Proxies Movish streams to handle headers/CORS.
- **`/playimdb_proxy`**: Signed HLS proxy for the PlayIMDb source (injects the referer the PlayIMDb CDNs require; the raw stream is extracted server-side so no PlayIMDb player/ad code is ever loaded).
- **`/voe_proxy`**: Signed HLS proxy for the VOE source (most of aniworld.to is hosted on VOE). VOE's CDN binds the stream token to the IP/ASN **and** User-Agent that resolved the embed, so the raw playlist/segment URLs 403 from the viewer's browser; the backend fetches them server-side (matching UA) and rewrites playlists so segments flow back through the proxy. Emitted as a `streamType: hls` source the frontend's in-app player loads directly.
- **`/vidmoly_proxy`**: Signed HLS proxy for the Vidmoly source (the other common aniworld.to host). Same idea as `/voe_proxy` — the stream is proxied server-side and emitted as a direct `hls` source for the in-app player (also future-proofs against the CDN's `asn=` token binding).
- **`/jellyfin_proxy`**: Proxies Jellyfin HLS segments for same-origin playback.
- **`/health`**: Check system status and database health.

---

## 🔐 Accounts (mnemonic sign-in)

No usernames, no passwords. An account **is** an Ed25519 public key derived from a
12-word BIP39 mnemonic that lives entirely on the client (like P-Stream). The
server stores only the public key and *verifies* signatures over one-time
challenges — the mnemonic / private key never reach the backend, so a DB leak
exposes no credential. User data (favorites + watch progress) lives in its own
PostgreSQL tables, untouched by mapping resyncs.

### Endpoints

| Method | Endpoint | Auth | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/auth/challenge` | – | Get a one-time challenge for a `public_key`. |
| `POST` | `/auth/register` | – | Create the account (signed challenge proves key ownership) → session. |
| `POST` | `/auth/login` | – | Log in to an existing account via signed challenge → session. |
| `POST` | `/auth/logout` | Bearer | Revoke the current session. |
| `GET`  | `/account/me` | Bearer | Profile + favorite/progress counts. |
| `GET`/`POST`/`DELETE` | `/account/favorites` | Bearer | List / add / remove a favorited show. |
| `GET`/`POST`/`DELETE` | `/account/progress` | Bearer | List / upsert / remove per-episode watch progress. |
| `GET`  | `/account/continue-watching` | Bearer | In-progress episodes only, most recent first. |
| `GET`  | `/account/recent` | Bearer | Recently-watched episodes of **any** status (incl. completed), most recent first. `?limit=` (default 20). |

Authenticated requests send `Authorization: Bearer <session_token>`.

### Client key-derivation spec (the frontend must match this exactly)

```text
mnemonic  : 12 BIP39 English words (128-bit entropy)
seed      : PBKDF2-HMAC-SHA512(mnemonic, "mnemonic"+passphrase, 2048, dklen=64)
privSeed  : seed[:32]
keypair   : Ed25519 from privSeed          (RFC 8032; == @noble/ed25519)
public_key: hex(publicKey)                 (64 lowercase hex chars) → the account id
```

In the browser with the standard libraries:

```js
import { generateMnemonic, mnemonicToSeedSync } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english';
import * as ed from '@noble/ed25519';

const mnemonic = generateMnemonic(wordlist);              // show once, user saves it
const seed = mnemonicToSeedSync(mnemonic).slice(0, 32);   // 32-byte private seed
const publicKey = Buffer.from(await ed.getPublicKeyAsync(seed)).toString('hex');

// login / register:
const { challenge } = await (await fetch('/auth/challenge',
  { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ public_key: publicKey }) })).json();
const signature = Buffer.from(
  await ed.signAsync(new TextEncoder().encode(challenge), seed)).toString('hex');
const res = await fetch('/auth/login',                    // or /auth/register
  { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ public_key: publicKey, challenge, signature }) });
const { session_token } = await res.json();
```

Favorites are show-level (keyed by `anilist_id` if given, else `tmdb_id`); watch
progress is per-episode (`status` auto-flips to `completed` past 90%). Both POST
bodies accept `{ tmdb_id?, anilist_id?, season_number?, episode_number?, title?,
poster?, position_seconds?, duration_seconds?, status? }`.

---

## ☕ Ko-fi Supporters ("Lumi's Loved Mortals")

A public list of Ko-fi supporters, fed automatically by Ko-fi's webhook — **no
email scanning, no polling**. Ko-fi has no "list my supporters" API; it only
*pushes* a webhook to us when a payment happens (tip / membership / commission /
shop order) and never on cancellation. So the backend keeps an append-only ledger
of every payment event and derives the public list by aggregating it.

### Setup (one-time)

1. Generate a token and set it: `KOFI_VERIFICATION_TOKEN=<your token>` (see
   `.env.example`). The token comes from Ko-fi itself.
2. In Ko-fi → **Settings → Advanced → Webhooks / API**, copy the *Verification
   Token* into that env var and set the **Webhook URL** to
   `https://<your-backend>/kofi/webhook`.
3. That's it. Every future payment POSTs to that URL and appears on `/supporters`.
   (Past payments aren't back-filled — Ko-fi only sends new events.)

### Endpoints

| Method | Endpoint | Auth | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/kofi/webhook` | Ko-fi token | **Ko-fi only** — receives payment events. Verifies `verification_token`; idempotent. Do not call from the frontend. |
| `GET`  | `/supporters` | – | Public list for the page. `?include_lapsed=true` to show lapsed subscribers; `?limit=N` to cap. |
| `GET`  | `/supporters/stats` | – | `{ supporter_count, total_raised, currency }` for a page header. |

`GET /supporters` returns (most-recent payment first, **never any email**):

```jsonc
{
  "success": true,
  "count": 2,
  "supporters": [
    {
      "name": "Jo Example",          // from_name, or "Anonymous"
      "message": "Good luck Lumi!",  // the supporter's public message (may be null)
      "total_amount": 9.0,           // summed across this supporter's payments
      "currency": "USD",
      "is_subscription": true,       // monthly member vs. one-time tipper
      "tier_name": "Bronze",         // membership tier (null for one-off tips)
      "type": "Subscription",        // latest event type
      "contribution_count": 3,
      "first_seen_at": "2026-03-01T10:00:00Z",
      "last_payment_at": "2026-06-01T10:00:00Z"
    }
  ]
}
```

### Expand / shrink behaviour

- **Expand** — automatic: each new payment upserts the supporter (a subscription
  renewal just advances their `last_payment_at`).
- **Shrink** — Ko-fi gives no cancellation event, so a *subscriber* is listed only
  while their last payment is within `KOFI_ACTIVE_WINDOW_DAYS` (default **35** =
  one cycle + grace). One-time tippers are kept forever. Pass `?include_lapsed=true`
  to override. Identity is grouped by email (server-side only), falling back to
  display name, so a member's monthly payments collapse into one entry.

> Supporters who chose to keep a donation private (`is_public = false` on Ko-fi)
> are recorded but excluded from the public list.

---

## 🏗 Architecture

- **`api.py`**: Main entry point, routing, and lifecycle management.
- **`db_pool.py`**: Shared psycopg 3 PostgreSQL connection pool used by every DB caller.
- **`metadata_engine/`**: Handles the complex mapping between TMDB and AniList. See its [README](metadata_engine/README.md) for details.
- **`scrapers/`**: Modular providers that find video embeds on streaming sites.
- **`resolvers/`**: Tools that extract raw video links from those embeds.
- **`account_engine/`**: Mnemonic (Ed25519) sign-in, favorites and watch progress. Self-contained crypto (`ed25519.py`, no native deps), PostgreSQL store (`db.py`, via the shared `db_pool`), and the API router (`routes.py`).
- **`supporters_engine/`**: Ko-fi supporters ("Lumi's Loved Mortals"). Webhook ingest into an append-only ledger plus the derived public list, as a PostgreSQL store (`db.py`) + API router (`routes.py`) — same shape as the account engine.
- **`player.py`**: The logic for our built-in HTML5 video player.

### Extending the Engine
To add a new source, implement a new scraper in `scrapers/` (inheriting from `BaseAnimeScraper`) and, if needed, a resolver in `resolvers/` (inheriting from `BaseResolver`).

---

## 🌹 TL;DR
Lumi's FastAPI backend for Crimson Haven. It maps TMDB to AniList, scrapes multiple sources, resolves direct links, and proxies streams to keep your viewing experience pure and ad-free. Set your key, run it, and enjoy! ( ^ ▿ ^ )
