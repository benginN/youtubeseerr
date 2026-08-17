# video-kutusu tests — run on the Mac before anything ships to the Pi.
# yt_dlp is faked (no network); the FastAPI app runs against a temp dir.
import os
import sys
import tempfile
import types
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="video-kutusu-test-")
os.environ["VIDEO_DIR"] = str(Path(TMP) / "videos")
os.environ["DATA_DIR"] = str(Path(TMP) / "data")
os.environ["JELLYFIN_URL"] = ""  # no nudges during tests
Path(os.environ["VIDEO_DIR"]).mkdir(parents=True)

# ── fake yt_dlp ─────────────────────────────────────────────────────────────
FAKE_RESULTS = {}  # url/search -> info dict


class FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if url not in FAKE_RESULTS:
            raise RuntimeError("fake: no result for " + url)
        return FAKE_RESULTS[url]


fake = types.ModuleType("yt_dlp")
fake.YoutubeDL = FakeYDL
sys.modules["yt_dlp"] = fake

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.init_db()
client = TestClient(main.app)

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1


# ── helpers ─────────────────────────────────────────────────────────────────
ok(main.clean_dir_name("AC/DC: Live") == "AC-DC- Live", "dir name slashes/colons")
ok(main.clean_dir_name(".hidden. ") == "hidden", "dir name leading dot, trailing junk")
ok(main.clean_file_name('a\\b/c:d*e?f"g<h>i|j') == "a-b-c-d-e-f-g-h-i-j", "file name invalid chars")
ok(main.clean_dir_name("") == "", "empty dir name")

p = main.target_path("Kurzgesagt", "Black Holes", "abcdefghijk")
ok(p == main.VIDEO_DIR / "Kurzgesagt" / "Black Holes.mp4", "target path is clean (no id)")
p2 = main.target_path("", "x" * 400, "abcdefghijk")
ok(p2.parent.name == "YouTube", "empty channel falls back")
ok(len(p2.name) <= 170, "long title trimmed")
p.parent.mkdir(parents=True, exist_ok=True)
p.touch()
p3 = main.target_path("Kurzgesagt", "Black Holes", "zzzzzzzzzzz")
ok(p3.name == "Black Holes [zzzzzzzzzzz].mp4", "title collision appends the id")
p.unlink()

fs = main.format_string()
ok("vcodec^=avc1" in fs and "height<=1080" in fs, "format prefers h264 and caps height")
ok(fs.count("/") == 3, "format has fallback chain")

ok(main.VIDEO_ID_RE.match("dQw4w9WgXcQ"), "video id regex accepts a real id")
ok(not main.VIDEO_ID_RE.match("../etc/pass"), "video id regex rejects traversal")

# existence: legacy [id]-suffix files + queue-db mapping
main.init_db()
d = main.VIDEO_DIR / "Kanal"
d.mkdir(parents=True)
(d / "Bir Video [AAAAAAAAAAA].mp4").touch()
ok(main.find_existing("AAAAAAAAAAA") is not None, "find_existing finds legacy [id] suffix")
ok(main.find_existing("BBBBBBBBBBB") is None, "find_existing misses absent id")
(d / "Temiz Ad.mp4").touch()
with main.db() as con:
    con.execute("INSERT INTO jobs (video_id, status, file, added) VALUES"
                " ('FFFFFFFFFFF', 'done', 'Kanal/Temiz Ad.mp4', '2026-08-17')")
ok(main.find_existing("FFFFFFFFFFF") == d / "Temiz Ad.mp4", "find_existing maps id via queue db")
(d / "Temiz Ad.mp4").unlink()
ok(main.find_existing("FFFFFFFFFFF") is None, "deleted file no longer counts as existing")

# ── API: health, search ─────────────────────────────────────────────────────
r = client.get("/api/health").json()
ok(r["status"] == "ok" and r["videos_writable"], "health ok")

FAKE_RESULTS["ytsearch12:kedi"] = {"entries": [
    {"id": "AAAAAAAAAAA", "title": "Var olan", "channel": "Kanal", "duration": 61},
    {"id": "CCCCCCCCCCC", "title": "Yeni video", "uploader": "Baska", "duration": 125},
    None,  # yt-dlp sometimes yields None entries — must not crash
]}
r = client.get("/api/search", params={"q": "kedi"}).json()["results"]
ok(len(r) == 2, "search drops None entries")
ok(r[0]["exists"] is True and r[1]["exists"] is False, "search marks existing")
ok(r[1]["channel"] == "Baska", "uploader used when channel missing")

