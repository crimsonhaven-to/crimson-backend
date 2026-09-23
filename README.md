# Crimson Backend

The backend of Crimson Haven: TMDB to AniList metadata, accounts and the
members-only login wall, recommendations, the admin dashboard, and the
progressive NDJSON `/watch` stream. It is the brain, not the pipe: third-party
sources are scraped and played in the viewer's browser by
[`crimson-sources`](../crimson-sources), and this repository hosts, embeds and
ships none of them. The only media it serves itself is operator-owned: Local
(your own directories), Cache (episodes this server remuxed onto your NAS) and
Jellyfin (your own server). See [`docs/architecture.md`](docs/architecture.md).

## Stack

FastAPI on Python 3.14, PostgreSQL through a psycopg 3 pool, httpx, APScheduler,
slowapi, pydantic-settings, Prometheus metrics. Docker Compose for one host,
Docker Swarm in production.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # set TMDB_API_KEY; REQUIRE_LOGIN=false for a quick look
uvicorn api:app --reload
```

Needs a PostgreSQL the configured user can create tables in (or
`docker compose up -d`, which bundles one). The schema is created at startup:
each store's `init_db()` builds the baseline and `migrations/*.sql` are applied in
order. Interactive docs are at `/docs`.

```bash
pytest          # no network, no database, a few seconds
ruff check .
mypy .
```

`openapi.json` is generated: `python scripts/export_openapi.py` after changing a
route.

## Configuration

Every setting is an environment variable read once by `core/config.py`;
[`.env.example`](.env.example) lists all of them with defaults. Only
`TMDB_API_KEY` is required. Features whose keys are unset switch themselves off,
and the startup log reports which ones are live. Compose and Swarm inject only the
variables a service lists under `environment:`; `.env` is used for `${...}`
substitution, not injected.

## Layout

Each engine owns its routes, its logic and its data. Routes import logic, logic
imports data, and nothing imports upward.

| Package | Owns |
| --- | --- |
| `api.py`, `startup.py` | App assembly; what the lifespan starts, and which replica runs which job |
| `core/` | Settings, the database pool, the shared HTTP client, response cache, signing, metrics, middleware, migrations, the `/watch` contract |
| `account_engine/` | Sign-in (Ed25519 mnemonic and email), profile, favorites, watch progress, the login wall, security ledger, Wrapped, and the account admin |
| `metadata_engine/` | The Fribb mapping sync, TMDB and AniList clients, title pages, search, browse catalogues, metadata maintenance |
| `playback_engine/` | The scrape and resolve pipeline, `/watch`, the client grants, the movie-web bridge, stream relays, source health |
| `scrapers/`, `resolvers/` | The operator-owned sources and their contract; a build-time overlay can add private ones |
| `local_engine/`, `cache_engine/`, `download_engine/` | The local library (with on-the-fly HLS), the server-side cache, admin downloads through aria2 |
| `system_engine/` | `/`, `/config`, `/health`, `/metrics`, and the admin System and Metrics tabs |
| `chat_engine/` | Lumi, the permission-gated chatbot |
| `notify_engine/` | The airing calendar, follows and airing emails |
| `recommend_engine/`, `manga_engine/`, `iptv_engine/`, `subtitles_engine/`, `skiptimes_engine/` | Recommendations, manga, Live TV, OpenSubtitles tracks, AniSkip timestamps |
| `supporters_engine/`, `changelog_engine/`, `apikey_engine/`, `telemetry_engine/` | Ko-fi supporters, the GitHub Releases changelog, movie-web bridge keys, client resolve beacons |
| `discord_bot/` | A separate process that mints single-use invite codes |
| `deploy/` | Patroni HA Postgres, PgBouncer, Prometheus |

## The `/watch` stream

`GET /watch/{tmdb_id}/{season}/{episode}` and `GET /watch/movie/{tmdb_id}` answer
`application/x-ndjson`, one object per line, flushed as each source resolves so
the fastest plays first:

```jsonc
{"type":"meta","success":true,"tmdb_id":1234,"season_number":1,"episode_number":1,"anilist_id":567,"title":"..."}
{"type":"stream","source":"Jellyfin","streamType":"hls","url":"https://<backend>/jellyfin_proxy/...","language":null,"cacheTicket":"..."}
{"type":"done","count":1}
```

An episode that has not aired yet answers `meta`, `unaired`, `done`. The shape is
pinned by `contracts/watch_ndjson.schema.json`; the browser engine emits the same
lines for the sources it resolves, and the client dedupes both by
`(source, language)`. The grants the browser engine needs (`/scrape-meta`,
`/sign`, `/resolve`) are described in `docs/architecture.md`.

## Accounts

Two sign-in methods, both invite-gated at registration (`SIGNUP_INVITE_CODE`, or a
single-use code from the Discord bot or the admin dashboard):

- **Mnemonic.** The account is an Ed25519 public key derived in the browser from a
  12-word mnemonic; the server only verifies signatures over one-time challenges.
- **Email and password.** PBKDF2-HMAC-SHA256, and the address must be verified
  before the first sign-in.

Both return a session token for `Authorization: Bearer`. With `REQUIRE_LOGIN` on
(the default) every other endpoint needs one, except the auth routes, health and
config, the Ko-fi webhook, and the stream relays (loaded by `<video>`, `<img>` and
hls.js, which cannot send a header; each is signed or bound to an enabled root).
The exact list is `account_engine/login_wall.py`, pinned by `tests/test_api.py`.

The client must derive keys exactly like this:

```text
mnemonic  : 12 BIP39 English words (128-bit entropy)
seed      : PBKDF2-HMAC-SHA512(mnemonic, "mnemonic"+passphrase, 2048, dklen=64)
privSeed  : seed[:32]
keypair   : Ed25519 from privSeed          (RFC 8032, same as @noble/ed25519)
public_key: hex(publicKey)                 (64 lowercase hex chars), the account id
```

```js
import { generateMnemonic, mnemonicToSeedSync } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english';
import * as ed from '@noble/ed25519';

const mnemonic = generateMnemonic(wordlist);
const seed = mnemonicToSeedSync(mnemonic).slice(0, 32);
const publicKey = Buffer.from(await ed.getPublicKeyAsync(seed)).toString('hex');

const { challenge } = await (await fetch('/auth/challenge', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ public_key: publicKey }),
})).json();
const signature = Buffer.from(
  await ed.signAsync(new TextEncoder().encode(challenge), seed)).toString('hex');
const res = await fetch('/auth/login', {   // or /auth/register with invite_code
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ public_key: publicKey, challenge, signature }),
});
const { session_token } = await res.json();
```

## Deployment

```bash
cp .env.example .env                       # set TMDB_API_KEY, PROXY_SECRET
docker compose up -d --build               # one host, bundled Postgres
docker stack deploy -c docker-stack.yml crimson
```

| File | Use |
| --- | --- |
| `docker-compose.yml` | one host; `--profile downloads` adds aria2, `--profile discord` the bot |
| `docker-compose.demo.yml` | source-less demo, never reads `.env` |
| `docker-stack.yml` | production Swarm reference; the live copy is on the manager |
| `deploy/` | Patroni, PgBouncer and Prometheus, each with its own README |

All state is in PostgreSQL, so API replicas are interchangeable. With more than
one replica:

| Setting | Rule |
| --- | --- |
| `RUN_DB_SYNC` | `true` on exactly one replica |
| `RUN_CACHE_WORKER`, `RUN_DOWNLOAD_WORKER` | `true` only on their dedicated worker services |
| `PROXY_SECRET` | identical everywhere, or signed links fail on the next replica |

Run behind a TLS-terminating proxy that sets `X-Forwarded-Proto` and
`X-Forwarded-Host`; otherwise stream URLs come out as `http://` and are blocked as
mixed content. CI (`.gitlab-ci.yml`) builds on every push to `main` and deploys
the dev stack; a `v*` tag builds, deploys production and the source-less demo.
CI only runs `deploy.sh` on the manager and never ships a stack file, so a change
to `docker-stack.yml` must be applied there by hand.

## Adding a source

A third-party site does not belong here: add it to `crimson-sources`, where it
runs in the viewer's browser. An operator-owned source (a media server you
control) is a scraper and a resolver: subclass `BaseAnimeScraper` and
`BaseResolver` (their docstrings define the contract), emit a
`crimson-<name>:<token>` marker, and register both in `scrapers/__init__.py` and
`resolvers/__init__.py`. `local_scraper.py` with `resolvers/local.py` is a complete
example.

## Ko-fi supporters

Ko-fi only pushes a webhook per payment, so the backend keeps an append-only ledger
and derives the list. Set `KOFI_VERIFICATION_TOKEN` to the token under Ko-fi,
Settings, Advanced, and point the webhook at `https://<backend>/kofi/webhook`. A
subscriber is listed while their last payment is within `KOFI_ACTIVE_WINDOW_DAYS`;
one-time tippers stay; private donations are recorded but not listed.

## License

MIT, see [`LICENSE`](LICENSE). A link back to
[crimsonhaven-to](https://gitlab.ramon.moe/crimsonhaven-to) in anything built on
this is appreciated, not required.
