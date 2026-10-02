#!/usr/bin/env python3
"""
Web UI for the Viral Shorts Clipper
-----------------------------------
A small local web app around clipper.py: paste a link (or upload a video), pick options,
watch the live log, then preview, copy and download the finished shorts.

Usage:
  python webui.py              # then open http://127.0.0.1:8000
  python webui.py --port 8080

Each run gets its own folder in runs/<id>/ (the same layout clipper.py writes to output/).
Only listens on 127.0.0.1, so it's reachable from this computer only.
"""

import argparse
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory
from werkzeug.utils import secure_filename

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
UPLOADS = HERE / "uploads"
WEB = HERE / "web"
CLIPPER = HERE / "clipper.py"
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN = HERE / "token.json"
CONFIG = HERE / "config.json"

sys.path.insert(0, str(HERE))
import uploader                          # noqa: E402  (YouTube upload + posting slots)
from clipper import write_post_sheet    # noqa: E402

CHOICES = {
    "resolution": {"4k", "1440", "1080"},
    "layout": {"auto", "crop", "blur"},
    "highlight": {"yellow", "green", "cyan", "red", "pink"},
    "whisper_model": {"tiny", "base", "small", "medium", "large-v3"},
    "mode": {"clips", "analyze", "whisper", "full"},
    "cookies_browser": {"", "chrome", "chromium", "firefox", "brave", "edge", "opera", "vivaldi"},
}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 ** 3   # 20 GB uploads

jobs = {}            # id -> job dict (in memory; job.json on disk mirrors it)
jobs_lock = threading.Lock()
api_key = {"value": os.environ.get("ANTHROPIC_API_KEY", "")}   # kept in memory only, never written to disk


# ----------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------
def save_meta(job):
    meta = {k: v for k, v in job.items() if k not in ("log", "proc")}
    (RUNS / job["id"] / "job.json").write_text(json.dumps(meta, indent=2))


def load_jobs():
    """Pick up earlier runs from disk so the history survives a restart."""
    RUNS.mkdir(exist_ok=True)
    for meta_path in RUNS.glob("*/job.json"):
        try:
            job = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") in ("queued", "running"):
            job["status"] = "interrupted"
        log_path = meta_path.parent / "log.txt"
        job["log"] = log_path.read_text(errors="replace").splitlines() if log_path.exists() else []
        jobs[job["id"]] = job
    # Runs made with clipper.py directly (no job.json) still show up, so they can be posted too
    for clips_path in RUNS.glob("*/work/clips.json"):
        folder = clips_path.parent.parent
        if folder.name in jobs:
            continue
        info = folder / "work" / "info.json"
        title = json.loads(info.read_text()).get("title") if info.exists() else folder.name
        jobs[folder.name] = {"id": folder.name, "source": "", "source_name": title, "video_title": title,
                             "status": "done", "stage": "Done", "progress": 100,
                             "created": clips_path.stat().st_mtime, "log": [],
                             "options": {"mode": "clips", "clips": "auto", "resolution": "", "min_len": "",
                                         "max_len": ""}}


def build_command(job):
    o = job["options"]
    cmd = [sys.executable, "-u", str(CLIPPER), job["source"], "--out", str(RUNS / job["id"]),
           "--clips", o["clips"], "--resolution", o["resolution"],
           "--min", str(o["min_len"]), "--max", str(o["max_len"]),
           "--language", o["language"], "--layout", o["layout"], "--highlight", o["highlight"],
           "--whisper-model", o["whisper_model"], "--model", o["model"]]
    if o["font"]:
        cmd += ["--font", o["font"]]
    if o["no_upper"]:
        cmd.append("--no-upper")
    if o["cookies_browser"]:
        cmd += ["--cookies-from-browser", o["cookies_browser"]]
    cmd += {"analyze": ["--analyze-only"], "whisper": ["--whisper"],
            "full": ["--full-download"]}.get(o["mode"], [])
    return cmd


