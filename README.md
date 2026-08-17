# youtubeseerr

A tiny self-hosted YouTube downloader that feeds a Jellyfin library —
musicseerr's video sibling.

Search YouTube (or paste a video/playlist URL), click download: the video is
fetched with yt-dlp as an mp4 (h264 preferred, 1080p cap by default — so a
Raspberry Pi class Jellyfin can direct-play it without transcoding), the
thumbnail is embedded and written as a `-thumb.jpg` sidecar, and the file
lands in a Jellyfin-friendly layout:

```
Videos/<channel>/<title>.mp4
```

Point a Jellyfin "Home Videos" library at that folder (realtime monitoring
on) and downloads appear on their own. If `JELLYFIN_URL` + `JELLYFIN_TOKEN`
are set, the app also nudges Jellyfin's scanner after each download.

## Design

- **Single worker, sequential queue** — built for small servers; parallel
  downloads + ffmpeg would starve a Pi, and YouTube dislikes bursts.
- **Idempotent downloads** — the queue db maps video ids to files (checked
  against the disk, so deleting a file makes it downloadable again), and
  files named with a `[<video id>]` suffix — e.g. from another tool — are
  recognized too. Jellyfin shows filenames, so titles stay clean; the id is
  appended only on a title collision.
- **Remux, never re-encode** — video+audio are merged with `-c copy`.
- One automatic retry per job (transient YouTube 403s), then a manual retry
  button.

## Run

```
docker build -t youtubeseerr .
docker run -p 8092:8092 \
  -v ./videos:/videos -v ./data:/data \
  youtubeseerr
```

Environment: `MAX_HEIGHT` (default 1080), `PLAYLIST_LIMIT` (200),
`COOKIES_FILE`, `POT_PROVIDER_URL`, `JELLYFIN_URL`, `JELLYFIN_TOKEN`,
`JELLYFIN_PATH_PREFIX` (how Jellyfin sees the videos folder, for the
scanner nudge).

Since mid-2026 YouTube demands PO tokens for real formats (`Sign in to
confirm you're not a bot`): run a
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
container next to this one and point `POT_PROVIDER_URL` at it — the image
ships the matching yt-dlp plugin and a JS runtime (deno).

If downloads suddenly start failing, the usual suspect is an outdated
yt-dlp: rebuild the image (`docker compose build --no-cache`).

Legal: intended for personal use. Downloading from YouTube may violate
YouTube's Terms of Service; there is no uploading/seeding involved.
