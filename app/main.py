# youtubeseerr — a Jellyseerr-style downloader for YouTube videos.
# (musicseerr's video sibling — same single-worker queue design)
#
# Search YouTube or paste a URL: the video is fetched with yt-dlp as an
# mp4 (h264 preferred, so a Raspberry Pi class Jellyfin can direct-play it),
# the thumbnail is embedded + written as a sidecar, and the file lands in a
# Jellyfin-friendly layout:
#     Videos/<channel>/<title> [<video id>].mp4
# Jellyfin's "YouTube Videos" library (home videos type, realtime monitoring)
# picks new files up on its own; a best-effort /Library/Media/Updated nudge
# is also sent when JELLYFIN_URL + JELLYFIN_TOKEN are configured.
#
# Design notes:
#   - The [video id] suffix in the filename makes downloads idempotent:
#     existence is checked from the filename, not from a database that could
#     drift from the disk.
#   - Downloads run on a SINGLE worker, sequentially (Raspberry Pi class
#     hardware; parallel downloads + ffmpeg remuxing starve the box, and
#     YouTube dislikes bursts anyway).
#   - Video quality is capped (default 1080p) and h264/avc1 is preferred so
#     playback needs no transcoding. Merging is a remux (-c copy), not a
#     re-encode.
#   - A transient YouTube 403 happens now and then; every job gets one
#     automatic retry, after that a manual retry button in the UI.
#
# Legal: intended for personal use. Downloading from YouTube may violate
# YouTube's Terms of Service; there is no uploading/seeding involved.
import os
import random
import re
import shutil
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

VIDEO_DIR = Path(os.environ.get("VIDEO_DIR", "/videos"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "1080"))
PLAYLIST_LIMIT = int(os.environ.get("PLAYLIST_LIMIT", "200"))
LANG_UI = os.environ.get("LANG_UI", "en")  # "en" or "tr"
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")  # optional, for bot checks
# optional: a bgutil-ytdlp-pot-provider instance. As of mid-2026 YouTube
# demands PO tokens for real video formats ("Sign in to confirm you're not
# a bot") — without a provider most downloads fail.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "")
# optional: nudge Jellyfin's scanner after each download
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_TOKEN = os.environ.get("JELLYFIN_TOKEN", "")
# how Jellyfin sees VIDEO_DIR (host path); empty -> no path translation
JELLYFIN_PATH_PREFIX = os.environ.get("JELLYFIN_PATH_PREFIX", "").rstrip("/")
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "queue.db"
STATIC = Path(__file__).parent / "static"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

app = FastAPI(title="youtubeseerr")