def update_progress(job, line):
    """Turn clipper.py's log lines into a stage + percentage for the progress bar."""
    if m := re.search(r"\[clipper\] Video: \"(.*)\" \(([\d.]+) min\)", line):
        job["video_title"], job["video_minutes"] = m.group(1), float(m.group(2))
        job["stage"], job["progress"] = "Reading transcript", 10
    elif "transcribed up to" in line or "Whisper" in line:
        job["stage"], job["progress"] = "Transcribing audio", max(job["progress"], 12)
    elif "Making" in line and "shorts from this video" in line:
        job["stage"], job["progress"] = "Picking viral moments", 20
    elif "Asking Claude" in line:
        job["stage"], job["progress"] = "Claude is picking viral moments", 25
    elif "Asking the local AI" in line:
        job["stage"], job["progress"] = "Local AI is picking viral moments (can take several minutes)", 25
    elif m := re.search(r"Rendering (\d+) shorts", line):
        job["total"] = int(m.group(1))
        job["stage"], job["progress"] = "Rendering", 35
    elif m := re.search(r"\[(\d+)/(\d+)\] (.*?)  \(", line):
        i, n = int(m.group(1)), int(m.group(2))
        job["stage"] = f"Short {i} of {n}: {m.group(3)}"
        job["progress"] = 35 + int(60 * (i - 1) / n)
    elif "saved " in line and job.get("total"):
        job["done_clips"] = job.get("done_clips", 0) + 1
        job["progress"] = 35 + int(60 * job["done_clips"] / job["total"])


def friendly_error(log_lines):
    """One readable sentence for the UI instead of a traceback."""
    last = next((l.strip() for l in reversed(log_lines) if l.strip()), "")
    text = " ".join(log_lines[-40:])
    if "AuthenticationError" in text or "invalid x-api-key" in text or "API key is invalid" in text:
        return "Your Claude API key was rejected. Check it in Settings (top right)."
    if "credit balance" in text.lower():
        return "Your Anthropic account is out of credits. Add credits at console.anthropic.com."
    if "Sign in to confirm" in text or "confirm you're not a bot" in text:
        return ("YouTube is asking to confirm you're not a bot. Wait a while and retry, or pick your browser "
                "under More options → YouTube login (you must be signed in to YouTube there).")
    if "Video unavailable" in text or "Private video" in text:
        return "That YouTube video is unavailable or private."
    return last[:300] or "clipper.py exited with an error."


def run_job(job):
    out = RUNS / job["id"]
    env = {**os.environ, "ANTHROPIC_API_KEY": api_key["value"], "PYTHONUNBUFFERED": "1"}
    with open(out / "log.txt", "a", encoding="utf-8") as logf:
        try:
            proc = subprocess.Popen(build_command(job), cwd=HERE, env=env, text=True, bufsize=1,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding="utf-8", errors="replace", start_new_session=True)
        except OSError as e:
            job["log"].append(f"Could not start clipper.py: {e}")
            job["status"] = "failed"
            save_meta(job)
            return
        job["proc"], job["status"], job["stage"] = proc, "running", "Fetching video info"
        save_meta(job)
        # Whisper prints progress with \r, so split on both kinds of line ending
        buf = ""
        while chunk := proc.stdout.read(1):
            if chunk in "\r\n":
                if buf.strip():
                    job["log"].append(buf)
                    logf.write(buf + "\n"); logf.flush()
                    update_progress(job, buf)
                buf = ""
            else:
                buf += chunk
        if buf.strip():
            job["log"].append(buf); logf.write(buf + "\n")
        code = proc.wait()

    job.pop("proc", None)
    if job["status"] == "stopping":
        job["status"], job["stage"] = "stopped", "Stopped"
    elif code == 0:
        job["status"], job["stage"], job["progress"] = "done", "Done", 100
    else:
        job["status"], job["stage"] = "failed", "Failed"
        job["error"] = friendly_error(job["log"])
    job["finished"] = time.time()
    save_meta(job)


def job_results(job):
    out = RUNS / job["id"]
    for path in (out / "work" / "clips.json", out / "clips.json"):
        if path.exists():
            try:
                clips = json.loads(path.read_text())
            except json.JSONDecodeError:
                return []
            state = uploader.load_state()
            credit = post_settings()["credit_original"]
            for c in clips:
                if c.get("file") and not (out / c["file"]).exists():
                    c["file"] = None
                if c.get("file"):
                    key = str((out / c["file"]).resolve())
                    c["upload"] = uploads.get(key) or state["uploaded"].get(key)
                if c.get("title"):
                    c["post"] = uploader.build_metadata(c, add_credit=credit)
            return clips
    return []


