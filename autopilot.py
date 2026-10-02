#!/usr/bin/env python3
"""
Autopilot — the "posts while you sleep" part
--------------------------------------------
Every run it:
  1. checks the channels in config.json for new long videos
  2. runs the clipper on each new one (only the chosen seconds are downloaded)
  3. uploads the finished shorts to your channel, each into the next posting slot

  python autopilot.py            # run once (use Task Scheduler / cron to run it every few hours)
  python autopilot.py --loop 3   # keep running, check every 3 hours (leave your PC on)
  python autopilot.py --no-upload   # clip only, don't upload
"""

import argparse
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import uploader

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen_videos.json"
LOG_FILE = HERE / "autopilot.log"

DEFAULTS = {
    "channels": [],
    "videos_to_check_per_channel": 5,
    "first_run_take_latest": 1,
    "min_video_minutes": 4,
    "clips_per_video": "auto",
    "resolution": "4k",
    "language": "en",
    "upload": True,
    "schedule": True,
    "timezone": "Asia/Kolkata",
    "post_times": ["12:00", "17:00", "20:00"],
    "credit_original": True,
    "library_dir": "library",
    "max_attempts_per_video": 2,
    "model": "ollama:qwen3:4b",   # free local AI via Ollama; or a Claude model like "claude-sonnet-5-5"
}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M} [autopilot] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    p = HERE / "config.json"
    if not p.exists():
        sys.exit("config.json not found. Copy config.example.json to config.json and add your channels.")
    return {**DEFAULTS, **json.loads(p.read_text(encoding="utf-8"))}


def load_seen():
    return json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def channel_videos_url(channel: str) -> str:
    """Accept '@MrBeast', a channel URL, or a /videos URL; return the channel's long-videos tab."""
    c = channel.strip().rstrip("/")
    if c.startswith("@"):
        c = f"https://www.youtube.com/{c}"
    elif not c.startswith("http"):
        c = f"https://www.youtube.com/@{c}"
    for tab in ("/videos", "/shorts", "/streams", "/featured"):
        if c.endswith(tab):
            c = c[: -len(tab)]
    return c + "/videos"


def latest_videos(channel, n):
    """Newest-first list of {id, title, duration} from a channel's Videos tab (no download)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": n}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(channel_videos_url(channel), download=False)
    out = []
    for e in (info.get("entries") or [])[:n]:
        if e and e.get("id"):
            out.append({"id": e["id"], "title": e.get("title") or "", "duration": e.get("duration"),
                        "channel": info.get("channel") or info.get("uploader") or channel})
    return out


def find_new_videos(cfg, seen):
    new = []
    for ch in cfg["channels"]:
        try:
            vids = latest_videos(ch, cfg["videos_to_check_per_channel"])
        except Exception as e:
            log(f"Could not read channel {ch}: {e}")
            continue
        first_time = not any(v["id"] in seen for v in vids) and not seen.get(f"_channel:{ch}")
        seen[f"_channel:{ch}"] = True
        for i, v in enumerate(vids):
            rec = seen.get(v["id"])
            if rec and not (rec.get("status") == "failed"
                            and rec.get("attempts", 0) < cfg["max_attempts_per_video"]):
                continue                                   # done, skipped, or out of retries
            if first_time and i >= cfg["first_run_take_latest"]:
                seen[v["id"]] = {"status": "skipped_backlog", "title": v["title"]}   # don't clip old backlog
                continue
            if v["duration"] and v["duration"] < cfg["min_video_minutes"] * 60:
                seen[v["id"]] = {"status": "skipped_short", "title": v["title"]}
                continue
            new.append(v)
    return new


def clip_video(video, cfg):
    url = f"https://www.youtube.com/watch?v={video['id']}"
    out = HERE / cfg["library_dir"] / video["id"]
    cmd = [sys.executable, str(HERE / "clipper.py"), url, "--out", str(out),
           "--clips", str(cfg["clips_per_video"]), "--resolution", cfg["resolution"],
           "--language", cfg["language"]]
    for k in ("layout", "highlight", "font", "model"):
        if cfg.get(k):
            cmd += [f"--{k}", str(cfg[k])]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"clipper exited with code {r.returncode}")
    return out


def run_once(cfg, do_upload=True):
    seen = load_seen()
    new = find_new_videos(cfg, seen)
    save_seen(seen)
    log(f"Found {len(new)} new video(s) to clip")

    for v in new:
        rec = seen.get(v["id"], {})
        attempts = rec.get("attempts", 0)
        if attempts >= cfg["max_attempts_per_video"]:
            continue
        log(f"Clipping: {v['title']}  ({v['channel']})")
        try:
            clip_video(v, cfg)
            seen[v["id"]] = {"status": "clipped", "title": v["title"], "at": datetime.now().isoformat(timespec="seconds")}
        except Exception as e:
            seen[v["id"]] = {"status": "failed", "title": v["title"], "attempts": attempts + 1, "error": str(e)}
            log(f"  failed: {e} (will retry next run)" if attempts + 1 < cfg["max_attempts_per_video"] else f"  failed: {e}")
        save_seen(seen)

    if do_upload and cfg["upload"]:
        library = HERE / cfg["library_dir"]
        if library.exists():
            try:
                n = uploader.upload_pending(library, cfg)
                log(f"Uploaded {n} short(s)")
            except SystemExit as e:
                log(str(e))
            except Exception:
                log("Upload error:\n" + traceback.format_exc())


def main():
    ap = argparse.ArgumentParser(description="Watch channels, clip new videos, post shorts on a schedule.")
    ap.add_argument("--loop", type=float, metavar="HOURS", help="keep running, checking every N hours")
    ap.add_argument("--no-upload", action="store_true", help="clip only; don't upload")
    args = ap.parse_args()
    cfg = load_config()
    if not cfg["channels"]:
        sys.exit("Add at least one channel to config.json")
    while True:
        try:
            run_once(cfg, do_upload=not args.no_upload)
        except Exception:
            log("Run failed:\n" + traceback.format_exc())
        if not args.loop:
            break
        log(f"Sleeping {args.loop} h ...")
        time.sleep(args.loop * 3600)


if __name__ == "__main__":
    main()
