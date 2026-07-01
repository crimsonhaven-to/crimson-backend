# The Crimson Architecture — Brain, Not Pipe

> A word from Lumi ( ^ . ^ ) — welcome, little mortal, to the blueprint of the
> whole sanctuary. This document is the map of how **Crimson Haven** actually
> breathes: how the backend, the browser client, the edge relay and the companion
> extension pass work between one another so that video bytes flow *straight to
> you* and never pool up in my datacenter. It is not a wishlist. Most of what
> follows is already standing; where something is still on the loom, I say so
> plainly.

---

## The one idea everything hangs on

The backend is the **brain**. It is *not* the pipe.

It keeps the things that must be kept in one trusted place — the TMDB↔AniList
metadata mind, identity and the members-only login wall, the signing secrets, and
the orchestration of who-does-what. What it deliberately refuses to be is the
tube every video byte squeezes through. Bandwidth is cruel to scale and crueller
to pay for, so the design goal is blunt:

> **Per-viewer backend cost stays roughly flat no matter how much anyone
> watches.** Cost should track *library size* and *how many mortals I love*, never
> *watch-hours*.

The only media the backend itself ever serves is **operator-owned** — **Local**
(your own NAS / bind mounts), **Cache** (episodes this server already remuxed onto
your own storage), and **Jellyfin** (your own media server), plus one inert
**template** source that documents the contract. Everything a viewer actually
watches from a *third party* is scraped, resolved and delivered **in that
viewer's own browser** — helped, when needed, by the edge relay and the companion
extension. The backend never hosts, stores, embeds or ships those sources; it only
hands the client the few crumbs it genuinely cannot derive on its own.

That single shift — *move the scraping and the bytes to the edge of the world,
keep only the mind at the center* — is the entire architecture. The rest of this
document is just the honest accounting of how it's done without losing a thing.

---

## The cast of four

Four things share the work. Each is a separate repository; each has one job it is
good at.

```
        ┌────────────────────────────────────────────────────────────────┐
        │  THE VIEWER'S BROWSER                                           │
        │                                                                 │
        │   crimson-client (SPA)                                          │
        │    └── crimson-sources  ── the client scraping engine (TS)      │
        │          scrape → resolve → emit the SAME /watch NDJSON line    │
        │                                                                 │
        │    fetch strategy chosen at runtime:                            │
        │      • direct browser fetch        [E1]  real Chrome, real IP   │
        │      • crimson-proxy edge relay    [E2]  headers + CORS + relay │
        │      • crimson-extension companion [E3]  the superpower         │
        └───────────────┬───────────────────────────┬────────────────────┘
                        │                            │
             signed edge links (/sign)     header rewrite + CORS bypass
                        │                            │
        ┌───────────────┴────────────┐   ┌───────────┴────────────────────┐
        │  crimson-proxy  [E2]        │   │  crimson-extension  [E3]        │
        │  signed CORS relay at the   │   │  MV3 companion; declarativeNet- │
        │  edge; injects headers,     │   │  Request header rewrite + host  │
        │  relays HLS segments        │   │  perms; off by default          │
        └───────────────┬────────────┘   └───────────┬────────────────────┘
                        │                            │
        ┌───────────────┴────────────────────────────┴────────────────────┐
        │  crimson-backend  [E0] — THE BRAIN                              │
        │   • metadata (TMDB↔AniList), accounts, login wall               │
        │   • signing secrets, secret-bound resolves                      │
        │   • operator-owned sources (Local / Cache / Jellyfin)           │
        │   • the /watch NDJSON orchestration — the permanent floor       │
        └──────────────────────────────────────────────────────────────────┘
```

- **`crimson-backend` (this repo) — E0, the brain.** FastAPI. Holds the metadata
  engine, accounts + the login wall, the operator-owned sources, the signing
  secrets, and the progressive `/watch` NDJSON pipeline. It is also the permanent
  **floor**: if any client can't run a source, the backend still can, exactly as
  it always did. Nothing can ever regress below "what the backend does today."