def public(job, since=0):
    return {**{k: v for k, v in job.items() if k not in ("log", "proc")},
            "log": job["log"][since:], "log_len": len(job["log"])}


# ----------------------------------------------------------------------------
# YouTube channel, editing, uploads & scheduling
# ----------------------------------------------------------------------------
POST_DEFAULTS = {"timezone": "Asia/Kolkata", "post_times": ["12:00", "17:00", "20:00"], "credit_original": True}
oauth_flows = {}      # OAuth state -> Flow, while the user is on Google's sign-in page
channel_cache = {}    # channel id -> channel details, so every page load doesn't cost API quota
uploads = {}          # mp4 path -> live status of an upload queued or running in this server
upload_queue = queue.Queue()
state_lock = threading.Lock()


def read_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    except json.JSONDecodeError:
        return {}


def post_settings():
    cfg = read_config()
    return {k: cfg.get(k, v) for k, v in POST_DEFAULTS.items()}


def save_post_settings(new):
    cfg = read_config()           # keep the autopilot's own keys (channels etc.)
    cfg.update(new)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def secret_type():
    try:
        data = json.loads(CLIENT_SECRET.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return next((k for k in ("installed", "web") if data.get(k, {}).get("client_id")), None)


def migrate_legacy_token():
    """A token.json from before multi-account support becomes accounts/<channel id>.json."""
    if not TOKEN.exists():
        return
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(str(TOKEN), uploader.SCOPES)
        items = build("youtube", "v3", credentials=creds, cache_discovery=False) \
            .channels().list(part="id", mine=True).execute().get("items") or []
        if items:
            uploader.save_account(items[0]["id"], creds.to_json())
            if not uploader.active_channel():
                uploader.set_active(items[0]["id"])
            TOKEN.unlink()
    except Exception as e:
        print(f"Couldn't move token.json into accounts/: {e}")


def load_creds(channel_id=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    channel_id = channel_id or uploader.active_channel()
    if not channel_id:
        return None
    token = uploader.token_path(channel_id)
    if not token.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token), uploader.SCOPES)
    except (ValueError, json.JSONDecodeError):
        return None
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            return None
        try:
            creds.refresh(Request())
        except Exception:
            return None
        uploader.save_account(channel_id, creds.to_json())
    return creds


def youtube_service(channel_id=None, creds=None):
    from googleapiclient.discovery import build
    creds = creds or load_creds(channel_id)
    if not creds:
        raise RuntimeError("Connect your YouTube channel first (top right).")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_info(channel_id, refresh=False):
    if channel_id in channel_cache and not refresh:
        return channel_cache[channel_id]
    items = youtube_service(channel_id).channels().list(part="snippet,statistics", mine=True).execute().get("items") or []
    if not items:
        info = {"error": "This Google account doesn't have a YouTube channel yet. Create one on YouTube first."}
    else:
        ch = items[0]
        stats = ch.get("statistics", {})
        info = {"id": ch["id"], "title": ch["snippet"]["title"],
                "handle": ch["snippet"].get("customUrl", ""),
                "thumbnail": ch["snippet"].get("thumbnails", {}).get("default", {}).get("url", ""),
                "subscribers": None if stats.get("hiddenSubscriberCount") else int(stats.get("subscriberCount", 0)),
                "videos": int(stats.get("videoCount", 0))}
    channel_cache[channel_id] = info
    return info


def clip_at(job_id, index):
    """(clips list, clips.json path, clip) for one rendered short of a run."""
    if job_id not in jobs:
        abort(404)
    path = RUNS / job_id / "work" / "clips.json"
    if not path.exists():
        abort(404)
    clips = json.loads(path.read_text(encoding="utf-8"))
    if not 0 <= index < len(clips):
        abort(404)
    return clips, path, clips[index]


