# Crimson Backend

Greetings from the heart of Crimson Haven! I am Luminas Crimsonveil—your curator of all things anime. You may call me Lumi ( ^ . ^ )

This is the **brain** of our streaming sanctuary: TMDB↔AniList metadata, accounts
& the members-only login wall, recommendations, supporters, the admin dashboard,
and the orchestration that ties it all together — served fast over a progressive
NDJSON `/watch` stream.

> ## 🩸 A word from Lumi — the disclaimer, made crimson
>
> Let me be perfectly clear, little mortal: this is — at its heart — an **expansive,
> performance-oriented streaming *framework*** with a metadata brain that natively
> maps **TMDB ↔ AniList** (per-season, with graceful TMDB fallback), runs accounts
> and a members-only login wall, recommendations, supporters and an admin dashboard,
> and orchestrates it all over a progressive NDJSON `/watch` stream. That is what it
> *is*, and it does it beautifully.
>
> What it is **not** is a pirate ship. Crimson Backend does **not host, store, embed,
> or ship any sources** — there are no third-party scrapers, no resolvers, no
> playlists and no pre-loaded streams anywhere in this repository, and I do **not**
> condone piracy. It is a neutral, hollow vessel; what (lawfully) fills it is entirely
> yours to decide.
>
> The only media the backend ever serves is **operator-owned** — things *you*, the
> operator, already control: **Local** (your own NAS / bind-mounted directories),
> **Cache** (episodes this server already remuxed onto your own NAS), and **Jellyfin**
> (your own self-hosted media server). Plus one inert, documented **template** source
> that shows the contract for wiring in another operator-owned source. Should you ever
> add your own third-party providers, they live in a **private** repository of your
> own and resolve **client-side**, in the viewer's browser — helped by the optional
> [companion extension](../crimson-extension) and/or the
> [`crimson-proxy`](../crimson-proxy) edge relay. Albeit sources can be added into the
> backend, it is not advisable to do so since the streams usually require to be proxied
> through this very backend, and bandwidth is both hard to scale and quite expensive~
>
> See [`New_System.md`](New_System.md) for the full architecture and
> [`New_System_Progress.md`](New_System_Progress.md) for the build log.

## 🩸 Core Features

- **Multi-Season Intelligence**: Maps TMDB TV shows/seasons to their AniList IDs using the Fribb dataset.
- **Unified Search & Metadata**: Searches TMDB and enriches with AniList (titles, posters, episode summaries) for anime, non-anime TV, and movies.
- **Progressive `/watch`**: streams resolved sources as **NDJSON** — each source is flushed to the client the instant it resolves, so the fastest plays first.
- **Operator-owned sources**: a generic scrape→resolve pipeline that now drives only **Local**, **Cache**, and **Jellyfin** (all server-operator media), plus a template.
- **Client-offload grants**: small login-gated endpoints (`/scrape-meta`, `/sign`, `/resolve`) that hand the client engine exactly what it can't derive — without ever leaking a server-held secret.
- **Crimson Player**: a minimal, ad-free HLS/MP4 player served from the backend for the operator-owned (and any iframe) streams.
- **Accounts & Login Wall**: two coexisting sign-in methods — Ed25519 mnemonic (P-Stream style) **and** invite-gated email + password with SMTP verify/reset — behind a site-wide, opt-out login wall so the whole API is members-only.
- **Extras**: recommendations, Ko-fi supporters, a public changelog from GitHub Releases, OpenSubtitles tracks, AniSkip skip-times, an admin dashboard, and a server-side video cache.
- **Rate Limiting**: built-in protection on sensitive endpoints (`slowapi`).

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.10+, runs on 3.14-slim in the image)
- **Database**: PostgreSQL (metadata mapping, API cache & accounts), pooled via psycopg 3
- **Networking**: HTTPX (async); the operator-owned sources use plain server-to-server fetches (no third-party impersonation)
- **Scheduling**: APScheduler
- **Rate Limiting**: slowapi (token bucket)
- **Containerization**: Docker & Docker Compose / Swarm

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**
- **PostgreSQL**: a reachable database (run one locally, or use the bundled `postgres` service via `docker compose`).
- **TMDB API Key**: required for metadata and search (legacy API key / v4 Read Access Token).

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/crimsonhaven-to/crimson-backend.git
   cd crimson-backend
   ```

2. **Create a virtualenv and install dependencies**:
   ```bash
   python -m venv .venv
   . .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   # for tests / linting:  pip install -r requirements-dev.txt
   ```

3. **Configure environment**: copy [`.env.example`](.env.example) to `.env` and fill in at least:
   ```env
   TMDB_API_KEY=your_tmdb_api_key_here
   DATABASE_URL=postgresql://crimson:crimson@localhost:5432/crimson
   # For a quick local spin-up you can also disable the login wall:
   # REQUIRE_LOGIN=false
   ```
   The database schema is created automatically on startup (idempotent
   `CREATE TABLE / ALTER … IF NOT EXISTS` migrations); you only need an empty
   database and a user that can create tables.

### Running the API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

The API is at `http://localhost:8000`; interactive docs at `/docs`. The committed
[`openapi.json`](openapi.json) is the generated schema — regenerate it after route
changes with `python scripts/export_openapi.py`.

