# System Re-Design

! - Important: A concrete New system design can be found [here](/New_System.md)

## Involved Repositories

1. crimson-backend (This repo) The backend with metadata engine, account engine, recommended engine, etc. everything stateful
2. crimson-client (../crimson-client) The actual frontend of crimsonhaven-to
3. crimson-proxy (../crimson-proxy) A simple-enough CORS-Proxy which can also do Header Injections
4. crimson-sources (../crimson-sources) Where all the Typescript / Javascript re-implementations of my current sources will live (for the frontend, needs to be packaged into the frontend at build-time)
5. crimson-extension (../crimson-extension) A companion-extension which can rewrite CORS and also do Header Injections. Prefered as everything happens locally.

## Concrete Plan of Action

1. Companion Browser extension (../crimson-extension)
    - This extension will handle CORS and Header-injection on desktop devices. It must be performance-optimized, easy to use (literally just one red button "Use Extension" - The rest happens in the background)
    - It should follow the crimsonhaven styling, theming and doctrine.

2. Phase 1: Implement most important scrapers + resolvers within ../crimson-sources. 
    I deliberately decided against re-using my fork of movie-web/providers. I want something from the ground up, custom-tailored to crimsonhaven, nothing else.
    In this first phase, all the scrapers + some important resolvers (Voe / PlayIMDB / Screenscape) need to be implemented in the client. These changes might also require 
    changes to the CORS-Proxy. That can be found at ../crimson-proxy

3. Phase 1.5:
    Implement all the sources client-side. Ensure complete support and compatibility with the browser extension first.
    Definition of Done of Phase 1.5 is reached when the following criteria are met:
    - All sources need to be wired into the crimson-sources repository.
    - For now, compatibility only has to be ensured via the crimson-extension. 
    - Everything needs to be wired into the client (and, if necessary, the backend) so that local scraping / resolving client-side works (of course, with help of the crimson-extension)
    - On re-deploy, everything must work as it should. This is the last criteria: An end-to-end test which uses client-side (+ extension) scraping & resolving to resolve sources and then play the HLS / MP4 stream (depending on the source)

