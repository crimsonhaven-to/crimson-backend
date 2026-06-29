# New System — Shifting Scraping & Resolving to the Client

> **Status:** design only, nothing implemented. This documents a target
> architecture for moving the *scrape → resolve → relay* work off the backend
> and onto the client (browser SPA, the `crimson-proxy` CORS relay, and a future
> browser extension) **without losing any current functionality**. It is a shift
> of *who does what*, not a feature change.
>
> **Author's framing:** the backend stays the brain for metadata, identity,
> secrets and orchestration policy; it stops being the pipe that every video byte
> *and* every scrape request flows through.

---

## 1. Goal & non-goals

**Goal.** Make per-viewer cost on the backend ~flat regardless of how much people
watch. Today both the *control bytes* (scrape/resolve HTTP traffic) and, for
proxied sources, the *video bytes* flow through the backend. We want:

- Video segment bytes: `CDN → (proxy/extension) → viewer`, never the backend
  (this is what `crimson-proxy` Phase 1 already started — see
  [[crimson-proxy-phase1]]).
- Scrape/resolve HTTP: executed from the **viewer's** browser/IP/extension, not
  a shared datacenter IP.
- Backend reduced to: metadata (TMDB/AniList), identity/accounts, signing,
  secret custody, and an **orchestration fallback** for clients that can't run a
  given source.

**Hard rules (from the brief).**

1. Retain every current capability. Every source listed in
   `scrapers/__init__.py::ALL_SCRAPERS` and `resolvers/__init__.py` keeps working.
2. No implementation in this pass — design only.
3. The public surface the frontend depends on stays intact (see
   [[frontend-api-contract]]): `/seasons`, `/info`, `/trending`, `/search`,
   `/watch*`. We may change *where* `/watch` runs, but the **NDJSON stream
   contract** and the resolved-stream shape `streamRank` consumes must not break.

**Non-goals.** Changing the metadata engine ([[crimson-metadata-engine]]),
account system ([[account-system]], [[email-password-auth-loginwall]]), or the
catalogue. Those stay server-side and are out of scope.

---

## 2. How it works today (the baseline)

### 2.1 The watch pipeline

`GET /watch/{tmdb}/{season}/{episode}` → `stream_watch_response()`
(`api.py:1492`) is a progressive **NDJSON** stream:

```
meta line  ──▶ stream line (per source, as it resolves) ──▶ … ──▶ done line
```

Internally, per request:

1. Resolve AniList metadata + air-date guard + German-title enrichment.
2. Fan out **every** `ALL_SCRAPERS` class as a concurrent task (`_work`,
   `api.py:1582`). Each scraper:
   - `search_anime(media_ctx)` → site slug,
   - `get_episode_embeds(...)` → list of embed URLs / `{url, language}`.
3. `resolve_streams(embeds, base_url)` (`api.py:978`) matches each embed to a
   resolver by `domain_keyword`, calls `resolver.resolve()`, and normalizes the
   result into `{source, type, url, [language], [subtitles], [cacheTicket]}`.
4. Resolvers return one of: a bare URL, a `{url, subtitles}` dict, a **list** of
   stream dicts (ScreenScape fan-out), or a **same-origin proxy path**
   (`/voe_proxy?…`, `/screenscape_proxy?…`, `/player`, `/{x}_proxy/h/…`).
5. The frontend (`crimson-client/src/hooks.js`) reads the NDJSON, ranks streams
   with `streamRank()` (`hooks.js:388`), and plays them in `CrimsonPlayer.jsx`.

### 2.2 Two delivery shapes exist today

| Shape | Example sources | Who carries video bytes |
| --- | --- | --- |
| **Same-origin proxy** | VOE, Vidmoly, VidSrc/megaplay, PlayIMDb, cinema.bz, ScreenScape, Movish, AnimeSuge, Jellyfin, Cache | **Backend** re-fetches playlist + every segment (header-injecting, HLS-rewriting). This is the bandwidth sink. |
| **Signed external relay** | The subset routed via `_crimson_proxy` ([[crimson-proxy-phase1]]) — VOE, cinema.bz, PlayIMDb | `crimson-proxy` edge host (Netlify/CF). Already off backend. |
| **Direct play** | Jellyfin (Auto direct), local, some direct-file | Player ↔ origin directly. |