### Running the tests

```bash
pytest -q
```

The suite covers the canonical contracts (`tests/test_contracts.py` asserts the
app imports and its OpenAPI schema generates), the proxy-signing + SSRF guards,
the config report and telemetry.

---

## ⚙️ Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)
for the fully-commented list). The most relevant:

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `TMDB_API_KEY` | **yes** | – | TMDB Read Access Token (v4) or legacy key. |
| `DATABASE_URL` | prod | – | Full PostgreSQL URL, e.g. `postgresql://crimson:crimson@postgres:5432/crimson`. Takes precedence over the discrete `POSTGRES_*` parts. |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | no | `localhost` / `5432` / `crimson` ×3 | Used to assemble the connection when `DATABASE_URL` is unset. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | no | `1` / `10` | Connection-pool sizing. |
| `RUN_DB_SYNC` | no | `true` | Whether this instance runs the periodic Fribb resync. Set `false` on all but one replica. |
| `REQUIRE_LOGIN` | no | `true` | Site-wide login wall — require a valid session on all content endpoints. `false` reopens the API. |
| `SIGNUP_INVITE_CODE` | no | – | Shared, **reusable** invite code(s) gating **both** email and mnemonic registration. **Empty ⇒ signups closed** (every register `403`s) — unless a single-use Discord-bot token is used instead. |
| `PROXY_SECRET` | for `/sign` | random | HMAC secret shared with [`crimson-proxy`](../crimson-proxy). Signs the `/sign` client links, `/subtitles_proxy`, and cache tickets. **Must be stable + identical across replicas (and equal to each proxy's `NITRO_PROXY_SECRET`)** or signed playback 403s. `openssl rand -hex 32`. |
| `CRIMSON_PROXY_BASE` | no | – | Comma-separated [`crimson-proxy`](../crimson-proxy) edge origin(s). When set, `POST /sign` mints **signed edge links** for client-resolved streams (segment bytes skip the backend). Unset ⇒ `/sign` returns 503 and the client stays on the extension (E3) or backend (E0). |
| `JELLYFIN_URL` / `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD` | no | – | Enable the optional **Jellyfin** source (your own server; reachable from the backend). |
| `JELLYFIN_EDGE_INJECT` | no | `off` | Deliver Jellyfin off-backend via crimson-proxy **edge token injection** instead of the backend `/jellyfin_proxy`. Requires the proxy deployed with `NITRO_JELLYFIN_*`. Off ⇒ backend-proxied (default, no regression). |
| `ALLOWED_ORIGINS` | no | built-in list | Comma-separated CORS origins (e.g. `https://crimsonhaven.to`). |
| `RATE_LIMIT_STORAGE_URI` | no | `memory://` | Rate-limit backend; `redis://…` to share limits across replicas. |
| `DEBUG` | no | unset | When truthy, includes exception detail in 500 responses. Leave unset in production. |

Other optional integrations (each self-disables when unset): the **Discord invite
bot** (`DISCORD_BOT_TOKEN` / `DISCORD_OWNER_ID`), **SMTP** for verify/reset email
(`SMTP_*`, `FRONTEND_BASE_URL`), **Ko-fi supporters** (`KOFI_VERIFICATION_TOKEN`),
the public **changelog** (`GITHUB_TOKEN` / `GITHUB_REPO`), **OpenSubtitles**
(`OPENSUBTITLES_API_KEY`), and the **server-side cache** worker (`RUN_CACHE_WORKER`
+ `CACHE_*`). All are documented inline in `.env.example`.

> **Containers:** Compose/Swarm only inject env vars **explicitly listed** in a
> service's `environment:` block — the `.env` file is used for `${...}`
> substitution, *not* auto-injected. Recreate the container after changing them.

All state (the TMDB↔AniList mapping, the API cache, and accounts/favorites/
progress) lives in **one PostgreSQL database**, reached through a process-wide
psycopg connection pool (`db_pool.py`). A Fribb resync only rebuilds the mapping
tables inside a transaction, so it never touches user data. Because the API holds
no local state, replicas are interchangeable and can all point at the same database.

---

## 🐳 Deployment

### Docker (single host)

```bash
docker build -t crimson-backend:1.0 .
# Provide secrets via an env file or the shell environment:
TMDB_API_KEY=... PROXY_SECRET=$(openssl rand -hex 32) docker compose up -d
```

The Compose file brings up a bundled **`postgres:17-alpine`** service (data on the
`crimson-pgdata` named volume) and the stateless API container, which runs as a
non-root user and ships a `HEALTHCHECK` that polls `/health`. The API waits for
Postgres to report healthy (`depends_on`) before starting.

> [!NOTE]
> The bundled `postgres` service is meant for **dev / single-host** use. In
> production, point `DATABASE_URL` at a managed or externally-operated PostgreSQL
> instance and drop the bundled service — the API needs no writable volume of its own.

### Docker Swarm

The provided [`docker-stack.yml`](docker-stack.yml) is Swarm-ready (restart policy,
resource limits, `stop-first` updates):

```bash
docker stack deploy -c docker-stack.yml crimson
```

> [!IMPORTANT]
> **State lives in PostgreSQL, so the API is stateless and scales across nodes.**
> When running more than one replica:
> 1. Set `RUN_DB_SYNC=true` on **exactly one** replica, `false` on the rest, so the
>    wholesale mapping rebuild runs once.
> 2. Set the **same `PROXY_SECRET`** on every replica so signed links verify
>    regardless of which replica serves the follow-up request.
> 3. Run the cache downloader on **one** dedicated worker (`RUN_CACHE_WORKER=true`
>    there, `false` everywhere else).

The stack must sit behind a TLS-terminating reverse proxy that sets
`X-Forwarded-Proto`/`X-Forwarded-Host` (uvicorn runs with `--proxy-headers`),
otherwise the absolute stream URLs the backend emits come out as `http://` and get
blocked as mixed content on the HTTPS frontend.

`deploy/` holds the production ops notes (Patroni HA Postgres, pgBackRest B2
backups, PgBouncer pooling); the CI/CD pipeline builds + pushes a private GHCR
image and auto-deploys to the swarm on a tagged release.

---

## 📜 API Reference

### Core content endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/search/anime` · `/search/shows` · `/search/movies` | Search by name (TMDB-based). |
| `GET` | `/trending` · `/trending/shows` · `/trending/movies` | Popular titles. |
| `GET` | `/catalogue` | All mapped anime in the local DB. |
| `GET` | `/show/{tmdb_id}` · `/info/{tmdb_id}?season=` | Show details / flat merged season metadata. |
| `GET` | `/seasons/{anilist_id}` · `/season/{tmdb_id}/{num}` | Season list / single-season metadata. |
| `GET` | `/overview/{anilist_id}` · `/show-overview/{tmdb_id}` · `/movie-overview/{tmdb_id}` | Aggregated metadata + seasons in one round-trip. |
| `GET` | `/watch/{tmdb_id}/{s}/{e}` · `/watch/movie/{tmdb_id}` | **Primary**: stream episode/movie links progressively as **NDJSON**. |
| `GET` | `/recommendations` · `/recommendations/similar/{anilist_id}` | Genre-based "watch next". |
| `GET` | `/anilist/{id}` | Reverse lookup (AniList ID → TMDB ID). |

### Progressive `/watch` (NDJSON streaming)

`GET /watch/{tmdb_id}/{season}/{episode}` does **not** return a single JSON body.
It responds with `Content-Type: application/x-ndjson` and emits **one JSON object
per line, flushed as each source resolves** — read the body incrementally and
surface each source the moment it arrives.

```jsonc
{"type":"meta","success":true,"tmdb_id":1234,"season_number":1,"episode_number":1,"anilist_id":567,"title":"..."}
{"type":"stream","source":"Jellyfin","streamType":"hls","url":"https://<backend>/jellyfin_proxy/...","language":null}
{"type":"stream","source":"Crimson Vault","streamType":"mp4","url":"https://<backend>/cache_proxy/<token>","language":"German Dub"}
{"type":"done","count":2}
```

- `meta` — always first (ids + resolved title).
- `stream` — zero or more; `streamType` is `hls` | `mp4` | `iframe`. The
  **client-side engine** ([`crimson-sources`](../crimson-sources)) emits the
  **byte-identical** line shape for the sources it resolves in the browser, and the
  frontend dedupes the two streams by `(source, language)` — so a backend
  (operator-owned) source and a client-resolved third-party source coexist in one list.
- `done` — terminal; `count` = number of `stream` lines emitted.

Each operator-owned source runs as its own task (search → embeds → resolve → emit);
a shared seen-set de-dupes across sources, and disconnecting cancels in-flight tasks.
The response carries `X-Accel-Buffering: no` so reverse proxies flush lines through.

### Operator-owned stream proxies & utilities

These are the **only** stream proxies the backend serves — all for media the
operator controls:

- **`/player`** — renders the Crimson Player for a direct HLS/MP4 (or iframe) stream.
- **`/jellyfin_proxy/{path}`** — authenticated reverse proxy to your own Jellyfin server. Injects the access token **server-side** (it never reaches the browser) and rewrites HLS playlists to flow back through the proxy.
- **`/local_proxy/{token}`** — streams a browser-playable file from an admin-registered local directory / NAS mount (direct play, HTTP Range supported).
- **`/cache_proxy/{token}`** — streams a server-side-cached (remuxed) episode straight off the NAS. Mirrors `/local_proxy`.
- **`/health`** — system + database status.

### Client-side offload grants (New System)

The backend stays the **brain** (metadata, identity, secret custody) but stays out
of the **byte path** for third-party sources: the [`crimson-sources`](../crimson-sources)
engine in the viewer's browser scrapes/resolves them and streams `CDN → viewer`.
The backend exposes small **grants** that hand the client only what it can't derive.
All are login-gated like `/watch`.

| Method | Endpoint | Purpose |
| :--- | :--- | :--- |
| `GET` | `/scrape-meta/{tmdb}/{season}` | Title bundle for the title-keyed client sources — AniList/TMDB titles + German synonyms, **+ `release_year` + `imdb_id`** (year/imdb come from the server-held TMDB key). |
| `GET` | `/scrape-meta/movie/{tmdb}` | Movie twin of the above for the Western movie sources. |
| `POST` | `/sign` | Mint a **signed crimson-proxy URL** (E2) for a header-only source — `PROXY_SECRET` never reaches the browser. 503 when no proxy is configured. |
| `POST` | `/resolve` | Secret-bound **resolve grant**: runs a token-gated lookup server-side (e.g. the Jellyfin token) and returns the **raw** stream for the client to deliver via the edge/extension. |

> The execution-environment model (E0 backend → E3 extension) and which source
> lands where lives in [`New_System.md`](New_System.md).

---

## 🔐 Accounts

Two sign-in methods coexist; an account row carries **either** an Ed25519
`public_key` **or** an `email`/`password_hash`. User data (favorites + watch
progress) lives in its own PostgreSQL tables, untouched by mapping resyncs. The
schema upgrades itself on boot via idempotent migrations.

### Site-wide login wall

When `REQUIRE_LOGIN=true` (default) the site is **members-only**: a pure-ASGI
middleware (`LoginWallMiddleware`) rejects any request without a valid
`Authorization: Bearer <session_token>` with `401`, except a small whitelist — the
auth endpoints, `/health` + `/`, the Ko-fi webhook, and the operator-owned stream
proxies + `/player` (loaded by `<iframe>`/`<video>` which can't send headers; only
reachable via an authenticated `/watch`). Validated tokens are cached in-process
for 60s so the wall adds no DB hit on hot paths, and it sits *inside* CORS so 401s
still carry CORS headers. Set `REQUIRE_LOGIN=false` to reopen the API.

### Mnemonic (Ed25519) sign-in

No usernames, no passwords. An account **is** an Ed25519 public key derived from a
12-word BIP39 mnemonic that lives entirely on the client (like P-Stream). The
server stores only the public key and *verifies* signatures over one-time
challenges — the mnemonic / private key never reach the backend.

### Email + password sign-in

Registration is **invite-gated** (`SIGNUP_INVITE_CODE`, comma-separated; empty ⇒
signups closed → `403`) and requires email verification before the first login.
Passwords are hashed with PBKDF2-HMAC-SHA256 (600k iters, stdlib `hashlib`);
hashing and SMTP both run in a threadpool. Verification + reset links are emailed
(single-use, hashed tokens) via SMTP.

| Method | Endpoint | Auth | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/auth/email/register` | – | Create an invite-gated, unverified account → sends verification email. |
| `POST` | `/auth/email/login` | – | Email + password → session (`403` until verified). |
| `POST` | `/auth/email/verify` | – | Consume a verification token → marks verified **and** returns a session. |
| `POST` | `/auth/email/resend` | – | Resend the verification email (always `200`, no account-exists oracle). |
| `POST` | `/auth/email/forgot` · `/auth/email/reset` | – | Start / complete a password reset (always `200` on forgot). |

### Mnemonic + shared account endpoints

| Method | Endpoint | Auth | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/auth/challenge` | – | Get a one-time challenge for a `public_key`. |
| `POST` | `/auth/register` | – | Create the account (signed challenge proves key ownership) → session. **Invite-gated** like email signup. |
| `POST` | `/auth/login` | – | Log in via signed challenge → session. |
| `POST` | `/auth/logout` | Bearer | Revoke the current session. |
| `GET`  | `/account/me` | Bearer | Profile + favorite/progress counts (+ `is_admin`). |
| `GET`/`POST`/`DELETE` | `/account/favorites` | Bearer | List / add / remove a favorited title (+ `/export`, `/import`). |
| `GET`/`POST`/`DELETE` | `/account/progress` | Bearer | List / upsert / remove per-episode watch progress. |
| `GET`  | `/account/continue-watching` · `/account/recent` | Bearer | In-progress / recently-watched episodes. |

### Client key-derivation spec (the frontend must match this exactly)

```text
mnemonic  : 12 BIP39 English words (128-bit entropy)
seed      : PBKDF2-HMAC-SHA512(mnemonic, "mnemonic"+passphrase, 2048, dklen=64)
privSeed  : seed[:32]
keypair   : Ed25519 from privSeed          (RFC 8032; == @noble/ed25519)
public_key: hex(publicKey)                 (64 lowercase hex chars) → the account id
```

```js
import { generateMnemonic, mnemonicToSeedSync } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english';
import * as ed from '@noble/ed25519';

const mnemonic = generateMnemonic(wordlist);              // show once, user saves it
const seed = mnemonicToSeedSync(mnemonic).slice(0, 32);   // 32-byte private seed
const publicKey = Buffer.from(await ed.getPublicKeyAsync(seed)).toString('hex');

const { challenge } = await (await fetch('/auth/challenge',
  { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ public_key: publicKey }) })).json();
const signature = Buffer.from(
  await ed.signAsync(new TextEncoder().encode(challenge), seed)).toString('hex');
const res = await fetch('/auth/login',                    // or /auth/register (+ invite_code)
  { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ public_key: publicKey, challenge, signature }) });
const { session_token } = await res.json();
```

The Discord invite bot (`python -m discord_bot`) optionally mints **single-use**
invite tokens for one whitelisted operator — see `.env.example` and `discord_bot/`.

---

## ☕ Ko-fi Supporters ("Lumi's Loved Mortals")

A public list of Ko-fi supporters, fed automatically by Ko-fi's webhook — no email
scanning, no polling. Ko-fi only *pushes* a webhook on each payment (and never on
cancellation), so the backend keeps an append-only ledger and derives the public
list by aggregating it.

1. In Ko-fi → **Settings → Advanced → Webhooks / API**, copy the *Verification
   Token* into `KOFI_VERIFICATION_TOKEN` and set the **Webhook URL** to
   `https://<your-backend>/kofi/webhook`.
2. Every future payment then appears on `GET /supporters` (`?include_lapsed=true`
   to show lapsed subscribers; `?limit=N` to cap). `GET /supporters/stats` gives a
   header summary. Emails are never returned. A subscriber is listed only while
   their last payment is within `KOFI_ACTIVE_WINDOW_DAYS` (default 35); one-time
   tippers are kept forever; private donations are recorded but excluded.

---

## 🏗 Architecture

| Path | Role |
| :--- | :--- |
| `api.py` | Main entry point, routing, the `/watch` NDJSON pipeline, the login wall, and lifecycle. |
| `db_pool.py` | Shared psycopg 3 PostgreSQL connection pool used by every DB caller. |
| `metadata_engine/` | The TMDB↔AniList mapping/sync (Fribb dataset, `tmdb_seasons`/`tmdb_extras`, overrides). |
| `scrapers/` | The generic scraper contract + the **operator-owned** sources only: `local_scraper.py`, `cache_scraper.py`, `jellyfin_scraper.py`, and the documented no-op `template_scraper.py`. `ALL_SCRAPERS` is the registry. |
| `resolvers/` | The matching resolvers (`local.py`, `cache.py`, `jellyfin.py`, `template.py`) + the shared `_crimson_proxy.py` (signing/health for `/sign`), `_proxy_secret.py`, and `_ssrf_guard.py`. `ALL_RESOLVERS` is the registry. |
| `local_engine/` · `cache_engine/` | The Local source (registered dirs/NAS) and the server-side video cache (background ffmpeg remux + replay). |
| `account_engine/` | Mnemonic (Ed25519, vendored pure-Python crypto) + email/password sign-in, favorites, progress, and the admin dashboard. |
| `metadata_engine/`, `recommend_engine/`, `supporters_engine/`, `changelog_engine/`, `subtitles_engine/`, `skiptimes_engine/`, `telemetry_engine/`, `apikey_engine/` | The feature engines (each a small store + router). |
| `core/` | Cross-cutting helpers: the canonical `/watch` contract, source-health metadata, config report, player. |

### Adding a source

- **A third-party streaming site** → it does **not** belong in this backend. Add it
  to the private [`crimson-sources`](../crimson-sources) engine, where it runs in
  the viewer's browser (extension / proxy). The backend never scrapes third parties.
- **An operator-owned source** (another media server you control) → copy
  `scrapers/template_scraper.py` + `resolvers/template.py`, implement the two
  scraper methods + the resolver, register them in `scrapers/__init__.py` /
  `resolvers/__init__.py`, and emit a `crimson-<name>:<token>` marker. See
  `local_scraper.py` + `resolvers/local.py` for a complete worked example.

---

## 🌹 TL;DR

Lumi's FastAPI backend for Crimson Haven. It maps TMDB to AniList, runs accounts +
a members-only login wall, serves your **own** media (Local / Cache / Jellyfin) over
a progressive NDJSON `/watch` stream, and hands the client engine signed grants so
**third-party** scraping happens in the viewer's browser — never here. Set your key,
run it, and enjoy! ( ^ ▿ ^ )

---

## 📜 License

Released under the **MIT License** — see [`LICENSE`](LICENSE). In short: take it,
fork it, remix it, build something lovely with it. ( ˶ ˆ ᗜ ˆ ˶ )

A tiny request from Lumi, heart-to-heart 🩸 — the MIT license only asks that you
keep the copyright notice, but I'd *so* appreciate it if you also left a little
link back to the original home, [`crimsonhaven-to`](https://github.com/crimsonhaven-to),
in anything you build on top of this. It's not a legal demand, just a kindness
between mortals and curators — it helps others find their way home to the source,
and it makes my little undead heart flutter. Thank you for being wonderful! ( ^ . ^ )