# ── database ────────────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            title TEXT, channel TEXT, duration INTEGER,
            status TEXT NOT NULL DEFAULT 'queued',
            error TEXT, file TEXT,
            added TEXT, finished TEXT, attempts INTEGER DEFAULT 0,
            quality INTEGER, subs TEXT, auto_subs INTEGER DEFAULT 0)""")
        con.execute("PRAGMA journal_mode=WAL")
        # v1 rows predate the quality/subtitle columns
        cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        for name, ddl in (("quality", "quality INTEGER"),
                          ("subs", "subs TEXT"),
                          ("auto_subs", "auto_subs INTEGER DEFAULT 0")):
            if name not in cols:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {ddl}")


# ── helpers ─────────────────────────────────────────────────────────────────
def clean_dir_name(s: str) -> str:
    # '/' and ':' are invalid in folder names; leading dots hide the folder,
    # trailing dots/spaces break some filesystems
    s = (s or "").replace("/", "-").replace(":", "-")
    return re.sub(r"^[ .]+|[ .]+$", "", s)


def clean_file_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "-", s or "")
    return re.sub(r"^[ .]+|[ .]+$", "", s)


def target_path(channel: str, title: str, video_id: str) -> Path:
    # clean "<channel>/<title>.mp4" — Jellyfin shows the filename, so the
    # video id stays OUT of it; it is appended only on a title collision
    ch = clean_dir_name(channel) or "YouTube"
    name = clean_file_name(title)[:150].rstrip(" .") or video_id
    p = VIDEO_DIR / ch / f"{name}.mp4"
    if p.exists():
        p = VIDEO_DIR / ch / f"{name} [{video_id}].mp4"
    return p


def find_existing(video_id: str) -> Path | None:
    """A video counts as present when a finished job's recorded file is
    still on disk (the queue db maps id -> path), or when a legacy/manual
    file carries the id as a "[<id>].mp4" suffix."""
    try:
        with db() as con:
            for r in con.execute(
                    "SELECT file FROM jobs WHERE video_id=? AND file IS NOT NULL"
                    " AND status IN ('done','exists') ORDER BY id DESC",
                    (video_id,)):
                p = VIDEO_DIR / r["file"]
                if p.exists():
                    return p
    except sqlite3.Error:
        pass
    suffix = f"[{video_id}].mp4"
    try:
        for root, _dirs, files in os.walk(VIDEO_DIR):
            for f in files:
                if f.endswith(suffix):
                    return Path(root) / f
    except OSError:
        pass
    return None


def format_string(max_height: int = 0) -> str:
    # avc1 (h264) preferred: the target box (Pi-class Jellyfin) direct-plays
    # it; vp9/av1 would force transcoding. Merging is a remux, no re-encode.
    # A per-job height (from the UI's quality picker) overrides the default;
    # above 1080p there is usually no avc1, so the fallback chain kicks in.
    h = max_height or MAX_HEIGHT
    return (f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best")


SUB_LANG_RE = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z]{2,8})?$")


def clean_sub_langs(subs: str) -> str:
    """Sanitize a comma-separated language list from the client."""
    langs = []
    for lang in (subs or "").split(","):
        lang = lang.strip().lower()
        if lang and SUB_LANG_RE.match(lang) and lang not in langs:
            langs.append(lang)
    return ",".join(langs[:5])


def clean_quality(quality: int) -> int:
    """0 = server default; otherwise clamp to something yt-dlp understands."""
    if not quality:
        return 0
    return max(144, min(int(quality), 2160))


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def entry_summary(e: dict) -> dict:
    vid = e.get("id") or ""
    return {
        "video_id": vid,
        "title": e.get("title") or "?",
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": int(e.get("duration") or 0),
        "exists": find_existing(vid) is not None,
    }


def _ydl_base_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 30}
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}}
    return opts


def resolve_url(url: str) -> dict:
    """Flat-extract a URL: a single video or a playlist/channel of entries."""
    from yt_dlp import YoutubeDL
    with YoutubeDL({**_ydl_base_opts(), "extract_flat": "in_playlist",
                    "playlist_items": f"1:{PLAYLIST_LIMIT}"}) as y:
        info = y.extract_info(url, download=False)
    if not info:
        raise RuntimeError("could not read that URL")
    if info.get("entries") is not None:
        entries = [e for e in info["entries"] if e and e.get("id")]
        return {"kind": "playlist", "title": info.get("title") or "playlist",
                "entries": entries}
    return {"kind": "video", "entries": [info]}


# ── API: pages, health, search ──────────────────────────────────────────────
@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    lang = "tr" if LANG_UI == "tr" else "en"
    return Response(html.replace("__LANG__", lang, 1), media_type="text/html")


@app.get("/api/health")
def health():
    return {"status": "ok", "queue_db": DB_PATH.exists(),
            "videos_writable": os.access(VIDEO_DIR, os.W_OK)}


@app.get("/api/search")
def search(q: str, limit: int = 12):
    from yt_dlp import YoutubeDL
    n = min(max(limit, 1), 20)
    with YoutubeDL({**_ydl_base_opts(), "extract_flat": True}) as y:
        found = y.extract_info(f"ytsearch{n}:{q}", download=False)
    results = [entry_summary(e) for e in (found or {}).get("entries") or []
               if e and e.get("id")]
    return {"results": results}


@app.get("/api/thumb")
def thumb(v: str):
    # The server builds the ytimg URL itself — this cannot become an open
    # proxy (the browser never talks to YouTube's CDN directly).
    if not VIDEO_ID_RE.match(v):
        raise HTTPException(400, "not a video id")
    for name in ("mqdefault.jpg", "hqdefault.jpg"):
        try:
            r = requests.get(f"https://i.ytimg.com/vi/{v}/{name}", timeout=15)
            if r.ok:
                return Response(r.content, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=604800"})
        except requests.RequestException:
            continue
    raise HTTPException(404, "no thumbnail")


# ── API: queueing ───────────────────────────────────────────────────────────
def enqueue_entries(entries: list, quality: int = 0, subs: str = "",
                    auto_subs: bool = False) -> dict:
    """Queue flat entries; skip files already on disk and ids already
    waiting in the queue."""
    added, skipped = 0, 0
    with db() as con:
        pending = {r["video_id"] for r in con.execute(
            "SELECT video_id FROM jobs WHERE status IN ('queued','downloading')")}
        for e in entries:
            vid = e.get("id") or ""
            if not vid:
                continue
            if vid in pending or find_existing(vid):
                skipped += 1
                continue
            con.execute(
                "INSERT INTO jobs (video_id, title, channel, duration, added,"
                " quality, subs, auto_subs) VALUES (?,?,?,?,?,?,?,?)",
                (vid, e.get("title") or "?",
                 e.get("channel") or e.get("uploader") or "",
                 int(e.get("duration") or 0),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 quality, subs, int(auto_subs)))
            pending.add(vid)
            added += 1
    return {"added": added, "skipped": skipped}


class DownloadRequest(BaseModel):
    video_id: str = ""   # from search results
    url: str = ""        # pasted URL (video, playlist or channel)
    quality: int = 0     # max height (720, 1080, ...); 0 = server default
    subs: str = ""       # comma-separated languages ("tr,en"); "" = none
    auto_subs: bool = False  # fall back to YouTube's auto captions


@app.post("/api/download")
def download(req: DownloadRequest):
    quality = clean_quality(req.quality)
    subs = clean_sub_langs(req.subs)
    auto_subs = bool(req.auto_subs and subs)
    if req.video_id:
        if not VIDEO_ID_RE.match(req.video_id):
            raise HTTPException(400, "not a video id")
        with db() as con:
            open_job = con.execute(
                "SELECT id, status FROM jobs WHERE video_id=?"
                " AND status IN ('queued','downloading')",
                (req.video_id,)).fetchone()
            if open_job:
                return {"job_id": open_job["id"], "status": open_job["status"],
                        "note": "already queued"}
            existing = find_existing(req.video_id)
            if existing:
                return {"status": "exists",
                        "file": str(existing.relative_to(VIDEO_DIR))}
            cur = con.execute(
                "INSERT INTO jobs (video_id, added, quality, subs, auto_subs)"
                " VALUES (?,?,?,?,?)",
                (req.video_id,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 quality, subs, int(auto_subs)))
            return {"job_id": cur.lastrowid, "status": "queued"}

    url = (req.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "paste a YouTube link or send a video_id")
    info = resolve_url(url)
    result = enqueue_entries(info["entries"], quality, subs, auto_subs)
    result["kind"] = info["kind"]
    if info["kind"] == "playlist":
        result["playlist"] = info.get("title", "")
    return result


@app.post("/api/retry/{job_id}")
def retry(job_id: int):
    with db() as con:
        r = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such job")
        if r["status"] != "error":
            return {"job_id": job_id, "status": r["status"], "note": "not in error state"}
        con.execute("UPDATE jobs SET status='queued', error=NULL, attempts=0,"
                    " finished=NULL WHERE id=?", (job_id,))
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/job/{job_id}")
def job_status(job_id: int):
    with db() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no such job")
    return _with_progress(dict(r))


@app.get("/api/queue")
def queue():
    with db() as con:
        rows = [_with_progress(dict(r)) for r in con.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 30")]
        pending = con.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','downloading')"
        ).fetchone()["c"]
    return {"jobs": rows, "pending": pending}


# ── download progress ───────────────────────────────────────────────────────
# In-memory only (single process): job_id -> percent. Queued jobs have no
# entry; the worker clears the entry when a job reaches a terminal state.
PROGRESS: dict[int, float] = {}


def _progress_hook(job_id: int):
    """yt-dlp calls this per chunk. Video and audio download as two phases,
    so the raw per-phase percent would jump 0→100 twice; keeping the maximum
    gives a monotonic number (the video phase is the bulk of the bytes).
    Capped at 99 — 100% is only ever shown by the 'done' state itself."""
    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if total:
            pct = min(99.0, d.get("downloaded_bytes", 0) * 100.0 / total)
            PROGRESS[job_id] = max(PROGRESS.get(job_id, 0.0), pct)
    return hook


def _with_progress(row: dict) -> dict:
    if row.get("status") == "downloading":
        row["progress"] = round(PROGRESS.get(row["id"], 0.0))
    return row


# ── Jellyfin nudge (best effort — realtime monitoring is the primary path) ──
def notify_jellyfin(target: Path):
    if not (JELLYFIN_URL and JELLYFIN_TOKEN):
        return
    path = str(target)
    if JELLYFIN_PATH_PREFIX:
        path = JELLYFIN_PATH_PREFIX + "/" + str(target.relative_to(VIDEO_DIR))
    try:
        requests.post(
            f"{JELLYFIN_URL}/Library/Media/Updated",
            json={"Updates": [{"Path": path, "UpdateType": "Created"}]},
            # Jellyfin 12+: legacy X-Emby-Token header is deprecated (removed in 13)
            headers={"Authorization": f"MediaBrowser Token=\"{JELLYFIN_TOKEN}\""}, timeout=10)
    except requests.RequestException:
        pass  # the library's realtime monitor will catch it anyway


# ── worker: single-lane downloads ───────────────────────────────────────────
def _download_one(job: dict):
    from yt_dlp import YoutubeDL

    vid = job["video_id"]
    existing = find_existing(vid)
    if existing:
        return "exists", str(existing.relative_to(VIDEO_DIR)), ""

    tmp = TMP_DIR / f"job-{job['id']}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        opts = {
            **_ydl_base_opts(),
            "format": format_string(int(job.get("quality") or 0)),
            "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True, "retries": 5,
            "progress_hooks": [_progress_hook(job["id"])],
            "writethumbnail": True,
            "postprocessors": [
                # remux/tag only — no re-encode
                {"key": "FFmpegMetadata"},
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                {"key": "EmbedThumbnail", "already_have_thumbnail": True},
            ],
        }
        with YoutubeDL(opts) as y:
            info = y.extract_info(watch_url(vid), download=True)

        # Subtitles are fetched in a SEPARATE, best-effort pass: YouTube's
        # caption endpoints rate-limit aggressively (429), and inside the
        # main download a subtitle failure would sink the whole video
        # (happened live on 2026-08-17). A failure here only leaves a note.
        sub_note = ""
        subs = clean_sub_langs(job.get("subs") or "")
        if subs:
            # "en" should also catch "en-US"-style variants
            langs = []
            for lang in subs.split(","):
                langs += [lang, lang + ".*"]
            try:
                with YoutubeDL({
                    **_ydl_base_opts(),
                    "skip_download": True,
                    "outtmpl": str(tmp / "%(id)s.%(ext)s"),
                    "writesubtitles": True,
                    "subtitleslangs": langs,
                    "writeautomaticsub": bool(job.get("auto_subs")),
                    # be gentle with the caption endpoints
                    "sleep_interval_subtitles": 3,
                    "postprocessors": [{"key": "FFmpegSubtitlesConvertor",
                                        "format": "srt"}],
                }) as y:
                    y.extract_info(watch_url(vid), download=True)
            except Exception as e:
                sub_note = f"subtitles failed: {str(e)[:150]}"

        files = list(tmp.glob("*.mp4"))
        if not files:
            raise RuntimeError("download produced no .mp4 file")
        f = files[0]

        title = (info or {}).get("title") or job.get("title") or vid
        channel = ((info or {}).get("channel") or (info or {}).get("uploader")
                   or job.get("channel") or "YouTube")
        target = target_path(channel, title, vid)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(target))
        # sidecar thumbnail: "<video basename>-thumb.jpg" — Jellyfin treats it
        # as artwork owned by the video (never as a separate photo item)
        thumbs = list(tmp.glob("*.jpg"))
        if thumbs:
            shutil.move(str(thumbs[0]),
                        str(target.with_name(target.stem + "-thumb.jpg")))
        # subtitles: "<id>.<lang>.srt" -> "<video basename>.<lang>.srt",
        # which Jellyfin picks up as an external subtitle track
        for sub in tmp.glob("*.srt"):
            parts = sub.name.split(".")
            lang = parts[-2].lower() if len(parts) >= 3 else "und"
            shutil.move(str(sub),
                        str(target.with_name(f"{target.stem}.{lang}.srt")))
        # keep the row's metadata fresh (queue rows from pasted single URLs
        # start out title-less)
        with db() as con:
            con.execute("UPDATE jobs SET title=?, channel=?, duration=? WHERE id=?",
                        (title, channel, int((info or {}).get("duration") or 0),
                         job["id"]))
        notify_jellyfin(target)
        return "done", str(target.relative_to(VIDEO_DIR)), sub_note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def worker():
    while True:
        try:
            with db() as con:
                r = con.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
                ).fetchone()
            if not r:
                time.sleep(3)
                continue
            with db() as con:
                con.execute("UPDATE jobs SET status='downloading' WHERE id=?", (r["id"],))
            try:
                status, file, note = _download_one(dict(r))
                with db() as con:
                    # a done job can still carry a note (e.g. subtitles
                    # failed) — the UI shows it as a tooltip, not a failure
                    con.execute(
                        "UPDATE jobs SET status=?, file=?, error=?, finished=? WHERE id=?",
                        (status, file, note or None,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            except Exception as e:
                traceback.print_exc()
                with db() as con:
                    attempts = (con.execute(
                        "SELECT attempts FROM jobs WHERE id=?",
                        (r["id"],)).fetchone()["attempts"] or 0)
                    if attempts < 2:
                        PROGRESS.pop(r["id"], None)  # retry restarts from 0%
                        # transient YouTube 403s usually pass on a fresh
                        # extraction, and on 2026-08-17 they came in pairs —
                        # two automatic retries, then it's manual
                        con.execute("UPDATE jobs SET status='queued', attempts=? WHERE id=?",
                                    (attempts + 1, r["id"]))
                        time.sleep(20 + attempts * 40)
                    else:
                        con.execute(
                            "UPDATE jobs SET status='error', error=?, finished=? WHERE id=?",
                            (str(e)[:500],
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            PROGRESS.pop(r["id"], None)
            # be gentle with YouTube — a slightly random gap looks less
            # robotic than a fixed one on long bulk queues
            time.sleep(2 + random.random() * 3)
        except Exception:
            traceback.print_exc()
            time.sleep(10)


@app.on_event("startup")
def startup():
    init_db()
    # put jobs interrupted by a restart back into the queue
    with db() as con:
        con.execute("UPDATE jobs SET status='queued' WHERE status='downloading'")
    threading.Thread(target=worker, daemon=True).start()