- **`crimson-sources` — the client engine.** A **from-scratch TypeScript** scrape
  → resolve engine that runs *inside the viewer's browser*, vendored into the
  client. It emits the **byte-identical** `/watch` NDJSON line the backend emits,
  so the player never learns the difference. (It is *not* a fork of any
  movie-web/providers code — that path was considered and set aside; this engine
  is our own, shaped to our contract.)

- **`crimson-proxy` — E2, the edge relay.** A separate, signed-only CORS relay at
  the edge (Netlify / Cloudflare). It injects the forbidden request headers and
  relays HLS segments `CDN → edge → viewer`, so those bytes skip the backend
  entirely. It is *not* an open relay — every link it serves is HMAC-signed.

- **`crimson-extension` — E3, the companion.** A tiny MV3 Chromium extension, off
  by default, one red **"Use Extension"** button. It does exactly two things and
  nothing more: rewrite forbidden request headers via `declarativeNetRequest`, and
  unblock cross-origin reads via host permissions — all from a real browser at a
  real residential IP. No scraping, no secrets, no signing live in it. It exposes
  `window.CrimsonExtension` to the page and announces itself with a
  `data-crimson-ext` attribute so the client can detect it without knowing its id.

---

## The five walls (why this is hard)

Move scraping into a browser and you walk straight into browser security. Every
clever thing the old server-side code did existed to climb *one* of these walls.
The whole design is really just: **map each source to the environment that can
climb the walls that source faces.**

| # | The wall | What it blocks | How the browser world climbs it |
| --- | --- | --- | --- |
| **C1 — CORS** | A page `fetch()` can't *read* a cross-origin response unless the host sends `Access-Control-Allow-Origin`. Most media hosts don't. | The **edge relay** is CORS-open; the **extension** bypasses CORS entirely via host permissions. |
| **C2 — Forbidden headers** | A page `fetch()` can't set `Referer`, `Origin`, `User-Agent`, `Sec-Fetch-*`. Many hosts gate on exactly those. | The **edge relay** injects them; the **extension** rewrites them with `declarativeNetRequest`. |
| **C3 — TLS/HTTP2 fingerprint (JA3/JA4)** | WAFs passively fingerprint the client stack; a plain HTTP library gets blocked where a real Chrome sails through. | **A real browser passes natively.** This wall *inverts* into an advantage the moment scraping runs in the viewer's Chrome (E1/E3). The edge relay is edge-`fetch`, *not* Chrome — so JA3-gated work must run in the browser, not the relay. |
| **C4 — IP / ASN binding** | Some hosts bind the stream token to the *network* that resolved it. A datacenter-resolved token then 403s for a home viewer — forcing the byte relay to also come from that same datacenter. | **Resolving in the viewer's own browser mints the token for their own residential network for free.** The flagship win. |
| **C5 — Server-held secrets** | A few sources need a secret that must *never* ship to a browser (a Jellyfin token, an API quota key, the proxy signing secret, the TMDB key). | These steps **stay at the backend** (E0), or the secret is injected at the **edge**, never in the client bundle. |

The headline, and the reason the whole remodel is worth it: **C3 and C4 — the two
nastiest walls — get *easier or vanish* when scraping runs in the viewer's real
browser.** C1 and C2 are the price, and both are already paid by the relay and the
extension. C5 is the only thing that genuinely pins certain work to the backend
forever — and that's fine, because those are all operator-owned or key-bound
anyway.

---

## The four environments

Because not every source can climb every wall from every place, we define **four
execution environments**, ordered by capability, and route each source to the
*lowest-cost environment that can still run it*.

- **E0 — Backend.** Runs the operator-owned sources and the secret-bound resolve
  steps, and remains the **fallback** for anything a client can't do. This is the
  guarantee that nothing ever regresses: worst case, a source falls back to E0 and
  behaves exactly as it always has.

- **E1 — Direct browser fetch.** Real Chrome fingerprint (clears C3), real
  residential IP (clears C4). But it's subject to CORS (C1) and forbidden headers
  (C2), so on its own it can only reach CORS-friendly hosts. Pairs with E2 for the
  cross-origin hops.

- **E2 — `crimson-proxy` edge relay.** Injects headers (C2), is CORS-open (C1),
  and relays segments off the backend. But it's edge-`fetch`: **no** Chrome JA3
  (fails C3) and a **datacenter IP** (fails C4). Perfect for *header injection +
  CORS + segment relay*; wrong for JA3-gated handshakes and network-bound tokens.