`crimson-proxy` Phase 1 proved the relay-offload model: the backend still
*resolves*, but hands the player a **signed link** to the edge proxy instead of
its own `/{x}_proxy` path. Segment bytes go `CDN → edge → viewer`. The signing
contract (HMAC over `url\nreferer\norigin\nua`, 32-hex) is in
`crimson-proxy/src/utils/signing.ts` and the backend's `resolvers/_crimson_proxy.py`.

### 2.3 You already have the client-side blueprint

`../sudo-sources` is a **`@movie-web/providers` fork**. Today it ships *one*
thin `crimson` source that just calls the backend's `/mw` bridge
([[movieweb-apikey-bridge]]). But the framework it's built on
(`@movie-web/providers`) is *designed* to run scrapers in a browser through a
cors-proxy or an extension. **That framework is the natural foundation for this
whole migration** — we expand it from a 1-source bridge into the real engine.

---

## 3. Why this is hard: the four constraints that decide everything

Moving scraping to the browser runs into browser security. Each current
server-side trick exists to dodge one of these. The design lives or dies on
mapping each source to an execution environment that can satisfy its constraints.

| # | Constraint | What it blocks | Server-side fix today | Client story |
| --- | --- | --- | --- | --- |
| **C1** | **CORS** | `fetch()` can't *read* a cross-origin response without the CDN sending `Access-Control-Allow-Origin`. Most embed/CDN hosts don't. | Backend reads it server-side (no CORS in server-to-server). | **Needs a relay** (cors-proxy) **or an extension** (host permissions bypass CORS entirely). |
| **C2** | **Forbidden request headers** | `fetch()` can't set `Referer`, `Origin`, `User-Agent`, `Sec-Fetch-*` on media requests. Many CDNs gate on exactly these (VOE Referer, megaplay needs Referer+Sec-Fetch). | Backend sets them freely. | Relay injects them (proxy already does); **extension** can rewrite them via `declarativeNetRequest`. |
| **C3** | **TLS/HTTP2 fingerprint (JA3/JA4)** | Cloudflare WAF passively fingerprints the client stack. `httpx` gets blocked; `curl_cffi` impersonates Chrome (see `scrapers/base_scraper.py`, `resolvers/vidsrc.py`). | `curl_cffi` chrome impersonation. | **A real browser passes natively** — this constraint *inverts* and becomes an advantage client-side. The cors-proxy (Node/edge fetch) does **not** have a Chrome JA3, so JA3-gated sources must run in the **browser/extension**, not the edge proxy. |
| **C4** | **IP/ASN binding** | VOE binds the stream token to the **ASN that resolved the embed** (note the `asn=` param — `resolvers/voe.py`). A datacenter-resolved token 403s for a residential viewer; today the backend must *also* relay the segments from that same ASN. | Backend resolves *and* relays from one ASN. | **Resolving in the viewer's browser fixes this for free** — token is minted for the viewer's residential ASN, so segments can play far more directly. Big win. |
| **C5** | **Server-held secrets** | Some sources need a secret that must NOT ship to browsers: `FEBBOX_UI_TOKEN` ([[showbox-febbox-source]]), Jellyfin token ([[jellyfin-source]]), `OPENSUBTITLES_*` ([[opensubtitles-subtitles]]), TMDB key, the `/mw` API key, `PROXY_SECRET`. | Lives in backend env. | These sources/steps **stay server-side** (or the secret is injected by the proxy edge, never the bundle — the `/mw` pattern sudo-proxy already uses). |

**The headline insight:** C3 and C4 — the two *nastiest* server-side problems —
get **easier or vanish** when scraping runs in the viewer's real browser. C1 and
C2 are the price, and they're already solved by the cors-proxy and trivially
solved by an extension. C5 is the only thing that genuinely pins certain work to
the backend.

---

## 4. Target architecture: a tiered execution model

Not every source can run in every environment (Section 6). So we define **four
execution environments**, ordered by capability, and route each source to the
*lowest-cost environment that can run it*.

```
            ┌─────────────────────────────────────────────────────────────┐
            │  CLIENT (crimson-client SPA)                                  │
            │  ┌──────────────────────────────────────────────────────┐    │
            │  │  crimson-sources  (TS providers engine, ex sudo-src)  │    │
            │  │   • scrape → resolve → emit Stream[]                  │    │
            │  │   • produces the SAME NDJSON the backend does today   │    │
            │  └───────────────┬───────────────────┬──────────────────┘    │
            │      fetcher A    │      fetcher B    │   fetcher C           │
            └──────────────────┼───────────────────┼──────────────────────┘
                   browser     │   cors-proxy      │   extension
                   fetch       │   (edge relay)    │   (DNR + host perms)
                   [E1]        │   [E2]            │   [E3]
                               │                   │
            ┌──────────────────┴───────────────────┴──────────────────────┐
            │  BACKEND  [E0]                                                │
            │   • metadata, accounts, signing, SECRET-bound sources only   │
            │   • /watch orchestration FALLBACK (today's code, untouched)  │
            └──────────────────────────────────────────────────────────────┘
```

