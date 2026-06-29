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

### ⏭️ Next — Step 3 candidates
1. **Live shakeout:** load crimson-extension, set `crimson:clientSources=1`, watch a
   show/movie, confirm cinema.bz/PlayIMDb resolve locally + segments go CDN→viewer.
2. **Backend `/sign` grant** (New_System §8a) → wire `signProxyUrl` so the E2
   (no-extension) path works; then web-only viewers benefit too.
3. **Rotating segment-host media rules** (playback.ts note): parse the master, widen
   the DNR rule to cover cross-host segments.
4. Then the heavier sources: VOE (E3, the ASN flagship), ScreenScape (WebCrypto), Vidmoly.