- **E3 — `crimson-extension` companion.** The superpower. Header rewrite (C2) +
  host-permission CORS bypass (C1), all from a **real browser at a residential IP**
  (clears C3 *and* C4). With the extension present, a non-secret source can run
  end-to-end with **no backend and no relay in the byte path at all.** This is the
  end-state we design toward.

**The routing rule.** For a given `(source, what this client can do right now)`,
pick the **leftmost** environment in `[E3, E1+E2, E2, E0]` whose capabilities meet
that source's walls. Always keep **E0** as the floor so nothing can fall below
today's behavior.

---

## The client engine — `crimson-sources`

`crimson-sources` is the browser-side scrape → resolve engine, written from
scratch in TypeScript and vendored into `crimson-client` (as a git submodule with
a Vite alias — TS transpiled inline, no separate build step). It is the mirror of
what the backend's pipeline used to do for third parties, but living where it
belongs: at the viewer's edge.

**Shape.** `createEngine().streamEpisode(...)` is an **async generator**. It
scrapes, resolves, and `yield`s each resolved source the instant it's ready — as
the *same* `{"type":"stream", …}` NDJSON line the backend emits. The client's
player consumer already races per-source, so the progressive "fastest source plays
first" feel is preserved for free. Backend-resolved (operator-owned) streams and
client-resolved (third-party) streams land in one list, **deduped by
`(source, language)`**, and neither side knows the other exists.

**The fetcher is the whole trick.** Each source is written **once** against an
abstract fetcher. At runtime the engine's capability probe injects the right
implementation for the environment on hand:

```
fetcher  ─┬─ extension present  → extensionFetcher  (E3: header rewrite, CORS bypass, residential)
          ├─ proxy configured   → proxiedFetcher    (E2: browser → crimson-proxy → upstream)
          ├─ direct-capable      → directFetcher     (E1: CORS-friendly hosts only)
          └─ none can            → backendFetcher     (E0: delegate to /watch, re-emit its lines)
```

The source code never branches on environment; the router does. A source that
declares a capability the current fetcher can't satisfy is routed to the backend
fetcher, and its `/watch` NDJSON lines are simply re-emitted into the same
pipeline. **That is the key to "retain every function with zero player rewrite."**

Each source carries a small **capability manifest** so the router can place it —
in spirit: *does it need a real JA3 (→ E1/E3), a residential IP (→ E1/E3), header
injection (→ E2/E3), a CORS bypass (→ E2/E3), or a server-held secret (→ pinned to
E0)?* Any crypto a source needs (per-session HMAC, AES envelopes, link signing) is
done with **WebCrypto** (`crypto.subtle` gives AES-CBC/GCM + HMAC natively — no
vendored crypto in the browser). Identity crypto (the Ed25519 account keys) stays
server-adjacent and out of scope; this engine is about *streams*, not *who you
are*.

---

## The contract that never breaks

The frontend's stream ranker and player consume one NDJSON line shape:

```jsonc
{"type":"stream","source":"…","streamType":"hls|mp4|iframe","url":"…",
 "language":null,"subtitles":null,"cacheTicket":"…?"}
```

We keep this **byte-compatible** and change only *who emits it*:

- **Yesterday:** the backend `/watch` route emitted every line.
- **Today:** `crimson-sources` runs in the browser and emits the **same line
  objects** to the same consumer, alongside the backend's operator-owned lines.
- **Always:** when a source is routed to E0, the client calls `/watch` for it and
  re-emits its NDJSON into the same pipeline. The consumer is none the wiser.

`cacheTicket` minting stays an E0 concern: the client asks the backend to mint a
ticket for a resolved stream (cheap — no bytes cross the backend), and the
existing cache-confirm flow is untouched. The backend can mint a ticket for a
stream it never fetched, because minting is keyed on the *stream descriptor*, not
on having carried its bytes.

---

## The backend's crumbs — the offload grants

The backend stays the brain by handing the client engine *only* what it genuinely
can't derive on its own — never a secret, never a byte it doesn't have to. These
are small, login-gated endpoints (the same login wall that guards `/watch`):