### The four environments

- **E0 — Backend (unchanged).** Runs the current Python scrapers/resolvers.
  Remains the home of secret-bound sources (Febbox, Jellyfin, OpenSubtitles,
  `/mw`) and the **fallback** for any client that can't run a source. This is
  what guarantees "retain all functions": worst case, a source falls back to E0
  and behaves exactly like today.

- **E1 — Browser fetch (SPA, no extension).** Real Chrome JA3 (clears C3), real
  residential IP (clears C4). But subject to CORS (C1) and forbidden headers
  (C2). On its own it can only talk to CORS-enabled hosts. Pair it with E2 for
  the cross-origin hops.

- **E2 — `crimson-proxy` edge relay (extended).** Already injects headers (C2)
  and is CORS-open (C1). But it's Node/edge `fetch` — **no Chrome JA3** (fails
  C3) and a **datacenter IP** (fails C4). So it's the right tool for *header
  injection + CORS + segment relay*, **wrong** for JA3-gated handshakes and
  ASN-bound token minting. Today it only relays; we extend it to also run a
  curated set of *scrape* steps that don't need JA3/residential-IP.

- **E3 — Browser extension (future).** The superpower environment. With
  `declarativeNetRequest` it rewrites `Referer`/`Origin`/`UA`/`Sec-Fetch-*`
  (clears C2) and host-permissions let it read cross-origin responses (clears
  C1) — all from a **real browser with a residential IP** (clears C3 *and* C4).
  An extension can run essentially *every* non-secret source end-to-end with **no
  backend and no proxy in the path**. This is the end-state we design toward.

### Routing rule

> For a given `(source, client capabilities)`, pick the **leftmost** environment
> in `[E3, E1+E2, E2, E0]` that satisfies the source's constraint set. Always
> have E0 as the floor so nothing can regress below today's behavior.

---

## 5. The client scraping engine (`crimson-sources`)

### 5.1 Foundation

Fork/extend `../sudo-sources` (already a `@movie-web/providers` fork) into the
**real** engine — call it `crimson-sources`. Movie-web's framework gives us, for
free, the exact abstractions this migration needs:

- A **source/embed** split (scrapers emit embeds, embeds resolve to streams) —
  structurally identical to our `scrapers/` → `resolvers/` split.
- A **fetcher abstraction** (`ctx.fetcher` / `ctx.proxiedFetcher`) — the seam
  where we plug E1/E2/E3.
- A **`Stream` / `Caption` type** and a runner that races sources — structurally
  identical to our NDJSON race.

### 5.2 The fetcher strategy (the heart of it)

Each provider is written **once** against `ctx.fetcher`. At runtime the host app
injects a fetcher implementation based on the environment:

```
ctx.fetcher  ─┬─ E3 present  → extensionFetcher  (DNR header rewrite, no CORS, residential)
              ├─ E1+E2       → proxiedFetcher     (browser → crimson-proxy → upstream)
              ├─ E1 only     → directFetcher      (CORS-enabled hosts only)
              └─ none usable → backendFetcher      (delegate the whole source to E0 /watch)
```

The provider code never branches on environment; the **capability probe** at
startup decides which fetcher each source gets, and a source that declares a
capability the current fetcher can't meet is routed to `backendFetcher` (E0).

### 5.3 What each provider needs to declare

Extend the movie-web source descriptor with a Crimson capability manifest so the
router can place it:

```ts
flags: {
  needsJA3: boolean        // C3 → must be E1 or E3, never E2/edge
  needsResidentialIP: boolean // C4 (VOE) → must be E1 or E3
  needsHeaderInjection: boolean // C2 → E2 or E3 (or E1 for CORS-safe-listed only)
  needsCORSBypass: boolean // C1 → E2 or E3
  needsServerSecret: boolean // C5 → pinned to E0
}
```

### 5.4 Porting the crypto resolvers

Two resolvers do non-trivial crypto that must be ported to TS/WASM to run client-side:

