#!/usr/bin/env python3
"""
YouTube uploader for the Viral Shorts Clipper
---------------------------------------------
Uploads finished shorts to your channel with title, description, hashtags and a credit line,
and schedules each one into the next free posting slot (e.g. 12:00, 17:00, 20:00 IST).

  python uploader.py library/            # upload everything not uploaded yet, scheduled
  python uploader.py output/ --no-schedule   # upload as private drafts, you publish them yourself

First run opens your browser once to sign in to YouTube. See README → "Auto-posting setup".
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
STATE_FILE = HERE / "upload_state.json"
ACCOUNTS = HERE / "accounts"          # one sign-in per YouTube channel: accounts/<channel id>.json
ACTIVE_FILE = ACCOUNTS / "active"      # channel id that uploads go to by default
LEGACY_TOKEN = HERE / "token.json"


def log(msg):
    print(f"[uploader] {msg}", flush=True)


class QuotaExhausted(Exception):
    pass


# ----------------------------------------------------------------------------
# State: what's been uploaded + which posting slots are taken
# ----------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"uploaded": {}, "slots": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def slots_for(state, channel_id):
    """Taken posting slots of one channel (each channel has its own schedule)."""
    return state.setdefault("channel_slots", {}).setdefault(channel_id or "", [])


# ----------------------------------------------------------------------------
# Signed-in accounts (one token file per channel)
# ----------------------------------------------------------------------------
def account_ids():
    return sorted(p.stem for p in ACCOUNTS.glob("*.json")) if ACCOUNTS.exists() else []


def token_path(channel_id):
    return ACCOUNTS / f"{channel_id}.json"


def active_channel():
    ids = account_ids()
    try:
        cid = ACTIVE_FILE.read_text().strip()
    except OSError:
        cid = ""
    return cid if cid in ids else (ids[0] if ids else None)


def set_active(channel_id):
    ACCOUNTS.mkdir(exist_ok=True)
    ACTIVE_FILE.write_text(channel_id or "")


def save_account(channel_id, creds_json):
    ACCOUNTS.mkdir(exist_ok=True)
    os.chmod(ACCOUNTS, 0o700)
    p = token_path(channel_id)
    p.write_text(creds_json)
    os.chmod(p, 0o600)


def next_slot(post_times, tz_name, taken, now=None, min_lead_minutes=30):
    """First free posting time (as an aware datetime) at one of `post_times` ("HH:MM") in `tz_name`."""
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    earliest = now + timedelta(minutes=min_lead_minutes)
    taken = set(taken)
    times = sorted(tuple(map(int, t.split(":"))) for t in post_times)
    day = now.date()
    for _ in range(3650):
        for h, m in times:
            slot = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if slot >= earliest and slot.isoformat() not in taken:
                return slot
        day += timedelta(days=1)
    raise RuntimeError("No free slot found")


def to_rfc3339_utc(dt):
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ----------------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------------
def clean(text):
    # YouTube rejects < and > in titles/descriptions
    return (text or "").replace("<", "").replace(">", "").strip()


def build_metadata(clip, add_credit=True):
    # AI descriptions often end in their own hashtags; move those into the hashtag line so they
    # aren't posted twice
    desc = clip.get("description", "")
    trailing = re.search(r"(\s*#[^\s#]+)+\s*$", desc)
    extra = re.findall(r"#([^\s#]+)", trailing.group(0)) if trailing else []
    desc = desc[:trailing.start()] if trailing else desc
    tags_raw = [t.lstrip("#").strip() for t in list(clip.get("hashtags", [])) + extra if t.strip()]
    firsts = {}
    for t in tags_raw:                      # dedupe case-insensitively, keep first spelling and order
        firsts.setdefault(t.lower(), t)
    tags_raw = list(firsts.values())
    hashtags = ["#" + re.sub(r"\s+", "", t) for t in tags_raw]
    if "#shorts" not in [h.lower() for h in hashtags]:
        hashtags.insert(0, "#shorts")

    title = clean(clip["title"])[:100]
    parts = [clean(desc)]
    if add_credit and clip.get("source_url"):
        who = clip.get("source_channel") or "the original creator"
        parts.append(f"Credit: {who}\nFull video: {clip['source_url']}")
    parts.append(" ".join(hashtags[:15]))
    description = "\n\n".join(p for p in parts if p)[:4900]

    tags, total = [], 0
    for t in ["shorts"] + tags_raw:                 # YouTube caps tags at ~500 chars total
        if t.lower() not in [x.lower() for x in tags] and total + len(t) + 1 <= 450:
            tags.append(t); total += len(t) + 1
    return {"title": title, "description": description, "tags": tags}


def pending_clips(folders, state):
    """Yield (mp4_path, clip_meta) for every rendered short not uploaded yet, oldest folder first."""
    for folder in folders:
        meta_file = folder / "work" / "clips.json"
        if not meta_file.exists():
            continue
        for clip in json.loads(meta_file.read_text(encoding="utf-8")):
            mp4 = folder / clip["file"]
            key = str(mp4.resolve())
            if mp4.exists() and key not in state["uploaded"]:
                yield mp4, clip


def find_output_folders(root: Path):
    """`root` may be one clipper output folder, or a library folder containing many."""
    if (root / "work" / "clips.json").exists():
        return [root]
    subs = [p for p in root.iterdir() if (p / "work" / "clips.json").exists()]
    return sorted(subs, key=lambda p: (p / "work" / "clips.json").stat().st_mtime)


# ----------------------------------------------------------------------------
# YouTube API
# ----------------------------------------------------------------------------
def get_service(secrets=HERE / "client_secret.json", token=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if token is None:
        cid = active_channel()
        token = token_path(cid) if cid else LEGACY_TOKEN
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if Path(token).exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not Path(secrets).exists():
                sys.exit(f"Missing {secrets}. Follow README → 'Auto-posting setup' to create it.")
            log("Opening your browser to sign in to YouTube (one time) ...")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token).write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_video(youtube, mp4: Path, meta, publish_at=None, category_id="24", privacy="private",
                 on_progress=None):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    # A scheduled video must be private until YouTube publishes it at publish_at
    status = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at
    body = {"snippet": {**meta, "categoryId": category_id, "defaultLanguage": "en"}, "status": status}
    media = MediaFileUpload(str(mp4), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            progress, response = request.next_chunk()
            if progress:
                if on_progress:
                    on_progress(progress.progress())
                else:
                    print(f"\r    uploading {int(progress.progress() * 100)}%", end="", flush=True)
        except HttpError as e:
            reason = ""
            try:
                reason = json.loads(e.content)["error"]["errors"][0]["reason"]
            except Exception:
                pass
            if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
                raise QuotaExhausted(reason)
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1; time.sleep(2 ** retries); continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if retries >= 5:
                raise
            retries += 1; time.sleep(2 ** retries)
    print()
    return response


def upload_pending(root: Path, cfg, youtube=None, max_uploads=None):
    """Upload every not-yet-uploaded short under `root`. Returns number uploaded."""
    state = load_state()
    folders = find_output_folders(root)
    todo = list(pending_clips(folders, state))
    if max_uploads is not None:
        todo = todo[:max_uploads]
    if not todo:
        log("Nothing new to upload.")
        return 0
    youtube = youtube or get_service()
    channel_id = active_channel() or ""
    done = 0
    for mp4, clip in todo:
        meta = build_metadata(clip, add_credit=cfg.get("credit_original", True))
        publish_at, slot = None, None
        if cfg.get("schedule", True):
            slot = next_slot(cfg.get("post_times", ["12:00", "17:00", "20:00"]),
                             cfg.get("timezone", "Asia/Kolkata"), slots_for(state, channel_id))
            publish_at = to_rfc3339_utc(slot)
        when = slot.strftime("%a %d %b %H:%M") if slot else "private draft"
        log(f"Uploading {mp4.name}  →  {when}")
        try:
            resp = upload_video(youtube, mp4, meta, publish_at)
        except QuotaExhausted as e:
            log(f"YouTube limit reached ({e}). The rest stay queued and upload on the next run.")
            break
        vid = resp["id"]
        state["uploaded"][str(mp4.resolve())] = {"video_id": vid, "publish_at": publish_at, "channel_id": channel_id,
                                                 "uploaded_at": datetime.now().isoformat(timespec="seconds")}
        if slot:
            slots_for(state, channel_id).append(slot.isoformat())
        save_state(state)
        log(f"    done: https://youtube.com/shorts/{vid}")
        done += 1
    return done


def load_config():
    p = HERE / "config.json"
    return json.loads(p.read_text()) if p.exists() else {}


def main():
    ap = argparse.ArgumentParser(description="Upload finished shorts to YouTube on a posting schedule.")
    ap.add_argument("folder", help="a clipper output folder, or a library folder with many")
    ap.add_argument("--no-schedule", action="store_true", help="upload as private drafts without a publish time")
    ap.add_argument("--times", help='posting times, e.g. "12:00,17:00,20:00" (overrides config.json)')
    ap.add_argument("--max", type=int, help="upload at most this many now")
    args = ap.parse_args()
    cfg = load_config()
    if args.no_schedule:
        cfg["schedule"] = False
    if args.times:
        cfg["post_times"] = [t.strip() for t in args.times.split(",")]
    n = upload_pending(Path(args.folder), cfg, max_uploads=args.max)
    log(f"Uploaded {n} short(s).")


if __name__ == "__main__":
    main()