| Grant | What it hands over | Why the client can't do it itself |
| --- | --- | --- |
| **`GET /scrape-meta/{tmdb}/{season}`** · **`/scrape-meta/movie/{tmdb}`** | The title bundle the title-keyed sources need — AniList/TMDB titles, German synonyms, `release_year`, `imdb_id`. | `release_year` and `imdb_id` come from the **server-held TMDB key** (C5). |
| **`POST /sign`** | A **signed `crimson-proxy` edge link** (E2) for a header-only source. | The **`PROXY_SECRET`** must never reach a browser (C5). The client sends the intended upstream + headers; the backend returns a signed edge URL. `503` when no proxy is configured — the client then stays on E3 or falls back to E0. |
| **`POST /resolve`** | A **secret-bound resolve grant**: runs a token-gated lookup server-side (e.g. a Jellyfin token) and returns the *raw* stream for the client to deliver via edge/extension. | The token is a server secret (C5); only the *lookup* is server-side, the *bytes* still skip the backend. |

The signing round-trip is per-*host*, not per-*segment* — the edge re-signs an
HLS playlist's sub-resources with the same secret as it relays them, so a whole
episode costs a few hundred signed bytes at the backend, not a stream of them.

---

## The trust boundary

Pushing scraping to the edge widens the circle of trust, so it's fenced:

1. **The relay stays signed-only.** The entire point of `crimson-proxy`'s HMAC is
   that it is *not* an open relay. Since the client now decides *what* to fetch but
   must not hold the signing secret, the backend mints **short-lived signed
   grants** via `POST /sign` (authed, rate-limited). `PROXY_SECRET` never leaves
   the backend; the extension (E3) often needs no relay at all and sidesteps this
   entirely.
2. **Secrets never ship to the browser.** C5 sources stay E0, or use **edge token
   injection** so the secret lives at the edge, never in the bundle.
3. **The login wall is preserved.** With `REQUIRE_LOGIN` on (the default), the
   grants and metadata endpoints are members-only — anonymous mortals can't turn
   the client engine into a free scraping/relay service.
4. **Abuse controls move with the work.** The grant endpoints and the E0 fallback
   carry the existing `slowapi` limits; the relay keeps its signed-only gate and
   its SSRF guard on every upstream.

---

## What this buys us

| Traffic | Yesterday | Web-only (E2) | With the extension (E3) |
| --- | --- | --- | --- |
| Video segments | **Backend** carried them all | Edge relay / direct | **Direct CDN → viewer** |
| Scrape / resolve HTTP | Backend (one datacenter IP) | Browser + edge | **Browser only** |
| Metadata (TMDB/AniList) | Backend | Backend (unchanged) | Backend |
| Signing / ticket mint | Backend | Backend (a few bytes) | Backend (a few bytes) |

Backend egress collapses from "every byte of every third-party stream" to
"≈0 video + a whisper of signing per play." The remaining cost scales with library
size and member count — never watch-hours. As a lovely side effect, the old
single-datacenter-ASN scraping every source for everyone disperses across
residential browsers, which quietly *improves* source reliability (exactly the
C3/C4 hosts that used to be so temperamental).

---

## Where we actually are

- **✅ Edge relay (E2), phase one.** `crimson-proxy` proved the offload model: the
  backend resolves, then hands the player a **signed edge link** instead of its own
  proxy path, and segment bytes go `CDN → edge → viewer`.
- **✅ The companion extension (E3) is built.** `crimson-extension` — MV3, no build
  step, off by default. Does *only* CORS unblock + header injection (no scraping,
  no secrets, no signing). Exposes `window.CrimsonExtension`
  (`fetch` / media-rule install/clear / hello / onChange) and self-announces via a
  page attribute. *Not yet shaken out live in Chrome.*
- **✅ The client engine (E1), phase one.** `crimson-sources` runs from-scratch TS
  in the browser, emits the byte-identical `/watch` line, and ships its first
  sources end-to-end, deduped alongside the backend. **Off by default** behind a
  flag while it earns trust. The E2 path activates once `/sign` grants are wired
  through it.
- **✅ The backend grants exist.** `/scrape-meta`, `/sign` and `/resolve` are live,
  each login-gated and secret-safe.