def api_error(e):
    """Readable message for a Google API error."""
    try:
        err = json.loads(e.content)["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        if reason in ("quotaExceeded", "uploadLimitExceeded"):
            return "YouTube's daily upload limit is used up (about 6 uploads a day). Try again tomorrow."
        return err.get("message") or str(e)
    except Exception:
        return str(e)


def upload_worker():
    """Uploads one at a time: YouTube's quota is small and posting slots must not collide."""
    from googleapiclient.errors import HttpError
    while True:
        key, mp4, clip, when, at, channel_id = upload_queue.get()
        u = uploads[key]
        try:
            yt = youtube_service(channel_id)
            cfg = post_settings()
            publish_at = slot = None
            with state_lock:
                state = uploader.load_state()
                if key in state["uploaded"]:
                    raise RuntimeError("Already uploaded.")
                if when == "slot":
                    taken = uploader.slots_for(state, channel_id)
                    slot = uploader.next_slot(cfg["post_times"], cfg["timezone"], taken)
                    publish_at = uploader.to_rfc3339_utc(slot)
                    taken.append(slot.isoformat())   # reserve it now
                    uploader.save_state(state)
                elif when == "at":
                    publish_at = at
            u.update(status="uploading", publish_at=publish_at, progress=0)
            meta = uploader.build_metadata(clip, add_credit=cfg["credit_original"])
            resp = uploader.upload_video(yt, mp4, meta, publish_at,
                                         privacy="public" if when == "now" else "private",
                                         on_progress=lambda p: u.update(progress=round(p * 100)))
            record = {"video_id": resp["id"], "publish_at": publish_at, "mode": when, "channel_id": channel_id,
                      "channel_title": u.get("channel_title", ""),
                      "title": meta["title"], "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            with state_lock:
                state = uploader.load_state()
                state["uploaded"][key] = record
                uploader.save_state(state)
            u.clear()
            u.update(status="done", progress=100, **record)
        except uploader.QuotaExhausted:
            u.update(status="failed", error="YouTube's daily upload limit is used up (about 6 uploads a day). "
                                             "Try again tomorrow.")
        except HttpError as e:
            u.update(status="failed", error=api_error(e))
        except Exception as e:
            u.update(status="failed", error=str(e) or e.__class__.__name__)
        finally:
            if u.get("status") == "failed" and when == "slot" and u.get("publish_at"):
                with state_lock:          # give the reserved slot back
                    state = uploader.load_state()
                    taken = uploader.slots_for(state, channel_id)
                    taken[:] = [s for s in taken
                                if uploader.to_rfc3339_utc(uploader.datetime.fromisoformat(s)) != u["publish_at"]]
                    uploader.save_state(state)
            upload_queue.task_done()


def queue_upload(job_id, index, when, at=None):
    clips, _, clip = clip_at(job_id, index)
    if not clip.get("file") or not (RUNS / job_id / clip["file"]).exists():
        raise ValueError("This short hasn't been rendered, so there's nothing to upload.")
    mp4 = RUNS / job_id / clip["file"]
    key = str(mp4.resolve())
    if key in uploader.load_state()["uploaded"]:
        raise ValueError("This short is already on YouTube.")
    if uploads.get(key, {}).get("status") in ("queued", "uploading"):
        raise ValueError("This short is already being uploaded.")
    channel_id = uploader.active_channel()
    title = (channel_cache.get(channel_id) or {}).get("title", "")
    uploads[key] = {"status": "queued", "progress": 0, "mode": when, "publish_at": at, "title": clip["title"],
                    "channel_id": channel_id, "channel_title": title}
    upload_queue.put((key, mp4, clip, when, at, channel_id))
    return uploads[key]


def parse_publish_at(value):
    """ISO time from the browser -> RFC 3339 UTC, checked to be at least 15 minutes ahead."""
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Pick a valid date and time.")
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo(post_settings()["timezone"]))
    if dt < datetime.now(timezone.utc) + timedelta(minutes=15):
        raise ValueError("Pick a time at least 15 minutes from now.")
    return uploader.to_rfc3339_utc(dt)


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/api/settings")
def get_settings():
    return jsonify({"has_key": bool(api_key["value"]), "key_hint": api_key["value"][-4:] if api_key["value"] else ""})


@app.post("/api/settings")
def set_settings():
    key = (request.json or {}).get("api_key", "").strip()
    if key and not key.startswith("sk-ant-"):
        return jsonify({"error": "That doesn't look like a Claude API key (it should start with sk-ant-)."}), 400
    api_key["value"] = key
    return get_settings()


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file received."}), 400
    UPLOADS.mkdir(exist_ok=True)
    name = f"{secrets.token_hex(3)}_{secure_filename(f.filename) or 'video.mp4'}"
    f.save(UPLOADS / name)
    return jsonify({"path": str(UPLOADS / name), "name": f.filename})


@app.post("/api/jobs")
def create_job():
    data = request.json or {}
    source = (data.get("source") or "").strip()
    if not source:
        return jsonify({"error": "Paste a YouTube link or upload a video first."}), 400
    is_url = re.match(r"https?://", source)
    if not is_url and not Path(source).is_file():
        return jsonify({"error": "That's not a link, and no file exists at that path."}), 400

    o = data.get("options") or {}
    try:
        options = {
            "mode": o.get("mode", "clips"),
            "clips": str(o.get("clips") or "auto").strip().lower(),
            "resolution": o.get("resolution", "1080"),
            "min_len": int(o.get("min_len", 20)),
            "max_len": int(o.get("max_len", 59)),
            "language": (o.get("language") or "auto").strip() or "auto",
            "layout": o.get("layout", "auto"),
            "highlight": o.get("highlight", "yellow"),
            "whisper_model": o.get("whisper_model", "small"),
            "model": (o.get("model") or "ollama:qwen3:4b").strip(),
            "font": (o.get("font") or "").strip(),
            "no_upper": bool(o.get("no_upper")),
            "cookies_browser": o.get("cookies_browser") or "",
        }
    except (TypeError, ValueError):
        return jsonify({"error": "Clip lengths must be whole numbers."}), 400
    for field, allowed in CHOICES.items():
        if options[field] not in allowed:
            return jsonify({"error": f"Invalid {field}: {options[field]}"}), 400
    if options["clips"] != "auto" and not options["clips"].isdigit():
        return jsonify({"error": "Number of shorts must be a number or 'auto'."}), 400
    if not 5 <= options["min_len"] < options["max_len"] <= 180:
        return jsonify({"error": "Clip length: min must be at least 5s and below max (max 180s)."}), 400
    if not re.fullmatch(r"[\w.:\-]+", options["model"]) or not re.fullmatch(r"[\w\-]+", options["language"]):
        return jsonify({"error": "Invalid model or language."}), 400
    if not options["model"].startswith("ollama:") and not api_key["value"]:
        return jsonify({"error": "Add your Claude API key (top right), or choose the free local AI "
                                 "under More options → AI model."}), 400

    job_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    (RUNS / job_id).mkdir(parents=True)
    job = {"id": job_id, "source": source, "source_name": data.get("source_name") or source,
           "options": options, "status": "queued", "stage": "Starting", "progress": 2,
           "created": time.time(), "log": []}
    with jobs_lock:
        jobs[job_id] = job
    save_meta(job)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(public(job))


@app.get("/api/jobs")
def list_jobs():
    items = sorted(jobs.values(), key=lambda j: -j["created"])
    return jsonify([{k: j.get(k) for k in ("id", "source_name", "video_title", "status", "created", "options")}
                    for j in items])


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.get(job_id) or abort(404)
    since = request.args.get("since", 0, type=int)
    res = public(job, since)
    if job["status"] in ("done", "failed", "stopped", "interrupted"):
        res["results"] = job_results(job)
    return jsonify(res)


@app.post("/api/jobs/<job_id>/stop")
def stop_job(job_id):
    job = jobs.get(job_id) or abort(404)
    proc = job.get("proc")
    if proc and proc.poll() is None:
        job["status"] = "stopping"
        try:
            os.killpg(proc.pid, signal.SIGTERM)   # also stops ffmpeg / yt-dlp children
        except (ProcessLookupError, AttributeError):
            proc.terminate()
    return jsonify({"ok": True})


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    job = jobs.get(job_id) or abort(404)
    if job.get("proc"):
        return jsonify({"error": "Stop the run before deleting it."}), 400
    import shutil
    shutil.rmtree(RUNS / job_id, ignore_errors=True)
    jobs.pop(job_id, None)
    return jsonify({"ok": True})


@app.get("/files/<job_id>/<path:name>")
def job_file(job_id, name):
    if job_id not in jobs:
        abort(404)
    return send_from_directory(RUNS / job_id, name, as_attachment=request.args.get("dl") == "1")


@app.post("/api/jobs/<job_id>/open")
def open_folder(job_id):
    if job_id not in jobs:
        abort(404)
    opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    subprocess.Popen([opener, str(RUNS / job_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True})


# ---- YouTube channel ---------------------------------------------------------
@app.get("/api/youtube")
def youtube_status():
    active = uploader.active_channel()
    res = {"has_secret": CLIENT_SECRET.exists(), "secret_type": secret_type(), "connected": False,
           "redirect_uri": request.host_url.rstrip("/") + "/oauth2callback", "settings": post_settings(),
           "active": active, "accounts": []}
    for cid in uploader.account_ids():
        if load_creds(cid):
            try:
                info = channel_info(cid, refresh=request.args.get("refresh") == "1")
            except Exception as e:
                info = {"error": f"Couldn't read this channel: {e}"}
        else:
            info = {"error": "This sign-in expired. Sign in to this account again."}
        res["accounts"].append({**info, "id": cid})
        if cid == active:
            res["channel"] = res["accounts"][-1]
            res["connected"] = not info.get("error")
    return jsonify(res)


@app.post("/api/youtube/secret")
def youtube_secret():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file received."}), 400
    try:
        data = json.loads(f.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "That isn't a JSON file. Download the OAuth client JSON from Google Cloud."}), 400
    if not any(data.get(k, {}).get("client_id") for k in ("installed", "web")):
        return jsonify({"error": "That JSON isn't an OAuth client file. In Google Cloud choose "
                                 "Credentials → Create credentials → OAuth client ID → Desktop app."}), 400
    CLIENT_SECRET.write_text(json.dumps(data))
    os.chmod(CLIENT_SECRET, 0o600)
    return youtube_status()


@app.get("/api/youtube/connect")
def youtube_connect():
    from google_auth_oauthlib.flow import Flow
    if not secret_type():
        return redirect("/?yt_error=" + "Add your client_secret.json first.")
    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET), scopes=uploader.SCOPES,
                                         redirect_uri=request.host_url.rstrip("/") + "/oauth2callback",
                                         autogenerate_code_verifier=True)
    # select_account: always show Google's account chooser, so another account can be added
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account")
    oauth_flows[state] = flow
    return redirect(url)


