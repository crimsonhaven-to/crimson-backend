# Music CDN (Cloudflare R2 behind your own domain)

An optional, off-site copy of the music library that players also stream from.
Without it, songs live only on the music share and stream through the api.

| Piece | Does |
| --- | --- |
| R2 bucket `crimson-music` | holds every song and cover, at the same paths as on the share |
| this Worker, on `cdn.<your domain>` | checks the link's signature and streams from the bucket (Range included); accepts uploads with the shared secret |
| music-worker | copies each ready song and its cover, then marks it `mirrored_at`; the first run copies the whole existing library, five songs per 10 s tick |
| api | hands out signed CDN links for mirrored songs, its own `/music_stream` links for the rest |

Why a Worker: R2's own pre-signed URLs only work on `<account>.r2.cloudflarestorage.com`,
never on a custom domain, and a public bucket would put the library on the open web.

## Setup

```sh
cd deploy/music-cdn
npx wrangler login
npx wrangler r2 bucket create crimson-music
openssl rand -hex 32                      # the shared secret
npx wrangler secret put CDN_SECRET        # paste it
npx wrangler deploy                       # also attaches cdn.crimsonhaven.to (see wrangler.toml)
```

The domain's zone must be on the same Cloudflare account. For another domain or
bucket name, edit `wrangler.toml` first.

Then on **api** and **music-worker**:

```
MUSIC_CDN_URL=https://cdn.crimsonhaven.to
MUSIC_CDN_SECRET=<the same secret>
```

The startup report shows `[  on] Music CDN copy`, and the music-worker log shows
`copied <path> to the CDN` as it catches up. The Music page counts songs "backed up".

## Behaviour

- **Links** are signed with `MUSIC_CDN_SECRET` and expire after a week, like the api's.
- **A re-download** (a new recording picked) clears `mirrored_at`, so the new file is copied too.
- **The CDN down** pauses copying for two minutes; songs not yet copied still play from the api.
  Songs already copied play only from the CDN, so an outage there stops them.
- **Turning it off**: unset the two variables. Every song plays from the api again; the bucket stays.
- **Nothing is ever deleted** from the bucket, matching the share.

## Restoring the share from the bucket

The keys are the share's paths, so a plain copy restores it, for example with
rclone and an R2 API token (read-only is enough):

```sh
rclone copy r2:crimson-music /mnt/crimsonmusic --progress
```

## Local test

```sh
npx wrangler dev --local --var CDN_SECRET:test
# backend: MUSIC_CDN_URL=http://127.0.0.1:8787 MUSIC_CDN_SECRET=test
```