**Still on the loom:** live extension shakeout in Chrome; rotating-segment media
rules for hosts that shuffle CDN hosts mid-stream; and porting the more defended
source archetypes (network-bound tokens, JA3-walled aggregators) now that the
tiering and the extension can carry them.

> Small note from Lumi's future self:
> SPA / JA3 is mostly done. Not beautifully, but it works.


---

## The archetypes, and where each one lives

Rather than a roll-call, here is how the *kinds* of source map onto the tiers.
The concrete third-party list lives in the private `crimson-sources` engine — the
backend names none of them.

| Source archetype | Walls it faces | Best home (with extension) | Web-only home |
| --- | --- | --- | --- |
| **Header-gated HLS host** (Referer/Origin only) | C1, C2 | E3 | **E1 resolve + E2 relay** — already the proven path |
| **Network-bound-token host** (token pinned to the resolving ASN) | C2, **C4** | **E3** | E1 resolve + E2 relay — resolving in the viewer's browser is the whole win |
| **JA3-walled aggregator** (needs a real Chrome handshake *and* headers together) | C1, C2, **C3** | **E3 only** | **E0** — the edge has no Chrome JA3, so there's no clean web-only path until the extension carries it |
| **Crypto-handshake aggregator** (HMAC/AES envelope, no JA3) | C1, C2, crypto | E3 | **E2** — the edge can run the handshake once the crypto is ported to WebCrypto |
| **Plain-CORS discovery** (ordinary web host) | C1 | E2/E3 | E2 |
| **iframe player-proxy** (a same-origin player, not a bare HLS link) | same-origin embed | E3 (reworked as iframe) | **E0** |
| **Operator-owned: Local / Cache / Jellyfin** | **C5** / server-bound | **E0** | **E0** — inherently the backend's own media |
| **Key-bound extras** (subtitle/quota-keyed tracks) | **C5** | **E0** | **E0** — the quota key stays server-side; the client just merges the tracks |
| **Keyless extras** (e.g. skip-times) | none | **E1** | **E1** — fully client-side, no backend needed |

**The takeaway.** With the extension in the picture, nearly everything except the
handful of secret-bound and operator-owned sources leaves the backend's byte path
entirely. Web-only, the biggest bandwidth wins already move to E1+E2. Only the
JA3-walled and same-origin-iframe archetypes wait on the extension — and until it
lands, they simply fall back to E0 and behave exactly as they always have.

---

## The shape of the roadmap

Each step is independently shippable and **never removes the E0 floor**, so there
is no flag-day and no regression:

- **Phase 0 — Edge offload.** ✅ Backend resolves, hands out signed edge links,
  bytes go `CDN → edge → viewer`.
- **Phase 1 — Engine + companion skeletons.** ✅ From-scratch `crimson-sources`
  emitting the byte-identical NDJSON with the tiered fetcher router; the
  `crimson-extension` companion built; the first easy (CORS + header only) sources
  proven end-to-end; E0 fallback for everything else.
- **Phase 2 — Web-only migration.** Wire `/sign` through the client fetcher so the
  E2 path lights up; move the header-only and crypto-handshake archetypes to
  E1+E2 / E2; JA3-walled and iframe archetypes stay on E0.
- **Phase 3 — The extension in earnest.** Live shakeout, rotating-segment media
  rules, then route the JA3 / network-bound / defended-discovery archetypes
  through E3 — most sources now bypass the relay entirely.
- **Phase 4 — Edge-injected secrets.** Move the key-bound archetypes to edge token
  injection where it's safe to, so even those leave the backend's data path.
  Cache / Local / operator-owned stay E0 by their very nature.

---

## A closing word from Lumi 🩸

So there it is, laid bare: a brain at the center that never touches your bytes, a
little engine in your own browser doing the fetching, an edge relay and a companion
extension to climb the walls the browser can't climb alone — and beneath it all,
the backend standing as a floor that guarantees *nothing you love ever stops
working*. It scales with how large the library grows and how many mortals I get to
adore, not with how many hours you spend in the dark with me. Which is exactly how
a sanctuary should breathe. ( ˶ ˆ ᗜ ˆ ˶ )