@app.get("/oauth2callback")
def oauth_callback():
    from urllib.parse import quote
    if request.args.get("error"):
        return redirect("/?yt_error=" + quote("Google sign-in was cancelled (" + request.args["error"] + ")."))
    flow = oauth_flows.pop(request.args.get("state", ""), None)
    if not flow:
        return redirect("/?yt_error=" + quote("That sign-in link expired. Click Connect again."))
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    try:
        # oauthlib insists on https; this callback only ever arrives on 127.0.0.1
        flow.fetch_token(authorization_response=request.url.replace("http://", "https://", 1))
    except Exception as e:
        return redirect("/?yt_error=" + quote(f"Google sign-in failed: {e}"))
    creds = flow.credentials
    try:
        items = youtube_service(creds=creds).channels().list(part="id", mine=True).execute().get("items") or []
    except Exception as e:
        return redirect("/?yt_error=" + quote(f"Signed in, but couldn't read the YouTube channel: {e}"))
    if not items:
        return redirect("/?yt_error=" + quote("That Google account doesn't have a YouTube channel yet. "
                                              "Create one on YouTube first, then sign in again."))
    cid = items[0]["id"]
    uploader.save_account(cid, creds.to_json())
    uploader.set_active(cid)
    channel_cache.pop(cid, None)
    return redirect("/?yt_connected=1")