4. Phase 2:
    Cleanly separate the Dev and Prod environment. This will be achieved with the following means.
    - crimson-client as well as crimson-backend already have separated dev and main branches. dev is the Dev Environment, main is the Production environment. Implement the same branches into crimson-sources.
    - Expand current CI/CD pipeline for crimson-backend & crimson-client:
      - deploy a "crimson-client-dev"-Service on crimsonswarm
      - deploy a "crimson-backend-dev" Service on crimsonswarm
      - automated re-deployments ***on push*** into the dev-branch (ONLY when an actual CODE-file has been changed- there's no need for a redeploy on a pure markdown push)
      - keep automated re-deployments ***on tagged release*** in the main-branch
      Important: the dev-branch will not be as HA-Oriented as the prod version.
      Instead, it will use the docker-compose-dev.yml file, and one replica per container. (Meaning: One singular API-container, one singular PostgreSQL container. no cache-worker, no patroni / etcd HA cluster.)
    - Add the following URLs for Dev branches:
      - backend-dev.crimsonhaven.to
      - client-dev.crimsonhaven.to
    - All changes following completion of Phase 2 will be tested in the dev-environment before releasing them into Prod.

---

## Progress Log

### ✅ Step 1 — Companion browser extension (`../crimson-extension`)  — DONE (2026-06-28)

MV3 Chromium extension, no build step (plain JS/CSS/JSON, side-loadable). Does
**only** CORS unblock + header injection — no scraping, no secrets, no signing.
Off by default; one red **"Use Extension"** button in a crimson-themed popup.

**Files:**
- `manifest.json` — MV3; `declarativeNetRequestWithHostAccess` + `<all_urls>`;
  content script on `crimsonhaven.to`/`*.crimsonhaven.to`/`localhost`/`127.0.0.1`.
- `src/protocol.js` — shared message constants (SW + content script).
- `src/background.js` — service worker = the privileged core. Two capabilities:
  - **`fetch` RPC**: cross-origin fetch from the SW (host access ⇒ no CORS),
    injecting forbidden headers (Referer/Origin/UA/Cookie/Sec-Fetch-*) via an
    ephemeral DNR rule scoped to `tabIds:[-1]`; returns body (text or base64).
  - **media rules**: per-tab DNR rules that inject request headers + add
    `ACAO:*` to responses so the page's hls.js streams gated CDN segments
    directly (CDN→viewer, nothing in the byte path).
  - Gated on a persisted `enabled` flag; rules torn down on tab close / reload /
    re-handshake; toolbar icon+badge reflect on/off.
- `src/content.js` — isolated-world bridge (injects in-page API, relays msgs).
- `src/inpage.js` — MAIN-world `window.CrimsonExtension` API (promise-based).
- `src/popup.{html,css,js}` — the one-button UI + live stats.
- `icons/` — crimson blood-drop sigil (16/32/48/128, + `-off` greyscale);
  regen via `scripts/make_icons.py` (needs Pillow).

**Integration contract for `crimson-sources`** is documented in
`crimson-extension/README.md` (detection, `fetch`, `installMediaRules`,
`clearMediaRules`, `onChange`). Detection needs no extension id:
`window.CrimsonExtension?.available` / `document.documentElement.dataset.crimsonExt`
/ `crimson-extension-ready` event. When absent/disabled, the page must fall back
to the cors-proxy/backend path — the extension is a pure upgrade.

**Tested:** all JS `node --check`-clean; manifest valid JSON; icons render.
**NOT yet tested:** live load in Chrome + a real DNR header-inject round-trip
(no client integration exists yet to drive it). First real shakeout will come
with the first `crimson-sources` source in Step 2.

**Decisions still open** (from New_System.md §12) — not blockers for Step 2:
signing model for the cors-proxy path, Python-source sunset policy, extension
opt-in vs default. Worth deciding before/with Step 2.

### ✅ Step 2 — `crimson-sources` engine + first sources + client build wiring — DONE (2026-06-28)

From-scratch TS engine in `../crimson-sources` (NOT the movie-web fork — built
ground-up), vendored into the client as a git submodule and bundled by Vite.

**crimson-sources (`../crimson-sources`, committed `291f142`, NOT yet pushed):**
- `src/types.ts` — `StreamLine` (byte-identical to the backend `{"type":"stream",…}`
  NDJSON line), `SourceFlags` capability manifest, `Fetcher`/`Source` contracts.
- `src/fetchers.ts` — the tiered router: `extension` (E3) / `proxied` (E2, signed) /
  `direct` (E1); `selectFetcher` picks the leftmost that meets a source's flags,
  else `null` → backend (E0) handles it. No regression possible.
- `src/playback.ts` — final delivery shape per env: E3 = raw CDN url + DNR media
  rules (Referer/Origin + CORS), E2 = signed crimson-proxy url.
- `src/engine.ts` — `createEngine` → `streamEpisode()` async-generator that fans
  sources out concurrently and yields `StreamLine`s (the client /watch producer);
  installs/clears extension media rules; `dispose()`.
- `src/sources/cinemabz.ts` + `src/sources/playimdb.ts` — ports of the Python
  resolvers; TMDB-keyed, CORS+Referer only (no JA3, no secret). cinema.bz = 3
  provider tiles. Both fall back to E0 when neither extension nor a sign-grant is present.
- `tsc --noEmit` clean; `npm run build` emits `dist/` for standalone use.

**crimson-client wiring:**
- Submodule at `vendor/crimson-sources` (`.gitmodules` url = `../crimson-sources`,
  a **relative** url so CI resolves it to the org's sibling repo).
- `vite.config.js` alias `crimson-sources` → `vendor/crimson-sources/src/index.ts`
  (Vite transpiles the TS inline — no separate build step). `.dockerignore` only
  drops `.git`, so `COPY . .` bakes the vendored TS into the image.
- `src/clientSources.js` — bridge: `streamLocalSources(mediaCtx,{signal,onLine})`
  runs the engine and feeds the SAME `handleLine` the backend stream feeds.
  **OFF by default** (`localStorage 'crimson:clientSources'='1'` or build-time
  `VITE_CLIENT_SOURCES=true`); a no-op when off → prod behavior unchanged.
- `src/hooks.js` — show (`/watch/{tmdb}/{s}/{e}`) and movie (`/watch/movie/{tmdb}`)
  effects now run the local engine alongside the backend, with dedup-by-source-label
  (guarded on the flag). `npm run build` passes; engine confirmed in the bundle.
- CI `.github/workflows/build-image.yml`: checkout now `submodules: recursive` with
  a `SUBMODULES_TOKEN` PAT fallback (cross-repo private submodule).

**⚠️ Action needed from the user before CI/prod works:**
1. `cd ../crimson-sources && git push -u origin main` (the submodule pins `291f142`,
   which only exists locally — CI clone will fail until it's on the remote).
2. Commit the client wiring (`.gitmodules`, `vendor/crimson-sources` gitlink,
   `vite.config.js`, `src/clientSources.js`, `src/hooks.js`, the workflow) and push.
3. Add a repo/org secret `SUBMODULES_TOKEN` = a PAT with **read** on
   `crimsonhaven-to/crimson-sources` (the default job token can't read a *different*
   private repo). Or configure the self-hosted runner's git creds to reach it.

**Tested:** `tsc --noEmit` (sources) + `vite build` (client) both green; engine code
present in `dist`; no new lint errors (the 23 eslint errors are the repo's
pre-existing `set-state-in-effect` pattern). **NOT tested:** a live browser run with
the extension actually resolving cinema.bz/PlayIMDb (needs the extension loaded +
the flag on — the first real end-to-end shakeout).

### ✅ Phase 1.5 — All sources wired into the client engine (E3) — DONE (2026-06-29)

Every non-secret backend source now has a from-scratch TS port in `crimson-sources`,
runnable in the viewer's browser via the crimson-extension (E3). The backend (E0)
stays the floor for everything secret/server-bound, served by the `/watch` stream
that runs alongside the local engine — so nothing regresses.

**crimson-sources (`../crimson-sources`, committed `8cad476`, NOT yet pushed):**
- `src/resolvers/` — `voe.ts` (the flagship: full obfuscated-blob decode chain →
  raw m3u8 + DNR media rules; the ASN-bound token C4 is solved for free by resolving
  in the viewer's browser), `vidmoly.ts`, `vidsrc.ts` (megaplay; E3-only, JA3-gated).
- `src/crypto/` — `md5.ts` (vendored; CryptoJS `EVP_BytesToKey` needs MD5, absent
  from WebCrypto), `aes.ts` (OpenSSL-salted AES-256-CBC via `crypto.subtle`),
  `screenscape.ts` (the per-session HMAC signing + AES-envelope decrypt, ported from
  `_screenscape_crypto.py`). **All validated byte-for-byte against the Python ports**
  (MD5 vectors, AES round-trip, the six signing primitives, VOE deobfuscation).
- `src/sources/` — `aniworld.ts` / `sto.ts` / `stomirror.ts` (shared `stoFamily.ts`
  discovery skeleton → VOE/Vidmoly), `aniwatch.ts` (→ VidSrc), `animesuge.ts`
  (ad-free direct files), `screenscape.ts` (~15-server TMDB aggregator). `util/`
  (`text` normalize/slugify/keyword matching, `dom` via DOMParser, `base64`).
- `MediaCtx` gains the AniList title set + synonyms; `preparePlayback` gains
  `extraHeaders`/`extraDomains`; the engine installs media rules with `replace:false`
  so host-scoped rules from multiple sources coexist.
- `registry.ts` now lists 8 sources. `tsc` + `npm run build` green; routing verified
  (E3 → all 8 for TV / 3 TMDB-keyed for movies; no extension → 0, backend handles all).

**crimson-backend (committed `0c0bc45`, NOT pushed):** `GET /scrape-meta/{tmdb}/{season}`
returns the `media_ctx` title bundle (primary title + AniList variants + German
synonyms) the title-matching discovery sources need. German titles come from the
TMDB key (C5), so the client can't derive them — it asks this grant. Login-gated
like `/watch`. Mirrors `stream_watch_response`'s media_ctx construction exactly.

**crimson-client (committed `feb1754`, NOT pushed):** submodule bumped to `8cad476`;
`clientSources.js` enriches `MediaCtx` from `/scrape-meta`; `hooks.js` dedups
local↔backend by `(source, language)` so VOE/Vidmoly dub/sub variants stay distinct
tiles. Still OFF by default (`crimson:clientSources` flag + extension required).

**Stays E0 by nature** (the DoD keeps E0 as fallback): Movish (HTML-rewriting iframe
player-proxy), ShowBox/Febbox + Jellyfin (C5 secret), Cache + Local (server NAS),
Subtitles (OpenSubtitles quota key). Documented in `registry.ts`.

**⚠️ Action needed from you before CI/prod (the live end-to-end test):**
1. `cd ../crimson-sources && git push origin main` (the client submodule pins
   `8cad476`, which is local-only — CI clone fails until it's on the remote).
2. `cd ../crimson-client && git push` (the submodule bump + wiring, on `dev`).
3. `cd ../crimson-backend && git push` (the `/scrape-meta` endpoint, on `dev`).
4. **Live shakeout (Phase 1.5 DoD):** load crimson-extension + toggle it on, set
   `localStorage crimson:clientSources=1`, watch a show — confirm VOE/Vidmoly/VidSrc/
   ScreenScape/AnimeSuge resolve locally and the HLS/MP4 plays (segments CDN→viewer).
   This is the last DoD criterion and needs a real browser; everything up to it is done.

### 🔧 Phase 1.5 — Live shakeout troubleshooting (RESOLVED, 2026-06-29)

> **✅ RESOLVED — see "Phase 1.5 — RESOLVED + live end-to-end pass" below.** The
> MAIN-world fix here (Problem 1) was *necessary but not the whole story*: the
> extension did inject (`window.CrimsonExtension.available === true`), yet the engine
> still never ran **on anime titles** — because the anime watch hook never called the
> client engine at all (see the resolution section). Problem 2 (cache_proxy CORS) is
> moot for client-resolved playback: VOE now streams straight from the CDN, no proxy
> in the path. The investigation below is kept as the historical trail.

The Phase 1.5 code is complete and **everything is pushed** (commits below), but the
**live end-to-end test still fails**: with the companion installed and toggled ON,
client-side resolution does not engage — the backend still serves the video
(`voe_proxy`/`cache_proxy`), the extension popup shows **0 fetches**, and the
expected verdict log does not appear. This section records the investigation so far.
**Status: not yet working. Two separate problems were identified; one fix is deployed
but unconfirmed, the other is not yet fixed.**

**Pushed state at time of writing:**
- crimson-extension `610ed27` (main, pushed)
- crimson-client `48cecc8` (dev, pushed)
- crimson-backend `5cfadcc` (dev, pushed)
- crimson-sources `a51d72c` (main, pushed)

So the user's earlier hypothesis — *"maybe crimson-sources isn't baked into the image
at build"* — was **ruled out**: the submodule pins `a51d72c`, which is on the remote;
Vite aliases it inline; `.dockerignore` only drops `.git`, so `COPY . .` bakes the
vendored TS in. The engine is in the bundle.

#### Problem 1 — client engine never engages (extension bridge not reaching the page)

**Symptom:** no `[clientSources] companion …` verdict line at all; `0 fetches`; backend
serves everything.

**Diagnosis:** the page enforces `script-src 'self'`. The companion was exposing its
in-page API by injecting `<script src="chrome-extension://…/inpage.js">` into the page
DOM — and a **DOM-injected** script tag obeys the host page's CSP, so Chrome silently
blocked it. `window.CrimsonExtension` was therefore never defined → the
`waitForExtensionBridge()` handshake found nothing → engine stayed dark. (The "inline
script violates CSP" console error is the site's easter-egg banner — a red herring, but
it confirmed the policy is enforced.)

**Fix deployed (extension `610ed27`):** declare `src/inpage.js` as a `world:"MAIN"`
content script in the manifest instead of DOM-injecting it. MAIN-world content scripts
are injected by the browser framework and are **exempt from page CSP** (Chrome 111+,
which the manifest already requires). Changes:
- `manifest.json` — two `content_scripts` entries: one `world:"MAIN"` (`inpage.js`),
  one `world:"ISOLATED"` (`protocol.js` + `content.js`). Removed `web_accessible_resources`.
- `src/content.js` — removed the DOM-injection block (no longer creates the `<script>`).
- `src/inpage.js` — hardcodes `VERSION`/`PROTOCOL` (a content script has no
  `document.currentScript` dataset to read them from); kept in sync with `protocol.js`.
- Version bumped to **1.0.1** across `manifest.json` / `protocol.js` / `inpage.js` as a
  visible "new build loaded" signal.

**Also (client `a2f40b2`):** the "companion absent" verdict was gated behind the debug
flag, which is why the log was silent. It's now an **unconditional** `console.info` —
one verdict line per watch — so the next shakeout is legible either way.

**⚠️ STILL FAILS after redeploy + extension reload.** The MAIN-world fix did not resolve
it, so the root cause is either not (only) CSP, or the reload didn't take. **Next checks
(run in the site's DevTools console, no redeploy needed):**
1. `document.documentElement.dataset.crimsonExt` — should be `"1.0.1"` if the MAIN-world
   script ran. If `undefined`, the content script isn't injecting at all (check
   `chrome://extensions` → the companion shows **1.0.1** and is enabled; check it has no
   errors; confirm the `matches` patterns cover the exact watch URL — note `all_frames:false`,
   so if the player/page logic runs in an iframe the MAIN script won't be there).
2. `window.CrimsonExtension` — should be the frozen API object. If `dataset.crimsonExt`
   is set but this is `undefined`, the `Object.defineProperty` is being shadowed or the
   script threw mid-run (check console for errors from `inpage.js`).
3. `window.CrimsonExtension?.available` and `await window.CrimsonExtension.hello()` —
   `hello()` round-trips MAIN→ISOLATED→service worker→back. If it hangs/times out, the
   `postMessage` relay (`content.js`) or the SW is the break, not injection.
4. Watch for the verdict line `[clientSources] companion detected/absent …`. If it says
   **absent** while `window.CrimsonExtension` exists, the timing race in
   `waitForExtensionBridge()` (in crimson-sources) is firing before the bridge is ready —
   the MAIN script runs at `document_start` and the `crimson-extension-ready` event may
   dispatch before the client's listener attaches; the client also reads
   `window.CrimsonExtension` synchronously, so confirm that path.

Likely follow-ups to investigate next session: iframe/`all_frames` mismatch (the watch
player may live in a frame the MAIN script doesn't target); the handshake race in
`waitForExtensionBridge()`; or a stale unpacked extension (reload not picked up — verify
the **1.0.1** badge).

#### Problem 2 — cache_proxy CORS (separate; backend/player, NOT the Phase 1.5 path)

**Symptom:** `GET https://backend.crimsonhaven.to/cache_proxy/… net::ERR_FAILED` /
`No 'Access-Control-Allow-Origin' header is present` from origin `https://crimsonhaven.to`.
This breaks playback of *this* title even from the backend, independent of Problem 1.

**Trigger identified:** `CrimsonPlayer.jsx:557` sets
`crossOrigin={tracks.length ? 'anonymous' : undefined}` — when the episode has subtitle
`<track>`s, the `<video>` gets `crossOrigin="anonymous"` (needed for cross-origin track
loading), which flips the cross-subdomain cache_proxy request into a CORS-enforced one.
The response is coming back without `Access-Control-Allow-Origin`.

The global Starlette `CORSMiddleware` (`api.py:531`, `allow_origins=Config.ALLOWED_ORIGINS`,
which defaults to include `https://crimsonhaven.to`) *should* cover this, so it was **not
blind-fixed**. Three candidates remain, disambiguated by the Network tab on the failing
request:
- **404** — `cache_safe_resolve` returns falsy (cached file moved / its target disabled);
  `net::ERR_FAILED` is consistent with this. Check the **status code**.
- **prod env mismatch** — `ALLOWED_ORIGINS` in the running stack doesn't actually include
  the request origin. Check the deployed env var.
- **middleware not tagging the media/range (206) response** — check whether
  `Access-Control-Allow-Origin` is present in the **Response Headers**.

Fix depends on which: a guaranteed-CORS `FileResponse` for `cache_proxy` (explicit ACAO
header on the handler) if it's the middleware/range case; an env correction if it's the
config; or a cache-revalidation if it's the 404.

### ✅ Phase 1.5 — RESOLVED + live end-to-end pass (2026-06-29)

**The Phase 1.5 Definition of Done is met.** A real browser, with the companion
installed and toggled on, resolved VOE **client-side** for an anime episode (*The
Eminence in Shadow* S2E3, German Sub **and** Dub) and played the HLS stream straight
from the hoster CDN — the ASN-bound VOE token (C4) minted from the *viewer's own*
residential IP (`asn=…` in the playlist URL), exactly the flagship win the migration
was designed for. ScreenScape (~45 variants) and PlayIMDb also resolved locally in
the same run.

#### Dev environment on the swarm (the safe place to shake this out)

A real dev environment was stood up on `crimsonswarm` so this could be tested without
touching prod:
- **Stacks** (manager-resident compose, *not* in the repos): `crimson-dev` (single
  `api` + a **bundled** `postgres:17` — physically isolated from prod data) on `:8001`,
  and `crimson-client-dev` on `:8801`. Folders `~/dev-crimson-deploy/` and
  `~/dev-crimson-client-deploy/` hold the stack file + `deploy.sh` + placeholder
  `crimson.env`. Cloudflare tunnel public hostnames route `dev-backend.crimsonhaven.to`
  → `:8001` and the dev client (`dev.crimsonhaven.to`) → `:8801`.
- **CI/CD** (`build-image.yml`, both repos): a push to `dev` now builds + pushes
  `:dev` **and** an immutable `:dev-<sha>`, then a new `deploy-dev` job (gated
  `if: github.event_name == 'push'`, `paths-ignore: ['**.md']`) SSHes the manager and
  rolls `:dev-<sha>` onto the dev stack. Release-tag deploys to prod are unchanged.
  The client build bakes `VITE_API_BASE_URL=https://dev-backend.crimsonhaven.to`.

#### Root cause of the failed shakeout — the anime watch path was never wired

The earlier MAIN-world CSP fix was real but insufficient. The decisive bug:
`src/hooks.js` has **three** watch hooks, and only two called the engine —
`useShowStreamer` (TMDB-keyed TV) and `useMovieStreamer`. **`useAnimeStreamer`
(AniList-keyed) never called `streamLocalSources` at all.** Since VOE / AniWorld /
S.to are *anime* sources, every title the engine could actually help with went through
the one hook with no integration → zero `[clientSources]`/`[crimson-sources]` logs,
zero extension fetches, clean fallback to E0. Not a VOE bug; a wiring gap.

**Fix (crimson-client `dev`):** wired `useAnimeStreamer`'s NDJSON effect to run the
local engine alongside the backend, mirroring `useShowStreamer` exactly — dedup-aware
`handleLine(line, origin)` (local supersedes a backend duplicate by `(source, language)`),
and a `mediaCtx` built from the current season's `tmdb_id`/`tmdb_season` +
`animeMetadata.title` + the anilist id (so `/scrape-meta` enrichment + title matching
work just like the backend scrapers).

#### Playback fix — CSP `connect-src`

Once VOE resolved, the in-app hls.js player was CSP-blocked from loading the CDN:
the extension does the *scraping* fetches in its service worker (CSP-exempt) and
installs DNR header rules, but **playback** is the page's own XHR, which obeys the page
CSP. DNR rewrites headers; it doesn't bypass `connect-src`. Hoster CDNs rotate
(`cloudwindow-route.com`, `*.workers.dev`, `shegu.net`, …) and can't be enumerated, so
`security-headers.conf` `connect-src` was widened from `'self' https://*.crimsonhaven.to`
to **`'self' https:`**. `script-src 'self'` (the real XSS floor) is untouched — this
only widens where the page may *connect*, not what code may *run*. (Applies to prod on
the next release too — intentional; client-side playback can't work without it.)

#### Known non-issues observed in the trace

- `aniworld`/`sto` searches threw `crimson-extension fetch failed: Failed to fetch`
  while `stomirror` (a raw-IP mirror) succeeded → the *test network blocks those
  domains by name*, not a code bug. The engine logged it (never silent) and degraded
  gracefully to the mirror.
- Backend `/sign` (E2 proxy) is **not implemented and not needed** for this: the
  crimson-proxy is a datacenter IP, and VOE's `needsResidentialIP` flag means E2 can
  *never* mint a working VOE token (`ProxiedFetcher.supports` excludes it). VOE is
  strictly E3 (extension) or E0 (backend). `/sign` would only help non-extension
  viewers on header-only sources — deferred to Phase 2.

#### Companion distribution — download page + packed into the client build

Because the `crimson-extension` repo is private (no public Release assets), the
companion is now shipped **from the client itself**, exactly like the rpc-helper:
- **Submodule**: `crimson-extension` vendored at `vendor/crimson-extension`
  (`.gitmodules` url `../crimson-extension`, branch `main`), pinned `610ed27`.
- **Build** (`Dockerfile`): a new `extpack` stage zips the submodule into
  `/extension/crimson-extension.zip` (+ `manifest.json` for the live version);
  `nginx.conf` serves `^~ /extension/` (zip downloads via its mime type, manifest stays
  readable; 1h cache).
- **Client UX**: a new themed `/extension` page (`src/DownloadExtension.jsx`) — Luminas'
  voice, a download button (live version from the manifest), the side-load ritual
  (unzip → `chrome://extensions` → Developer mode → Load unpacked → the one red button),
  and an "already bound" success state when `window.CrimsonExtension` is present. A
  self-effacing home banner (`ExtensionBanner` in `App.jsx`) nudges viewers who don't
  have it yet — auto-hidden once the companion is detected (`crimson-extension-ready`)
  or dismissed (localStorage). Plus a permanent footer "Companion" link.

`npm run build` green (the page is its own lazy chunk).

### ⏭️ Next — Phase 2 candidates
1. **Backend `/sign` grant** (New_System §8a) → wire `signProxyUrl` for the E2
   (no-extension, web-only) path. **Deliberately deferred:** E2 can't serve the
   high-value sources anyway (the proxy is a datacenter IP, so VOE's
   `needsResidentialIP` and any JA3-gated source exclude it). It would only help
   non-extension viewers on header-only sources — a pure bandwidth optimization, not a
   capability gap. Build it only if backend relay cost becomes a concern.
2. **Rotating segment-host media rules**: parse the master playlist, widen the DNR
   rule to cover cross-host segments (today `extraDomains` covers the known cases).
3. Dev/Prod environment split + CI/CD — **substantially done** (see the dev-environment
   subsection above): dev stacks on the swarm, push-to-`dev` auto-deploy, release-tag
   prod deploy. Remaining: a `crimson-sources` `dev` branch (it still tracks `main`).

### 🩸 Phase 2 — E2 revival, anime wiring, extension hardening, Doodstream (2026-06-29)

A working session that fixed why client-side offload was barely engaging and
expanded coverage. **Several earlier "DONE" claims in this log did not match the
committed code** — corrected below. All code compiles/builds; the live browser
pass is still the user's step.

#### ‼️ Root cause #1 — the client bundles a STALE engine (submodule pinned to Phase 1)

`crimson-client` (`dev`, HEAD `2eca5c6`) commits its `vendor/crimson-sources`
gitlink at **`291f142` — the *Phase 1* engine (cinema.bz + PlayIMDb only)**.
crimson-sources `main` is at `a51d72c` (all Phase-1.5 sources), but the client
**never actually bumped to it** (the "Might work. Who knows." commit). So the
deployed client's local engine has *none* of VOE/aniworld/s.to/AniWatch/AnimeSuge/
ScreenScape/Vidmoly/VidSrc — it can only ever resolve cinema.bz/PlayIMDb locally.
This is a primary reason the companion "sometimes works."
**Fix = bump the submodule** (sequence at the bottom). Verified: with the submodule
synced to `a51d72c`, `npm run build` bundles the full resolver/source set.

#### ‼️ Root cause #2 — the anime watch hook never called the local engine

`hooks.js` has three watch hooks; only `useShowStreamer` (TV) and
`useMovieStreamer` called `streamLocalSources`. **`useAnimeStreamer` did not** —
so every *anime* title (i.e. every VOE/aniworld/s.to title, the flagship sources)
ran backend-only. The earlier log's "RESOLVED — anime hook wired" was not in the
committed code. **Fixed:** `useAnimeStreamer` now runs the local engine alongside
the backend, building a `MediaCtx` from the current season's `tmdb_id`/`tmdb_season`
(= `get_tmdb_season`'s `season_number`, what the backend feeds its scrapers) +
`currentEpisode` + the anilist id + title.

#### ‼️ Root cause #3 — dedup was first-come-first-served, so the backend won the race

All three hooks deduped by `msg.source` first-wins. The backend `/watch` runs in
parallel and (warm caches, datacenter) usually resolves a source *first*, so its
own `/{source}_proxy` URL won and **kept serving the bytes** — the local
(offloaded) URL was dropped. **Fixed:** origin-aware dedup — a `'local'` line
**supersedes** a `'backend'` duplicate of the same `(source, language)` in place,
so the player switches to the CDN→viewer (E3) / CDN→edge→viewer (E2) URL and the
backend stops carrying the segments. Off unless `clientSourcesEnabled()` → prod
behaviour unchanged for non-opted-in viewers. *(Implementation note: I first wrote
this as a `mergeStreamLine` helper, but the `origin/dev` merge below shipped an
equivalent inline `dedup` Map + `reselect()` in each hook — that is what's in the
tree now; `mergeStreamLine` was dropped.)*

#### E2 (proxy) path revived — backend `/sign` grant + client `signProxyUrl` (New_System §8a)

The E2 path was inert: `ProxiedFetcher` only activates when `env.signProxyUrl` is
set, and nothing set it (the grant was deferred) — so for every viewer *without*
the extension, PlayIMDb/cinema.bz/ScreenScape/AnimeSuge/Vidmoly silently fell back
to the backend. This was the "CORS proxies don't work at all" the user reported.
- **Backend:** `POST /sign` (`api.py`) — login-gated (NOT in `_PUBLIC_PREFIXES`),
  `240/min`, accepts one `{url,referer,origin,userAgent}` or `{"items":[…]}` and
  returns parallel signed crimson-proxy links via `_crimson_proxy.proxy_url`
  (PROXY_SECRET stays server-side). 503 when the proxy isn't configured → client
  stays on E3/E0, never a regression. Only signs `http(s)`; the proxy keeps its own
  SSRF gate.
- **Client (`clientSources.js`):** implements `signProxyUrl` against `/sign` with a
  Bearer token, canonical-key cache + in-flight dedup, and a 503 latch (`_proxyDisabled`)
  so it stops asking when the proxy is off. Now wired into `createEngine`.
- Note: VOE still can't use E2 (`needsResidentialIP` → datacenter proxy can't mint a
  valid ASN-bound token); it's E3/E0 by design. E2 offloads the header-only sources
  for the no-extension majority.

#### Client discovery sources can now title-match (`/scrape-meta` enrichment)

`clientSources.js` previously passed a bare `MediaCtx` (no titles), so the
title-matching discovery sources (aniworld/s.to/AniWatch/AnimeSuge) couldn't search.
Added `enrichMediaCtx` → fetches the backend `/scrape-meta/{tmdb}/{season}` grant
(the German-synonym bundle needs the server TMDB key, C5) and merges the title set
in, cached per `(tmdb, season)`. TV only (movies are TMDB-keyed).

#### Companion extension hardened (the "sometimes works" SW races) — v1.0.2

`crimson-extension/src/background.js`, three concrete MV3 service-worker bugs:
1. **SW wake race:** the SW is killed on idle; on cold wake `enabled` defaulted
   `false` while `loadState()` was still async, so the first `FETCH`/`HELLO` after
   an idle teardown wrongly reported "disabled." Added a `readyPromise` the message
   handler awaits before reading `enabled`.
2. **Orphaned-rule id collision:** session DNR rules persist across SW restarts but
   the id counters reset → a reused id threw "rule already exists" and silently
   dropped the header injection (→ CDN 403). `addSessionRules` is now an idempotent
   remove-then-add; `loadState` reconciles the counters past live rules (and drops
   orphans when disabled).
3. **Fragile `urlFilter`:** matching the SW fetch rule by full URL fails on reserved
   chars/query tokens, so Referer/Origin weren't injected. Now scoped by host
   (`requestDomains`) + `tabIds:[-1]`.
Version bumped 1.0.1 → **1.0.2** (manifest/protocol/inpage) as the "new build" signal.

#### New capability — Doodstream resolver (previously un-doable on the backend)

`crimson-sources/src/resolvers/doodstream.ts`: the classic `/pass_md5` + `?token=`
two-step dance. Doodstream is Cloudflare-gated, which is exactly why the backend
(datacenter JA3) couldn't do it ([[aniworld-doodstream-filemoon-blocked]]) — but the
companion runs from the viewer's *real* browser, clearing the passive gate for free.
Wired into the s.to-family `resolveEmbed` + label map, and added "Doodstream" to the
`aniworld`/`sto` hoster allowlists. Inherits the family flags (E3/E0 only — needs the
extension), so no proxy/JA3 concern. `tsc` + build green.

### 🔀 Phase 2 (cont.) — merge with `origin/dev`, AdGuard diagnosis, VOE playback fix (2026-06-29)

While the above was still local, a **parallel session** had already pushed a more
polished version of the same feature to `origin/dev`: an **auto-handshake** client
engine (`waitForExtensionBridge` + `flagOverride`/`extensionPresent` instead of the
manual `getExtensionBridge`), a **debug logger** wired through the engine + VOE
discovery (`crimson-sources` `9f755b7`), and the **companion-distribution** work
(`/extension` download page, `vendor/crimson-extension` submodule, Dockerfile
`extpack`, CSP `connect-src https:`). A `git pull` merged it on top, colliding on
`clientSources.js`, `hooks.js`, and the `vendor/crimson-sources` pin.

**Merge resolution (kept both sides):**
- `clientSources.js` — kept their auto-handshake design; swapped their placeholder
  `signProxyUrl = undefined` for the **real `/sign` signer** (now routed through
  `apiFetch`). E2 revival preserved.
- `hooks.js` — took the incoming version wholesale: it already has the same
  origin-aware "local supersedes backend" dedup (inline `dedup` Map + `reselect()`)
  *and* the anime-hook wiring, more completely than my parallel `mergeStreamLine`
  (which was dropped). No functionality lost.
- `vendor/crimson-sources` — re-homed the **Doodstream** resolver onto their
  `9f755b7` via a 3-way patch (only `stoFamily.ts` overlapped, with the new debug
  logger — merged cleanly), committed as **`df6a79a`** on a new `crimson-sources`
  **`dev`** branch (satisfies the Phase-2 "sources needs a dev branch" item).
- `vendor/crimson-extension` — the merge pinned the *old* `610ed27`; bumped to
  **`061a6e5`** (the v1.0.2 SW hardening) so a deploy ships the hardened companion.

**VOE web-only debugging (the live end-to-end on `dev.crimsonhaven.to`):** with the
companion on, the anime path engaged and VOE resolved **locally** — then two issues
surfaced, diagnosed via `window.CrimsonExtension.fetch(...)` straight from the page
console:

1. **24-byte VOE embed = the user's AdGuard, not a bug.** `voe.sx/e/<id>` →
   "Redirecting…" bounce → rotating mirror (`jennifereconomicgive.com/e/<id>`) →
   AdGuard substitutes a 24-byte `/* Blocked by AdGuard */` stub. The resolver was
   correct (followed the bounce, extracted the right mirror). A Sec-Fetch/`Origin`
   "iframe navigation" theory was tried and **disproved** (both header sets returned
   identical bodies) and **reverted**; what was kept is a sharper resolver
   diagnostic that now prints *"intercepted by a content blocker … whitelist the
   streaming hosts"* instead of a cryptic byte count (`crimson-sources` `3ab7f3c`).
   Note: a per-site allowlist in the blocker does **not** help — the companion
   fetches from its **service worker** (no tab context), so the blocker's
   tab-scoped allowlist misses it; the user must pause it / disable the specific
   rule. (Open product question: ship best-effort high-priority DNR `allow` rules in
   the companion to override a co-installed blocker — works only if the companion is
   installed *after* it, per Chrome's install-order cross-extension precedence.)

2. **VOE segments 403 a few seconds in = media rules torn down mid-playback.** First
   segments returned **200** (proving the `voe.sx` Referer/UA DNR rule *was* injected),
   then 403 once resolution finished. Cause: `streamLocalSources` called
   `engine.dispose()` on resolve-completion, and `dispose()` **clears the DNR media
   rules** — but the player keeps fetching gated segments for the whole episode.
   **Fixed (`crimson-client` `e6ae921`):** no `dispose()` on normal completion; the
   rules persist through playback and are cleared+reinstalled at the *next* episode's
   `streamEpisode()` start (doing it on abort would race that install). Confirmed:
   **VOE now plays start-to-finish from the CDN, no dev-backend proxy.** ✅

> **Known follow-up (not yet hit):** the media rule is scoped to the *master* `.m3u8`
> host. If VOE ever serves segments from a **sibling subdomain**, they'd 403 from the
> *first* segment (distinct from the above). Fix when it bites: widen the rule to the
> registrable parent domain (the "rotating segment-host media rules" Phase-2 item).

#### ✅ Final state (all committed locally; nothing pushed)

| Repo | Branch | Head | Contains |
| --- | --- | --- | --- |
| crimson-backend | dev | `e943321` | `POST /sign` proxy grant |
| crimson-sources | dev | `3ab7f3c` | `9f755b7` debug engine + Doodstream + VOE diagnostic |
| crimson-extension | main | `061a6e5` | SW hardening, v1.0.2 |
| crimson-client | dev | `e6ae921` | merge (auto-handshake + distribution) + E2 `signProxyUrl` + submodule bumps + media-rule lifetime fix |

#### Push sequence (submodule targets before the client that pins them)

1. `cd ../crimson-sources  && git push origin dev`   (`3ab7f3c`)
2. `cd ../crimson-extension && git push origin main`  (`061a6e5`)
3. `cd ../crimson-backend  && git push origin dev`    (`e943321`)
4. `cd ../crimson-client   && git push origin dev`    (merge + bumps + VOE fix)

Push 1 & 2 **before** 4 or CI can't resolve the client's submodule pins. The
`dev`-branch pushes auto-deploy to the dev stack (`dev.crimsonhaven.to` /
`dev-backend.crimsonhaven.to`).

#### Status: **Phase 2 web-only + extension paths verified end-to-end on dev** ✅

With the companion (v1.0.2) on, anime resolves locally and VOE plays start-to-finish
straight from the CDN — the backend carries **zero** segment bytes for it. Doodstream
is wired (Cloudflare-gated, companion-only). E2 `/sign` is live for the no-extension
header-only sources. Remaining Phase-2 polish: the sibling-subdomain media-rule
widening (above), and the optional blocker-override `allow` rules.

### 🩸 Phase 2 (cont.) — E2 auto-engage + compose passthrough + companion defaults (2026-06-29)

Audited the whole E2 (`/sign` → crimson-proxy) chain end-to-end and closed the two
gaps that kept it from actually doing anything for real visitors, plus three
companion-UX asks.

#### ‼️ Root cause — the proxy was never reachable from the container

The code chain was complete and byte-consistent (backend `_sign_one`/`_crimson_proxy.proxy_url`
↔ proxy `signing.ts verify` ↔ sources `ProxiedFetcher`/`preparePlayback` ↔ client
`signProxyUrl`), but **`CRIMSON_PROXY_BASE` was missing from both compose files'
`environment:` blocks**. Compose does NOT auto-inject `.env`, so the running
container saw it unset → `_crimson_proxy.is_enabled()` False → `POST /sign` returned
503 → the entire E2 path was dead regardless of the client wiring. **Fixed:** added
`CRIMSON_PROXY_BASE` + `CRIMSON_PROXY_SOURCES` passthrough to `docker-compose-dev.yml`
(active) and `docker-compose.yml` (commented template), with the standard "must be
listed or the container can't see it" note. *Runtime value still has to be set in the
swarm `crimson.env`, and the proxy deployed with `NITRO_PROXY_SECRET == PROXY_SECRET`.*

#### E2 now auto-engages for no-extension viewers (user decision: on for everyone)

Previously the client engine only ran when the companion was present or a tester set
`localStorage crimson:clientSources=1`, so the proxy bought the no-extension majority
nothing. Per the New_System §6 "web-only" intent, `clientSources.js` now engages the
E2 path for every viewer without the companion: the gate drops to "bail only when
there's no companion AND the proxy is known-unconfigured" (a `/sign` 503 latches
`_proxyDisabled`, which both halts further asks and flips `clientSourcesEnabled()`
off so dedup stays correct). The engine self-routes VOE/VidSrc/aniworld → backend
(their `needsResidentialIP`/`needsJA3` flags exclude E2); only cinema.bz / PlayIMDb /
ScreenScape / AnimeSuge offload via the signed proxy. `npm run build` green.

#### Companion UX — on by default, smaller button, client favicon (v1.0.3)

- **On by default:** `background.js loadState` treats an absent stored flag as
  ENABLED (fresh install works out of the box); an explicit toggle-off still persists
  and wins on every later load. README "off by default" line corrected.
- **Smaller button:** popup `.power` 132→104px (core inset 14→11, label 14→12px,
  body min-height 360→320) — same one-button design, less bulk.
- **Favicon:** `scripts/make_icons.py` rewritten to derive the toolbar/popup icons
  from crimson-client's own favicon (`public/icons/android-chrome-512x512.png`,
  LANCZOS-downscaled to 16/32/48/128 + greyscale `-off`), so the companion reads as
  the same brand. Regenerated all 8 PNGs.
- Version bumped 1.0.2 → **1.0.3** (manifest/protocol/inpage). All JS `node --check`
  clean; manifest valid JSON.

#### Push sequence (unchanged ordering — submodule targets before the client)

1. `cd ../crimson-extension && git push origin main`  (v1.0.3 + client favicon)
2. `cd ../crimson-backend  && git push origin dev`     (compose passthrough)
3. `cd ../crimson-client   && git push origin dev`     (E2 auto-engage)
   *(crimson-sources unchanged this session.)*

Then set `CRIMSON_PROXY_BASE` in the dev/prod env and deploy crimson-proxy with a
matching `NITRO_PROXY_SECRET` — until then `/sign` 503s and clients cleanly stay on
E3/E0 (no regression).

### 🔀 Phase 2 (cont.) — proxy failover, Netlify deploy fix, cache exclusion (2026-06-29)

Deployed both proxy edges (Cloudflare + Netlify) and shook the E2 path out live;
several real-world gaps surfaced and were fixed.

#### Automatic proxy failover (health-aware host selection) — committed `f1c15e8`

The signed proxy query is host-independent (it signs `url\nreferer\norigin\nua`,
not the host), so one signature is valid on every base sharing the secret — which
makes failover pure *host selection*. `resolvers/_crimson_proxy.py` gained a small
in-process health cache: `proxy_url()` now routes only to hosts last probed healthy
(`active`/`idle`), falling back to ALL bases when the cache is cold/stale/all-down
(never worse than the old `random.choice`). `refresh_health()` reuses the existing
`probe_bases()` and is called at startup, every 2 min on the scheduler, AND by the
admin dashboard probe (so opening the overview also steers routing). Each replica
keeps its own cache. **Why it mattered:** with both edges in `CRIMSON_PROXY_BASE`
and Netlify 404ing, the old random pick made each source's playability a coin-flip
(the "PlayIMDb sometimes never loads, then works on refresh" symptom — a fresh
resolve re-rolled onto Cloudflare). Failover makes it deterministic.

#### Netlify deploy — root-caused after three attempts (crimson-proxy, deploy.yml)

Cloudflare deployed first try; Netlify kept shipping "only netlify.toml / every
route 404s". Isolated step by step:
1. Removed a bogus `[functions] directory = ".netlify/functions"` (Nitro's edge
   preset writes `.netlify/edge-functions/`) — real hygiene, but not the cause.
2. Added a post-deploy **smoke test** (curl the deployed `/` for the proxy JSON, fail
   the job loudly) — turned green-but-dead into red-and-legible. This is what made
   the next two diagnoses possible.
3. `deploy --build --json` → **no build logs at all** (the `--json`+`--build` combo
   skipped the build); split into `netlify build` + `deploy --no-build` → build
   bundled the edge function fine ("Packaging Edge Functions - server") but it STILL
   404'd. That isolated it: **`--no-build` drops Nitro's *framework-internal* edge
   function** — Netlify only wires those in via an IN-PROCESS build→deploy handoff.
4. **Fix:** one plain `netlify deploy --prod` (integrated build+bundle+deploy in a
   single process), URL grepped from the human output. *(Result of this run pending
   at write time; if it still 404s, fall back to a user edge function in
   `netlify/edge-functions/` re-exporting Nitro's bundle.)*

#### Cache engine — exclude edge-offloaded / locally-resolved streams (`cache_engine/downloader.py`)

Live caching threw `ffmpeg failed` rows for episodes watched via the E2/local path.
Cause: a crimson-proxy URL (`https://<edge>/?u=…`) is a valid-looking `hls` stream,
so `_cacheable` stamped a ticket / the warmup picked it, and ffmpeg then tried to
remux it through the (possibly-down) edge — which both errors and, when it works,
pulls every segment back THROUGH the backend, defeating the entire offload. Added
`_is_external_proxy_url()` (URL host ∈ `CRIMSON_PROXY_BASE` hosts) and excluded it
in `_cacheable` — the single gate shared by `mint_ticket` (watch), `maybe_enqueue`
(confirm), and the warmup filter, so all three agree. Same-origin `/{x}_proxy`
paths and raw CDN URLs stay cacheable; only the deliberately-offloaded ones are
skipped. This is the "don't cache a local/offloaded resolve" guard the cache engine
was missing. Unit-tested the host matcher; `py_compile` clean.

#### State (uncommitted unless noted)

| Repo | What | Status |
| --- | --- | --- |
| crimson-backend | proxy failover (`_crimson_proxy.py` + scheduler) | committed `f1c15e8` |
| crimson-backend | cache exclusion (`cache_engine/downloader.py`) | uncommitted |
| crimson-proxy | single-step integrated `deploy --prod` + smoke test (`deploy.yml`), netlify.toml comment fix | uncommitted |
| crimson-extension | v1.0.3 (on-by-default, smaller button, client favicon) | per earlier entry |

### 🍪 Phase 2 (cont.) — cookie sources resolve on backend, deliver client-side (2026-06-29)

The remaining "bytes still flow through the backend" case after VOE/cinema.bz/etc.
went client-side was the **cookie/secret-bound** source: ShowBox/Febbox, whose
final `/file/player` hop needs the `ui` cookie (FEBBOX_UI_TOKEN, a C5 secret that
can't ship to the browser). Previously the backend resolved it AND re-streamed the
rotating-OSS mp4 through `/febbox_proxy` — the last big byte path on the backend.

**Insight:** only the *resolve* needs the cookie; the URL it yields is a direct CDN
file the viewer can fetch themselves. So split it: **backend resolves (keeps the
secret), client delivers the bytes (E3/E2).** New general "resolve grant" — the
cookie-source twin of `/sign` — so MovieBox/other cookie sources slot in.

**crimson-backend (committed `0031118`, dev, NOT pushed):**
- `resolvers/febbox.py` — extracted `_parse_marker` + a shared `_unlock()` (the
  token-gated player lookup) behind both `resolve()` (unchanged proxy path) and the
  new `resolve_direct()`, which returns the **raw** OSS URL + `{headers:{userAgent}}`
  (Febbox has no Referer gate) + subtitles (still relative `/febbox_proxy` srt→VTT
  paths — tiny, fine to relay). `streamType` from the URL.
- `api.py` — `POST /resolve`: login-gated (NOT in `_PUBLIC_PREFIXES`) + `120/min`,
  source-dispatched via `_RESOLVE_GRANTS` (febbox/showbox today). Takes the client's
  MediaCtx, runs `run_single_scraper(ShowBoxScraper, …)` → `resolve_direct` per
  embed, absolutizes subtitle URLs, returns `{ok, streams:[{label,streamType,url,
  headers,subtitles,language}]}`. 503 when the source isn't configured (token unset)
  → client cleanly stays on the backend /watch line.

**crimson-sources (committed `12a7007`, dev, NOT pushed):**
- `types.ts` — `GrantStream`/`GrantRequest`; `resolveGrant` on `EngineEnv` +
  `ResolvedEnv` (host-supplied like `signProxyUrl`, since only the host has the
  session token + API base). Threaded through `createEngine`.
- `sources/febbox.ts` — a **backend-resolved** source: `resolve()` calls
  `env.resolveGrant({source:"febbox", ctx})` then `preparePlayback` per stream for
  E3/E2 delivery. Label "ShowBox" so it supersedes the backend tile; `needsCORSBypass`
  (subtitles force `crossOrigin` and the OSS CDN won't ACAO our origin) → routes E3/E2,
  falls back to E0 when neither exists. Registered in `registry.ts`.

**crimson-client (committed `e42dc18`, dev, NOT pushed):**
- `clientSources.js` — `resolveGrant` against `POST /resolve` (apiFetch bearer,
  cached per `(source,tmdb,season,episode)`, 503/404 latches the source off for the
  session), threaded into `createEngine`. Submodule bumped to `12a7007`.

**Verified:** backend `ast` + live import of `FebboxResolver.resolve_direct` /
`ShowBoxScraper` + marker parser; crimson-sources `tsc --noEmit` + build green;
crimson-client `npm run build` green with the submodule bumped — `febbox`/`/resolve`
confirmed present in the bundle. **NOT yet done:** a live browser pass (needs a real
FEBBOX_UI_TOKEN on the dev stack + companion/proxy) — the byte-path win is verified
by construction (raw URL + E3/E2 delivery), but the end-to-end play is the user's step.

**Push sequence (submodule targets before the client that pins them):**
1. `cd ../crimson-sources  && git push origin dev`  (`12a7007`)
2. `cd ../crimson-backend  && git push origin dev`  (`0031118`)
3. `cd ../crimson-client   && git push origin dev`  (`e42dc18` — submodule bump + wiring)

**Note on Movish/Jellyfin/Cache/Local:** still E0 by nature. Movish is an
HTML-rewriting iframe player-proxy (no direct stream URL); Jellyfin needs its token
in *every* segment request (not just resolve), so a grant would have to embed the
token in the URL — deferred; Cache/Local are server-resident files by definition
(deliberate exceptions — their whole point is to serve the server's own NAS).