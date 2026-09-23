# Architecture: brain, not pipe

The backend keeps what must live in one trusted place: the TMDB to AniList
metadata, accounts and the login wall, the signing secrets, and orchestration.
It is not the pipe video bytes flow through. Third-party sources are scraped,
resolved and delivered in the viewer's own browser, so backend cost tracks
library size and member count, not watch hours. The full walkthrough is on the
docs site: <https://docs.crimsonhaven.org/architecture/new-system/>.

## Who does what

| Repo | Role |
| --- | --- |
| `crimson-backend` | E0. Metadata, accounts, login wall, signing, the operator-owned sources (Local, Cache, Jellyfin) and the `/watch` NDJSON pipeline. The floor: anything a client cannot run, the backend still can. |
| `crimson-sources` | The TypeScript scrape and resolve engine that runs in the browser and emits the same `/watch` line the backend does. |
| `crimson-proxy` | E2. A signed-only CORS relay at the edge that injects headers and relays HLS segments. |
| `crimson-extension` | E3. An MV3 companion that rewrites forbidden headers and bypasses CORS from a real browser on a residential IP. |

## Environments

A source runs in the cheapest environment that clears the walls it faces, with
E0 always available as the fallback.

| Env | Clears | Cannot |
| --- | --- | --- |
| E0 backend | everything, including server-held secrets | keep bytes off the backend |
| E1 direct browser fetch | TLS fingerprinting, IP-bound tokens | CORS, forbidden headers |
| E2 edge relay | CORS, forbidden headers | TLS fingerprinting, IP-bound tokens |
| E3 extension | CORS, headers, fingerprinting, IP-bound tokens | server-held secrets |

Routing order: E3, then E1 with E2, then E2, then E0.

## The wire contract

The player reads one NDJSON line shape, defined in
`contracts/watch_ndjson.schema.json` and built by `core/contracts.py`. The
browser engine emits the same lines as the backend, so the player cannot tell
which side resolved a stream. A stream the client resolved can still get a
`cacheTicket`: minting is keyed on the stream descriptor, not on having carried
its bytes.

## The grants

The only things the backend hands the client engine: none carries a secret or a
video byte.

| Endpoint | Returns | Why the client cannot do it |
| --- | --- | --- |
| `GET /scrape-meta/{tmdb}/{season}`, `GET /scrape-meta/movie/{tmdb}` | titles, synonyms, release year, IMDb id | needs the server-held TMDB key |
| `POST /sign` | a signed crimson-proxy link for an upstream URL and its headers | `PROXY_SECRET` never reaches a browser; 503 without a configured proxy |
| `POST /resolve` | the raw stream of a secret-bound source, such as Jellyfin | the lookup needs a server secret; the bytes still skip the backend |

Signing is per playlist, not per segment: the edge re-signs a playlist's
sub-resources as it relays them.

## Trust boundary

1. The relay only serves HMAC-signed links, minted by the backend through the
   authenticated, rate-limited `/sign`. The secret never leaves the backend.
2. Server-held secrets stay at E0, or are injected at the edge
   (`JELLYFIN_EDGE_INJECT`), never in the client bundle.
3. With `REQUIRE_LOGIN` on (the default) every grant and metadata endpoint is
   members-only, so the engine cannot become a free scraping service.
4. The grants carry the same rate limits as `/watch`, and the relay checks every
   upstream against its SSRF guard.
