# Crimson Backend — Mobile (Android) Integration Guide

Everything an Android client needs to talk to the Crimson streaming backend.
This is a plain HTTP/JSON API (FastAPI). No SDK — use Retrofit/OkHttp + a JSON
parser (Moshi/kotlinx.serialization). There is **one** non-standard transport:
the `/watch` endpoint streams **NDJSON** (see [§5](#5-watch--progressive-ndjson-stream)).

---

## 1. Basics

- **Base URL (dev):** `https://dev-backend.crimsonhaven.to`
  (Production: `https://crimsonhaven.to`’s backend host — confirm with the API owner.)
- **Format:** JSON request/response, UTF-8. Send `Content-Type: application/json` on POSTs.
- **Auth:** none for browsing; account endpoints use a `Authorization: Bearer <token>` header (see [§7](#7-accounts-auth)).
- **Success flag:** most JSON responses include `"success": true`.
- **Errors:** non-2xx responses look like:
  ```json
  { "success": false, "error": "Show not found", "status_code": 404 }
  ```
  Validation errors (bad/missing query params) return HTTP **422** (FastAPI default).

### ID model (important)
Two id systems are used together:
- **`tmdb_id`** + **`season_number`** + **`episode_number`** → the canonical way to play and to fetch metadata. TMDB’s season list is the source of truth.
- **`anilist_id`** → an alternate per-season id used for discovery/navigation and as the favorites/progress key.
- A season may have **`anilist_id: null`** (e.g. long shows TMDB splits into seasons). That is valid — still playable by `tmdb_id`. Never treat `null` anilist_id as an error.

---

## 2. Discovery endpoints

### `GET /trending?limit=10`
```json
{
  "success": true,
  "count": 10,
  "animes": [
    { "title": "...", "tmdb_id": 1234, "anilist_id": 567,
      "poster": "https://image.tmdb.org/t/p/w500/xxx.jpg",
      "year": "2023", "vote_average": 8.1 }
  ]
}
```

### `GET /search/anime?query_name=<text>`
```json
{
  "success": true, "query": "naruto", "count": 3,
  "suggestions": [ { /* same item shape as trending animes[] */ } ]
}
```

### `GET /catalogue?category=<optional>`
Full local catalogue, no external calls. Good for a “browse by category” screen.
`category` is optional (e.g. `TV`, `MOVIE`, `OVA`, `ONA`, `SPECIAL`, `TV_SHORT`, `MUSIC`, `UNKNOWN`).
```json
{
  "success": true, "count": 120, "total": 6800,
  "categories": [ { "category": "TV", "count": 4200 }, { "category": "MOVIE", "count": 900 } ],
  "animes": [
    { "anilist_id": 567, "title": "...", "title_romaji": "...", "title_english": "...",
      "category": "TV", "year": 2023, "tmdb_id": 1234, "season_number": 1, "poster": null }
  ]
}
```
`categories` always covers the whole catalogue; `animes` is filtered when `category` is passed. `poster` is often `null` here.

---

## 3. Show / season metadata

### `GET /seasons/{anilist_id}`
All seasons of the show this anilist_id belongs to. Use each season’s `tmdb_id` + `tmdb_season` to build the next calls.
```json
{
  "success": true, "anilist_id": 567, "title": "...", "total_seasons": 2,
  "seasons": [
    { "season_number": 1, "anilist_id": 567, "tmdb_id": 1234, "tmdb_season": 1,
      "name": "Season 1", "poster": "https://image.tmdb.org/t/p/w500/...jpg",
      "summary": "...", "air_date": "2023-01-05",
      "episode_count": 12, "title_romaji": "...", "title_english": "...", "anime_type": "TV" }
  ],
  "extras": [ /* specials/OVAs/movies, shape: {anilist_id, anime_type, title_romaji, title_english, start_year} */ ]
}
```

### `GET /info/{tmdb_id}?season=<n>`  ← primary metadata call for a season
Flat merged TMDB + AniList object. This is what you render on a detail/episodes screen.
```json
{
  "success": true,
  "tmdb_id": 1234, "anilist_id": 567,
  "current_season": 1,
  "available_seasons": [1, 2, 3],
  "title": "...",
  "description": "...",        // never empty: AniList → TMDB season → TMDB show
  "summary": "...",
  "poster": "https://image.tmdb.org/t/p/w500/...jpg",
  "backdrop": "https://image.tmdb.org/t/p/original/...jpg",
  "banner": "https://...",     // from AniList, may be null
  "cover": "https://...",      // from AniList, may be null
  "status": "FINISHED",
  "total_episodes": 12,
  "next_airing_episode": null,
  "episodes_list": [
    { "episode_number": 1, "title": "...", "thumbnail": "https://...|null",
      "overview": "...", "air_date": "2023-01-05", "url": null }
  ]
}
```
Notes:
- **Play by `episode_number` from `episodes_list`** against `tmdb_id` + the `season` you queried.
- `episodes_list` already uses the *longer* of AniList vs TMDB episode counts so every episode is reachable.
- `available_seasons` lets you build a season switcher.

### Other metadata endpoints (optional)
- `GET /show/{tmdb_id}` → `{ success, show, seasons:[...], extras:[...] }` (show-level + all seasons).
- `GET /season/{tmdb_id}/{season_number}` → `{ success, tmdb_id, season_number, anilist_id, tmdb_metadata, anilist_metadata }` (nested, if you prefer the split shape over `/info`).
- `GET /anilist/{anilist_id}` → `{ success, anilist_id, tmdb_id, season_number }` (id resolver).

---

## 4. Image URLs
TMDB image fields already come back as **fully-qualified URLs** (`https://image.tmdb.org/t/p/w500/...`). Load them directly with Coil/Glide. AniList `banner`/`cover` are also full URLs. Any field may be `null` — show a placeholder.

---

## 5. `/watch` — progressive NDJSON stream

> **This is the one endpoint that does NOT return a single JSON body.**

### `GET /watch/{tmdb_id}/{season_number}/{episode_number}`
- **Response `Content-Type`:** `application/x-ndjson`
- The server scrapes multiple sources concurrently and **emits one JSON object per line, flushed as each source resolves** — the fastest source arrives first, you don’t wait for the slowest. Read the body **incrementally**, line by line; do **not** buffer the whole response.

Line types, in order:
```jsonc
{"type":"meta","success":true,"tmdb_id":1234,"season_number":1,"episode_number":1,"anilist_id":567,"title":"..."}
{"type":"stream","source":"AnimeSuge","streamType":"hls","url":"https://.../playlist.m3u8"}
{"type":"stream","source":"Movish","streamType":"iframe","url":"https://dev-backend.crimsonhaven.to/movish_proxy/h/.../..."}
{"type":"done","count":2}
```
- `meta` — always first; ids + resolved title.
- `stream` — zero or more; **append each to your source list as it arrives** so the user can start playing the first one immediately. Fields: `source` (display label), `streamType` (`hls` | `mp4` | `iframe`), `url`.
- `done` — terminal; `count` = number of `stream` lines emitted. If `count` is 0, no source was found.

### Stream `streamType` → how to play
| `streamType` | What it is | Android handling |
|--------------|-----------|------------------|
| `hls`  | `.m3u8` playlist (often same-origin proxied) | ExoPlayer **HlsMediaSource** |
| `mp4`  | progressive file | ExoPlayer **ProgressiveMediaSource** |
| `iframe` | a backend-hosted player page or a proxied web player (ad-free) | Load `url` in a **WebView** (full-screen, JS enabled). These are HTML pages, not raw video — ExoPlayer cannot play them. |

All `url`s are ready to use as-is:
- `hls`/`mp4` and proxy/player `iframe` URLs are **absolute and same-origin** to the backend (the backend signs/proxies them) — pass them straight to ExoPlayer/WebView. No extra headers, no Referer needed; the backend injects whatever upstream needs.
- A few legacy `iframe` sources may be third-party absolute URLs — still just load them in the WebView.
- Prefer `hls`/`mp4` sources for the native player; fall back to `iframe`/WebView when that’s all that’s available. Let the user pick when multiple sources arrive.

### Consuming NDJSON on Android (OkHttp)
```kotlin
val req = Request.Builder().url("$BASE/watch/$tmdbId/$season/$episode").build()
client.newCall(req).execute().use { resp ->
    val source = resp.body!!.source()
    while (true) {
        val line = source.readUtf8Line() ?: break   // blocks until next line / EOF
        if (line.isBlank()) continue
        val obj = json.decodeFromString<WatchLine>(line)
        when (obj.type) {
            "meta"   -> onMeta(obj)
            "stream" -> onStream(obj)   // add to UI list immediately
            "done"   -> break
        }
    }
}
```
Run this off the main thread (Dispatchers.IO). Cancelling the OkHttp call mid-stream is fine — the backend cancels its workers when the client disconnects.

### Legacy alias
`GET /watch/{anilist_id}/{episode_number}` — resolves the anilist_id and **301-redirects** to the canonical `/watch/{tmdb_id}/{season}/{episode}` for normal seasons (OkHttp follows redirects by default), or streams directly for extras (specials/OVAs/movies). Prefer calling the canonical 3-part route directly when you have `tmdb_id` + `season`.

---

## 6. The `/player` and proxy endpoints (no action needed)
You generally never call these directly — they appear inside `iframe` stream `url`s and just work in a WebView:
- `/player?src=...&type=hls|mp4&title=...` — backend-hosted themed player wrapping a proxied stream.
- `/vidking_proxy/...`, `/movish_proxy/...`, `/playimdb_proxy?...`, `/animesuge_proxy?...`, `/jellyfin_proxy/...` — same-origin reverse proxies (ad-stripping / token injection / HLS rewriting). Just load the URL the `stream` line gave you.

---

## 7. Accounts (auth)

Passwordless, P-Stream-style. **An account *is* an Ed25519 keypair derived from a 12-word BIP39 mnemonic.** The mnemonic / private key **never leave the device**. The server stores only the public key and verifies signatures.

### Client-side key derivation (must match exactly)
```
12-word BIP39 mnemonic
  → BIP39 seed            (PBKDF2-HMAC-SHA512, empty passphrase, 64 bytes)
  → seed[:32]             (first 32 bytes = Ed25519 private seed)
  → Ed25519 keypair       (compatible with @noble/ed25519)
public_key = 32-byte pubkey, lowercase hex (64 hex chars)
signature  = 64-byte sig over the challenge’s UTF-8 bytes, lowercase hex (128 hex chars)
```
Android libraries: a BIP39 lib for mnemonic↔seed (e.g. `novacrypto:BIP39`), and **BouncyCastle** `Ed25519PrivateKeyParameters(seed32)` for keygen/signing (Ed25519 sign over the raw challenge bytes). Store the mnemonic in the Android Keystore / EncryptedSharedPreferences.

### Sign-in flow
```
1. POST /auth/challenge   { "public_key": "<hex64>" }
   → { "public_key", "challenge": "<string>", "expires_at" }   (challenge valid ~5 min, one-time)

2. Sign the exact `challenge` string (UTF-8 bytes) with the Ed25519 private key → signature hex128.

3a. New device/account:
    POST /auth/register   { "public_key", "challenge", "signature", "label"? }
    → 200 AuthResponse  |  409 if key already registered (then do 3b with the SAME challenge)

3b. Existing account:
    POST /auth/login      { "public_key", "challenge", "signature" }
    → 200 AuthResponse  |  404 if key not registered (then do 3a with the SAME challenge)
```
A 409/404 leaves the challenge intact, so the common pattern is: try `login`, on 404 fall back to `register` with the same challenge (or vice-versa).

**AuthResponse:**
```json
{ "public_key": "<hex64>", "label": "My Phone",
  "session_token": "<opaque token>", "expires_at": "2026-07-07T...", "created": false }
```
Save `session_token` securely; send it as `Authorization: Bearer <session_token>` on all `/account/*` calls. Sessions last 30 days.

- `POST /auth/logout` (Bearer) → revoke current session. `{ "success": true }`
- `GET /account/me` (Bearer) → `{ success, public_key, label, created_at, last_login_at, favorites_count, progress_count }`

### Favorites (show-level) — all require Bearer
- `GET /account/favorites` → `{ success, count, favorites:[...] }`
- `POST /account/favorites` body:
  ```json
  { "tmdb_id": 1234, "anilist_id": 567, "season_number": 1,
    "media_type": "TV", "title": "...", "poster": "https://..." }
  ```
  (at least one of `tmdb_id`/`anilist_id` required.) → `{ success, favorite }`
- `DELETE /account/favorites?tmdb_id=&anilist_id=&item_key=` → remove one (provide `item_key`, or `tmdb_id`/`anilist_id`). 404 if not found.

### Watch progress (per-episode) — all require Bearer
- `POST /account/progress` body:
  ```json
  { "tmdb_id": 1234, "anilist_id": 567, "season_number": 1, "episode_number": 5,
    "position_seconds": 540.0, "duration_seconds": 1440.0,
    "status": "in_progress", "title": "...", "poster": "https://..." }
  ```
  `status` is optional — the server marks `completed` automatically when `position/duration ≥ 0.9`, else `in_progress`. Call this periodically during playback and on pause/exit. → `{ success, progress }`
- `GET /account/progress?status=in_progress|completed` → `{ success, count, progress:[...] }`
- `GET /account/continue-watching` → `{ success, count, items:[...] }` — in-progress episodes only, most-recent first (build a “Continue Watching” row).
- `GET /account/recent?limit=20` → `{ success, count, items:[...] }` — recently-watched episodes of **any** status (in-progress **and** completed), most-recent first (build a “Recent” / watch-history row that survives finishing an episode).
- `DELETE /account/progress?item_key=&tmdb_id=&anilist_id=&season_number=&episode_number=` → remove one. 404 if not found.

---

## 8. Health
`GET /health` → `{ status: "healthy", database, entries_count, scrapers_available, resolvers_available, jellyfin_configured }`. Use for a connectivity/diagnostics check.

---

## 9. Typical app flow (putting it together)
1. **Home:** `GET /trending` (+ `GET /catalogue` for browse).
2. **Search:** `GET /search/anime?query_name=`.
3. **Detail screen:** from a result’s `anilist_id` → `GET /seasons/{anilist_id}` for the season list; for the selected season → `GET /info/{tmdb_id}?season={tmdb_season}` for description + `episodes_list`.
4. **Play:** open `GET /watch/{tmdb_id}/{season}/{episode}`, read the NDJSON stream, show sources as they arrive; play `hls`/`mp4` in ExoPlayer or `iframe` in a WebView. Report `POST /account/progress` while playing.
5. **Accounts (optional):** mnemonic-based register/login, then favorites + continue-watching.

---

### Quick reference
| Need | Endpoint |
|------|----------|
| Trending | `GET /trending?limit=` |
| Search | `GET /search/anime?query_name=` |
| Browse | `GET /catalogue?category=` |
| Seasons of a show | `GET /seasons/{anilist_id}` |
| Season metadata + episodes | `GET /info/{tmdb_id}?season=` |
| **Play (NDJSON stream)** | `GET /watch/{tmdb_id}/{season}/{episode}` |
| Get challenge | `POST /auth/challenge` |
| Register / Login | `POST /auth/register` · `POST /auth/login` |
| Favorites | `GET/POST/DELETE /account/favorites` |
| Progress / Continue / Recent | `GET/POST/DELETE /account/progress` · `GET /account/continue-watching` · `GET /account/recent` |