- **ScreenScape** ([[screenscape-source]]): per-session HMAC bootstrap + AES
  envelope decrypt (`resolvers/_screenscape_crypto.py`, vendored AES in
  `resolvers/_aes.py`). Port to WebCrypto (`crypto.subtle` does AES-CBC/GCM +
  HMAC natively — no vendored AES needed in the browser). **Note:** ScreenScape
  uses plain `httpx` not `curl_cffi`, so it's **not** JA3-gated → it can run in
  E2 (edge) as well as E1/E3.
- **VOE / VidSrc**: HMAC link signing is trivial in WebCrypto. VidSrc/megaplay
  needs JA3 **and** Referer+Sec-Fetch *together* (`resolvers/vidsrc.py`) → E1
  (browser fetch has the JA3) **+ extension or proxy for the headers**, i.e.
  effectively **E3-only** for a clean run, with E0 fallback.

Account crypto (Ed25519, [[account-system]]) stays server-side — it's identity,
not scraping, and out of scope.

---

## 6. Per-source placement matrix

This is the concrete migration target. "Best env" = where it should run once the
extension exists; "Web-only env" = best achievable in the SPA without the
extension. **Everything keeps E0 as fallback.**

| Source | Constraints | Best env (with ext) | Web-only (no ext) | Notes |
| --- | --- | --- | --- | --- |
| **VOE** (via aniworld/s.to) | C2 (Referer), C4 (ASN!) | **E3** | E1 resolve + **E2 relay** | ASN binding *wants* client resolve; this is the flagship win. Already partly on `crimson-proxy`. |
| **Vidmoly** | C1, C2 | E3 | E1+E2 | Same family as VOE. |
| **VidSrc / megaplay** | C1, C2 (Referer+Sec-Fetch), C3 (JA3) | **E3 only** | **E0** (needs JA3+headers together; edge can't do JA3) | Hardest web-only case — keep on backend until extension ships. |
| **ScreenScape** | C1, C2, crypto (no JA3) | E3 | **E2** (edge can run the HMAC/AES handshake) | Port crypto to WebCrypto; ~15 servers → list fan-out preserved. |
| **cinema.bz** | C1, C2 | E3 | E1+E2 | Already on `crimson-proxy`. |
| **PlayIMDb** | C1, C2 (Referer-gated) | E3 | E1+E2 | Already on `crimson-proxy`. |
| **s.to / aniworld / stomirror** (discovery) | C1, C3 (Turnstile/JA3 on s.to) | E3 | E1 (real browser clears the passive gate) → **E2 for embeds** | stomirror exists precisely to dodge the Turnstile gate; in a real browser (E1/E3) the gate is far less of a problem. |
| **AniWatch** (discovery) | C1 | E2/E3 | E2 | Plain WordPress; CORS only. |
| **Movish** | single-origin player-proxy | E3 | **E0** | It's an iframe player-proxy (`/{x}_proxy/h/…`), not an HLS link; needs same-origin embed/api. Keep on E0 or rework as iframe. |
| **AnimeSuge** | C1, C2 (Referer), signing | E3 | E1+E2 | Direct-file; signed proxy. |
| **ShowBox / Febbox** | **C5 (FEBBOX_UI_TOKEN)** | **E0** (or E2 with edge-injected token) | **E0** | Discovery is auth-free but `/file/player` needs the secret. Mirror the `/mw` pattern: edge injects token, never the bundle. |
| **Jellyfin** | **C5 (token)**, personal | **E0** | **E0** | Token-injecting proxy; personal source; stays server-side. |
| **Cache** | server NAS, signed ticket | **E0** | **E0** | Server-side cache engine ([[video-cache-engine]]); inherently backend. |
| **Local** | admin NAS mounts | **E0** | **E0** | Direct play of server-registered dirs. |
| **Subtitles (OpenSubtitles)** | **C5 (quota key)** | **E0** | **E0** | `/subtitles` search authed, `/subtitles_proxy` public-signed. Keep key server-side; client merges tracks ([[opensubtitles-subtitles]]). |
| **Skip times (AniSkip)** | keyless, free | **E1** | **E1** | Already keyless; can move fully client-side ([[skiptimes-aniskip]]). |

**Takeaway:** with the extension (E3) almost everything except the 4 secret-bound
sources (Febbox, Jellyfin, Cache, Local, Subtitles) leaves the backend entirely.
Web-only, the big bandwidth winners (VOE, cinema.bz, PlayIMDb, ScreenScape,
AnimeSuge, Vidmoly) move to E1+E2/E2; only VidSrc and Movish stay on E0 until the
extension lands.

---

## 7. The contract: keep NDJSON, move its producer

The frontend's `streamRank` + player consume an NDJSON line shape:

```json
{"type":"stream","source":"…","streamType":"hls|mp4|iframe","url":"…",
 "language":null,"subtitles":null,"cacheTicket":"…?"}
```

**We keep this contract byte-compatible** and change only *who emits it*:

- **Today:** backend `/watch` emits it.
- **New:** `crimson-sources` runs in the client and emits the **same line
  objects** to the player's existing consumer. The progressive "race" UX is
  preserved because movie-web's runner already yields per-source.
- **Fallback:** when a source is routed to E0, the client calls the existing
  `/watch` route for *just that source* (or the whole request) and re-emits its
  NDJSON lines into the same pipeline. So the consumer never knows the
  difference. **This is the key to "retain all functions" with zero player
  rewrite.**

`cacheTicket` minting ([[video-cache-engine]]) stays an E0 concern: the client
asks the backend to mint a ticket for a resolved stream (cheap, no bytes), and
the existing `/cache/confirm` flow is unchanged.

---

## 8. Security model

Moving scraping client-side widens the trust boundary. Mitigations:

1. **The cors-proxy must stay signed-only.** The whole point of `crimson-proxy`'s
   HMAC (`PROXY_SECRET`) is that it isn't an open relay. **Problem:** if the
   *client* now decides what to fetch, who signs? Options:
   - **(a) Backend mints short-lived signed fetch grants.** Client sends the
     intended upstream + headers to a tiny backend `/sign` endpoint (authed,
     rate-limited); backend returns the signed proxy link. Keeps `PROXY_SECRET`
     server-only. Costs one tiny round-trip per upstream host (not per segment —
     HLS rewrite re-signs sub-resources at the edge with the same secret, exactly
     as today). **Recommended.**
   - **(b) Per-session scoped signing key.** Backend hands the logged-in client a
     short-TTL HMAC key scoped to that session; client signs its own links; proxy
     verifies against the session key. Fewer round-trips, larger blast radius if
     leaked. Consider for E3 only.
   - The extension (E3) often needs **no** proxy at all (DNR + host perms), so it
     sidesteps this entirely for most sources.
2. **Secrets never ship to the browser.** C5 sources stay E0, or use the
   `/mw`-style **edge token injection** (`sudo-proxy` already injects the `/mw`
   key when it sees the backend host) so `FEBBOX_UI_TOKEN` lives at the edge, not
   in the bundle.
3. **Login wall preserved.** `REQUIRE_LOGIN` ([[email-password-auth-loginwall]])
   still gates the `/sign` + metadata endpoints, so anonymous users can't use the
   client engine as a free scraping/relay service.
4. **Abuse / rate-limiting moves with the work.** The `/sign` endpoint and the
   E0 fallback carry the existing `slowapi` limits. The proxy keeps its
   signed-only gate + `isSafeUpstream` SSRF check (`crimson-proxy/src/utils/ssrf.ts`).

---

## 9. Bandwidth & cost impact

| Traffic class | Today | After (web-only) | After (extension) |
| --- | --- | --- | --- |
| Video segments (proxied sources) | **Backend** | Edge proxy / direct | **Direct CDN→viewer** |
| Scrape/resolve HTTP | Backend (1 datacenter IP) | Browser + edge | **Browser only** |
| Metadata (TMDB/AniList) | Backend | Backend (unchanged) | Backend |
| Signing / ticket mint | Backend | Backend (tiny) | Backend (tiny) |

Backend egress drops from "all video for proxied sources" to "≈0 video + a few
hundred bytes of signing per play." The remaining backend cost scales with
*library size and user count*, not *watch-hours* — which is the scaling wall you
flagged. The shared-IP ban/ratelimit risk (one datacenter ASN scraping every
source for every user) also disperses across residential IPs, which incidentally
improves source reliability (esp. VOE C4 and s.to C3).

---

## 10. Rollout phases

- **Phase 0 (done):** `crimson-proxy` relay offload for VOE/cinema.bz/PlayIMDb
  ([[crimson-proxy-phase1]]). Backend still resolves.
- **Phase 1 — Engine skeleton.** Expand `sudo-sources` → `crimson-sources`: port
  the fetcher strategy, the capability manifest, and **one** easy source
  end-to-end web-only (recommend **cinema.bz** or **PlayIMDb** — already
  proxy-routed, CORS+header only, no JA3/secret). Client emits NDJSON locally;
  E0 fallback for everything else. Prove the player consumer is unchanged.
- **Phase 2 — Web-only migration.** Move the CORS+header sources (VOE resolve via
  E1 + relay via E2, ScreenScape via E2 with WebCrypto, AnimeSuge, Vidmoly).
  Implement backend `/sign` (option 8a). VidSrc/Movish/secret sources stay E0.
- **Phase 3 — Extension (E3).** Ship the browser extension with `declarativeNetRequest`
  header rewrite + host permissions. Route JA3/header/ASN sources (VidSrc, VOE,
  s.to discovery) through it; most sources now bypass the proxy entirely.
- **Phase 4 — Secret sources via edge injection.** Move Febbox (and optionally
  Jellyfin for non-personal deployments) to edge-token injection à la `/mw`, so
  even those leave the backend's data path. Cache/Local/Subtitles stay E0 by
  nature.

Each phase is independently shippable and **never removes the E0 fallback**, so
there's no flag-day and no functionality regression.

---

## 11. Risks & open questions

1. **VidSrc web-only gap.** Needs JA3 + Referer + Sec-Fetch *together*; the edge
   proxy has no Chrome JA3, so there's **no clean web-only path** — it stays E0
   until the extension. Accept this gap, or invest in a JA3-impersonating edge
   runtime (heavier).
2. **Maintenance doubling.** Sources would exist in Python (E0 fallback) *and* TS
   (client). Mitigation: treat the TS engine as primary and let the Python side
   bit-rot to "fallback only," OR generate one from the other. Decide the
   long-term source of truth.
3. **Signing round-trips (option 8a)** add latency before first byte. Mitigation:
   batch-sign all of a source's likely hosts in the meta phase; the HLS
   sub-resource re-signing already happens at the edge.
4. **Extension adoption.** Web-only users get the Phase-2 subset; the *full* win
   needs the extension. Is the extension opt-in (power users) or pushed to
   everyone? Affects how much we invest in the web-only E2 paths vs. waiting for E3.
5. **CSP.** The SPA's `frame-src`/`connect-src` CSP must allow the proxy origins
   and any direct-CORS hosts; iframe sources (Movish) already rely on
   `frame-src https:`. Audit before Phase 2.
6. **`cacheTicket` semantics** when the client resolves: confirm the backend can
   still mint a ticket for a stream it didn't resolve (it can — minting is keyed
   on the stream descriptor, not on having fetched it).