@app.post("/api/youtube/active")
def youtube_active():
    cid = (request.json or {}).get("id", "")
    if cid not in uploader.account_ids():
        return jsonify({"error": "That account isn't signed in."}), 400
    uploader.set_active(cid)
    return youtube_status()


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    cid = (request.json or {}).get("id") or uploader.active_channel()
    if cid:
        uploader.token_path(cid).unlink(missing_ok=True)
        channel_cache.pop(cid, None)
    uploader.set_active(uploader.active_channel() or "")   # falls back to another signed-in account
    return youtube_status()


@app.post("/api/youtube/settings")
def youtube_settings():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    d = request.json or {}
    times = sorted({t.strip() for t in d.get("post_times", []) if t.strip()})
    if not times or not all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in times):
        return jsonify({"error": "Add at least one posting time, like 12:00."}), 400
    tz = (d.get("timezone") or "").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"error": f"Unknown time zone: {tz}"}), 400
    save_post_settings({"post_times": times, "timezone": tz, "credit_original": bool(d.get("credit_original"))})
    return jsonify(post_settings())


# ---- edit a short's title, description & hashtags ------------------------------
@app.put("/api/jobs/<job_id>/clips/<int:index>")
def edit_clip(job_id, index):
    clips, path, clip = clip_at(job_id, index)
    d = request.json or {}
    title = str(d.get("title", clip["title"])).strip()
    if not title:
        return jsonify({"error": "The title can't be empty."}), 400
    if len(title) > 100:
        return jsonify({"error": "YouTube titles can be at most 100 characters."}), 400
    tags = d.get("hashtags", clip.get("hashtags", []))
    if isinstance(tags, str):
        tags = tags.replace(",", " ").split()
    tags = list(dict.fromkeys("#" + re.sub(r"[^\w]", "", t.lstrip("#")) for t in tags if t.strip("# ")))
    clip.update(title=title, description=str(d.get("description", clip.get("description", ""))).strip(),
                hashtags=tags)
    path.write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")
    if (RUNS / job_id / "POST_THESE.txt").exists():
        write_post_sheet([c for c in clips if c.get("file")], RUNS / job_id / "POST_THESE.txt")
    return jsonify(job_results(jobs[job_id])[index])


