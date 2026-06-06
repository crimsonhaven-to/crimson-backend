# Crimson Backend

Greetings from the heart of Crimson Haven! I am Luminas Crimsonveil—your curator of all things anime. You may call me Lumi ( ^ . ^ )

This is the robust, high-performance engine powering our streaming sanctuary. It handles multi-season anime mapping, automated metadata aggregation, and multi-source stream resolution with elegance and speed.

## 🩸 Core Features

- **Multi-Season Intelligence**: Automatically maps TMDB TV shows and seasons to their corresponding AniList IDs using the Fribb dataset.
- **Unified Search**: Search across TMDB with automatic suggestions and metadata enrichment.
- **Smart Metadata**: Aggregates data from TMDB and AniList for complete info (titles, posters, episode summaries).
- **Advanced Scraping**: Multi-threaded, async scraping from various sources (AnimeKai, AnimeSuge, GogoAnime, etc.).
- **Stream Resolution**: Resolves third-party embed URLs to direct HLS/MP4 streams or ad-free proxied players.
- **Automatic Sync**: Built-in scheduler keeps the local mapping database up-to-date with upstream sources.
- **Internal Proxies**: Custom reverse proxies for providers like VidKing, Movish, and Jellyfin to bypass ads and CORS.
- **Crimson Player**: A minimal, ad-free HLS/MP4 player served directly from the backend for a seamless experience.

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **Database**: SQLite (Metadata Mapping & API Cache)
- **Networking**: HTTPX (Async)
- **Parsing**: BeautifulSoup4, Selectolax, lxml
- **Scheduling**: APScheduler
- **Containerization**: Docker & Docker Compose

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**
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
   DEBUG=False
   ```

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
| `PROXY_SECRET` | prod | random | HMAC secret for the signed stream proxies. **Must be stable and shared across replicas** or proxied playback 403s. `openssl rand -hex 32`. |
| `MAPPING_DB` | no | `anime_mappings.db` | Path to the TMDB↔AniList mapping + cache DB. |
| `ACCOUNTS_DB` | no | `accounts.db` | Path to the accounts/favorites/progress DB (kept separate so user data survives mapping resyncs). |
| `RUN_DB_SYNC` | no | `true` | Whether this instance runs the periodic Fribb resync. Set `false` on all but one replica. |
| `SQLITE_BUSY_TIMEOUT` | no | `30` | Seconds a sqlite call waits on a locked DB before erroring. |
| `ALLOWED_ORIGINS` | no | built-in list | Comma-separated CORS origins. |
| `DEBUG` | no | unset | When truthy, includes exception detail in 500 responses. Leave unset in production. |
| `JELLYFIN_URL` / `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD` | no | – | Enable the optional Jellyfin source. |

The two SQLite databases run in **WAL mode** with a busy timeout so the async
workers can read and write concurrently without `database is locked` errors.

---

## 🐳 Deployment

### Docker (single host)

```bash
docker build -t crimson-backend:1.0 .
# Provide secrets via an env file or the shell environment:
TMDB_API_KEY=... PROXY_SECRET=$(openssl rand -hex 32) docker compose up -d
```

The container runs as a non-root user, ships a `HEALTHCHECK` that polls
`/health`, and persists both SQLite DBs to the `crimson-data` named volume.

### Deploying to Docker Swarm

The provided [`docker-compose.yml`](docker-compose.yml) is Swarm-ready
(`deploy:` block with restart policy, resource limits and `stop-first` updates):

```bash
docker stack deploy -c docker-compose.yml crimson
```

> [!IMPORTANT]
> **State lives in SQLite, which is single-writer and cannot be shared across
> hosts.** The stack therefore runs as a **single replica pinned to one node**
> (the `placement.constraints` entry) with a local named volume. This gives you
> Swarm's self-healing (auto-restart/reschedule on that node) and rolling
> updates without risking DB corruption.
>
> **To run more than one replica:**
> 1. Keep them on the **same node**, sharing the same local volume (WAL allows
>    concurrent same-host access).
> 2. Set `RUN_DB_SYNC=true` on **exactly one** replica, `false` on the rest, so
>    the wholesale mapping rebuild runs once.
> 3. Set the **same `PROXY_SECRET`** on every replica so signed proxy URLs
>    verify regardless of which replica serves the follow-up request.
>
> **True multi-node horizontal scaling** requires replacing SQLite with a
> networked database (e.g. Postgres) for `accounts.db` + the mapping/cache
> tables — the access layer is small and isolated in `account_engine/db.py`,
> `metadata_engine/db_handler.py` and the helpers in `api.py`.

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
| `GET` | `/season/{tmdb_id}/{num}` | Get metadata for a specific season. |
| `GET` | `/watch/{tmdb_id}/{s}/{e}` | **Primary**: Retrieve streaming links for an episode. |
| `GET` | `/anilist/{id}` | Reverse lookup (AniList ID -> TMDB ID). |

### Internal Proxies & Utilities

- **`/player`**: Renders the Crimson Player for direct HLS/MP4 streams.
- **`/vidking_proxy`**: Removes ads and pop-unders from VidKing/Videasy embeds.
- **`/movish_proxy`**: Proxies Movish streams to handle headers/CORS.
- **`/playimdb_proxy`**: Signed HLS proxy for the PlayIMDb source (injects the referer the PlayIMDb CDNs require; the raw stream is extracted server-side so no PlayIMDb player/ad code is ever loaded).
- **`/jellyfin_proxy`**: Proxies Jellyfin HLS segments for same-origin playback.
- **`/health`**: Check system status and database health.

---

## 🔐 Accounts (mnemonic sign-in)

No usernames, no passwords. An account **is** an Ed25519 public key derived from a
12-word BIP39 mnemonic that lives entirely on the client (like P-Stream). The
server stores only the public key and *verifies* signatures over one-time
challenges — the mnemonic / private key never reach the backend, so a DB leak
exposes no credential. User data (favorites + watch progress) lives in a separate
`accounts.db` that survives mapping resyncs.

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
| `GET`  | `/account/continue-watching` | Bearer | In-progress episodes, most recent first. |

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

## 🏗 Architecture

- **`api.py`**: Main entry point, routing, and lifecycle management.
- **`metadata_engine/`**: Handles the complex mapping between TMDB and AniList. See its [README](metadata_engine/README.md) for details.
- **`scrapers/`**: Modular providers that find video embeds on streaming sites.
- **`resolvers/`**: Tools that extract raw video links from those embeds.
- **`account_engine/`**: Mnemonic (Ed25519) sign-in, favorites and watch progress. Self-contained crypto (`ed25519.py`, no native deps), SQLite store (`db.py`), and the API router (`routes.py`).
- **`player.py`**: The logic for our built-in HTML5 video player.

### Extending the Engine
To add a new source, implement a new scraper in `scrapers/` (inheriting from `BaseAnimeScraper`) and, if needed, a resolver in `resolvers/` (inheriting from `BaseResolver`).

---

## 🌹 TL;DR
Lumi's FastAPI backend for Crimson Haven. It maps TMDB to AniList, scrapes multiple sources, resolves direct links, and proxies streams to keep your viewing experience pure and ad-free. Set your key, run it, and enjoy! ( ^ ▿ ^ )