---

## 12. Decisions I need from you

1. **Foundation:** confirm we build on the `@movie-web/providers` fork
   (`sudo-sources` → `crimson-sources`) rather than a bespoke client runtime. I
   strongly recommend the fork — you already run it and its abstractions map 1:1.
2. **Source of truth long-term:** keep Python sources as permanent fallback, or
   sunset them once the TS engine + extension cover a source (Risk 2)?
3. **Signing model:** backend `/sign` grants (8a, safer) vs. per-session scoped
   key (8b, faster)? I lean 8a, with 8b reserved for the extension.
4. **Extension reach:** opt-in power feature, or the intended default for all
   users? This sets how hard we push the web-only E2 paths.
5. **Scope check:** is VidSrc/Movish staying on E0 until the extension acceptable,
   or do you want a JA3-capable edge runtime to close the web-only gap sooner?

---

### Appendix A — File map (current → future home)

| Current (backend) | Future primary home |
| --- | --- |
| `scrapers/*_scraper.py` | `crimson-sources/src/providers/sources/*` (TS), E0 fallback kept |
| `resolvers/voe.py`, `cinemabz.py`, `playimdb.py`, `animesuge.py`, `vidmoly.py` | TS embeds; relay via `crimson-proxy`/extension |
| `resolvers/_screenscape_crypto.py`, `_aes.py` | WebCrypto in `crimson-sources` |
| `resolvers/_crimson_proxy.py` + `crimson-proxy/` | unchanged relay; add backend `/sign` grant endpoint |
| `resolvers/febbox.py`, `jellyfin.py`, `local.py`, `cache.py` | **stay E0** (C5 / server-bound) |
| `subtitles_engine/`, `skiptimes_engine/` | subtitles stay E0; skiptimes → E1 |
| `stream_watch_response()` (`api.py:1492`) | stays as **E0 fallback**; client runner becomes primary producer of the same NDJSON |