# ── API: download by video_id ───────────────────────────────────────────────
r = client.post("/api/download", json={"video_id": "AAAAAAAAAAA"}).json()
ok(r["status"] == "exists", "download of existing id short-circuits")
r = client.post("/api/download", json={"video_id": "CCCCCCCCCCC"}).json()
ok(r["status"] == "queued" and r["job_id"], "new id queued")
job1 = r["job_id"]
r = client.post("/api/download", json={"video_id": "CCCCCCCCCCC"}).json()
ok(r.get("note") == "already queued" and r["job_id"] == job1, "duplicate id not requeued")
ok(client.post("/api/download", json={"video_id": "kisa"}).status_code == 400, "bad id rejected")
ok(client.post("/api/download", json={}).status_code == 400, "empty request rejected")

# ── API: download by URL (single + playlist) ────────────────────────────────
FAKE_RESULTS["https://youtu.be/DDDDDDDDDDD"] = {"id": "DDDDDDDDDDD", "title": "Tekil", "channel": "K2"}
r = client.post("/api/download", json={"url": "https://youtu.be/DDDDDDDDDDD"}).json()
ok(r["kind"] == "video" and r["added"] == 1, "single URL queued")

FAKE_RESULTS["https://www.youtube.com/playlist?list=PL1"] = {
    "title": "Liste", "entries": [
        {"id": "AAAAAAAAAAA", "title": "Var olan"},      # on disk -> skip
        {"id": "DDDDDDDDDDD", "title": "Tekil"},          # queued above -> skip
        {"id": "EEEEEEEEEEE", "title": "Taze", "channel": "K3"},
        {"id": None, "title": "bozuk"},
    ]}
r = client.post("/api/download", json={"url": "https://www.youtube.com/playlist?list=PL1"}).json()
ok(r["kind"] == "playlist" and r["playlist"] == "Liste", "playlist recognized")
ok(r["added"] == 1 and r["skipped"] == 2, "playlist dedupes disk + queue")
ok(client.post("/api/download", json={"url": "ftp://x"}).status_code == 400, "non-http url rejected")

# ── API: queue, job, retry ──────────────────────────────────────────────────
q = client.get("/api/queue").json()
ok(q["pending"] == 3, "pending counts queued jobs")
ok(all(j["status"] == "queued" for j in q["jobs"] if j["video_id"] != "FFFFFFFFFFF"),
   "all enqueued jobs are queued")

with main.db() as con:
    con.execute("UPDATE jobs SET status='error', error='boom' WHERE id=?", (job1,))
r = client.post(f"/api/retry/{job1}").json()
ok(r["status"] == "queued", "retry requeues an errored job")
r = client.post(f"/api/retry/{job1}").json()
ok(r.get("note") == "not in error state", "retry refuses a non-errored job")
ok(client.post("/api/retry/99999").status_code == 404, "retry 404 on unknown job")
ok(client.get("/api/job/99999").status_code == 404, "job 404 on unknown id")
ok(client.get("/api/job/%d" % job1).json()["video_id"] == "CCCCCCCCCCC", "job endpoint returns row")

# ── thumb endpoint guards ───────────────────────────────────────────────────
ok(client.get("/api/thumb", params={"v": "../../etc"}).status_code == 400, "thumb rejects non-id")

# ── worker: download path (fake yt_dlp writes files) ────────────────────────
class DownloadingYDL(FakeYDL):
    def extract_info(self, url, download=False):
        info = super().extract_info(url, download)
        if download:
            tmp = Path(self.opts["outtmpl"]).parent
            (tmp / (info["id"] + ".mp4")).write_bytes(b"fake video")
            (tmp / (info["id"] + ".jpg")).write_bytes(b"fake thumb")
        return info


fake.YoutubeDL = DownloadingYDL
FAKE_RESULTS["https://www.youtube.com/watch?v=EEEEEEEEEEE"] = {
    "id": "EEEEEEEEEEE", "title": "Taze: Bölüm 1/2", "channel": "K3", "duration": 42}
with main.db() as con:
    row = dict(con.execute("SELECT * FROM jobs WHERE video_id='EEEEEEEEEEE'").fetchone())
status, file = main._download_one(row)
ok(status == "done", "worker download completes")
target = main.VIDEO_DIR / "K3" / "Taze- Bölüm 1-2.mp4"
ok(target.exists(), "video landed in <channel>/<title>.mp4")
ok(target.with_name(target.stem + "-thumb.jpg").exists(), "sidecar -thumb.jpg written")
with main.db() as con:
    # what worker() records after a successful download
    con.execute("UPDATE jobs SET status='done', file=? WHERE video_id='EEEEEEEEEEE'", (file,))
    row2 = dict(con.execute("SELECT * FROM jobs WHERE video_id='EEEEEEEEEEE'").fetchone())
ok(row2["duration"] == 42, "row metadata refreshed from download info")
status2, _ = main._download_one(row)
ok(status2 == "exists", "re-download detects the file on disk")

print(f"OK — {PASS} assertions passed")