# ---- upload / schedule ----------------------------------------------------------
@app.post("/api/jobs/<job_id>/clips/<int:index>/publish")
def publish_clip(job_id, index):
    d = request.json or {}
    when = d.get("when")
    if when not in ("now", "slot", "at", "draft"):
        return jsonify({"error": "Choose when to publish."}), 400
    if not load_creds():
        return jsonify({"error": "Connect your YouTube channel first (top right)."}), 400
    try:
        at = parse_publish_at(d.get("at")) if when == "at" else None
        return jsonify(queue_upload(job_id, index, when, at))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/jobs/<job_id>/publish_all")
def publish_all(job_id):
    when = (request.json or {}).get("when", "slot")
    if when not in ("slot", "draft"):
        return jsonify({"error": "Choose next free slots or private drafts."}), 400
    if not load_creds():
        return jsonify({"error": "Connect your YouTube channel first (top right)."}), 400
    queued = 0
    for i, c in enumerate(job_results(jobs.get(job_id) or abort(404))):
        if c.get("file") and not c.get("upload"):
            try:
                queue_upload(job_id, i, when)
                queued += 1
            except ValueError:
                pass
    return jsonify({"queued": queued})


@app.get("/api/scheduled")
def scheduled():
    """Everything uploaded or uploading, soonest publish time first."""
    state = uploader.load_state()
    rows = {}
    for key, rec in state["uploaded"].items():
        rows[key] = {**rec, "status": "done"}
    for key, u in uploads.items():
        if u.get("status") != "done":
            rows[key] = u
    out = []
    for key, r in rows.items():
        p = Path(key)
        job_id = p.parent.name if p.parent.parent == RUNS.resolve() else None
        out.append({**r, "file": p.name, "job_id": job_id,
                    "title": r.get("title") or re.sub(r"^\d+ - |\.mp4$", "", p.name)})
    if request.args.get("live") == "1" and uploader.account_ids():
        try:
            live, active = {}, uploader.active_channel()
            for cid in uploader.account_ids():
                if not load_creds(cid):
                    continue
                # private/scheduled videos are only visible to the channel that owns them
                ids = [r["video_id"] for r in out if r.get("video_id") and (r.get("channel_id") or active) == cid]
                yt = youtube_service(cid)
                for i in range(0, len(ids), 50):
                    for v in yt.videos().list(part="status", id=",".join(ids[i:i + 50])).execute().get("items", []):
                        live[v["id"]] = v["status"]
            for r in out:
                st = live.get(r.get("video_id"))
                r["live"] = {"privacy": st.get("privacyStatus"), "publish_at": st.get("publishAt"),
                             "upload_status": st.get("uploadStatus")} if st else {"missing": bool(r.get("video_id"))}
        except Exception as e:
            return jsonify({"items": out, "error": f"Couldn't check YouTube: {e}"})
    out.sort(key=lambda r: (r.get("publish_at") or "9999", r.get("uploaded_at") or ""))
    return jsonify({"items": out})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Web UI for the Viral Shorts Clipper")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    load_jobs()
    migrate_legacy_token()
    threading.Thread(target=upload_worker, daemon=True).start()
    print(f"Viral Clipper UI running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, threaded=True)
