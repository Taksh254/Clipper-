#!/usr/bin/env python3
"""
Viral Shorts Clipper
--------------------
Give it a YouTube link (or a local video). It will:
  1. read the transcript straight from YouTube's captions   (no video download)
  2. ask Claude to find the most viral-worthy moments
  3. download ONLY those few seconds                          (not the whole video)
  4. reframe each moment to vertical 9:16  (face-tracked crop, or blurred-background fill)
  5. burn in bold word-by-word animated captions + a hook title
  6. export ready-to-upload Shorts + clips.json with titles/descriptions/hashtags

Modes:
  default           captions -> Claude -> download only the chosen clips -> render
  --analyze-only    captions -> Claude -> list of timestamps + links. Downloads NO video at all.
  --full-download   old behaviour: download the whole video and transcribe it locally with Whisper
  --whisper         use local Whisper instead of YouTube captions (downloads audio only)

Usage:
  python clipper.py "https://www.youtube.com/watch?v=XXXX" --clips 5
  python clipper.py "https://www.youtube.com/watch?v=XXXX" --analyze-only
  python clipper.py my_podcast.mp4 --clips 3 --language hi
"""

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
RESOLUTIONS = {"4k": (2160, 3840), "1440": (1440, 2560), "1080": (1080, 1920)}
OUT_W, OUT_H = RESOLUTIONS["4k"]          # overwritten by --resolution
ASS_W, ASS_H = 1080, 1920                 # caption layout space; libass scales it to any output size
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"   # use "claude-opus-5-5" for best picks
SECTION_PAD = 1.0   # extra seconds fetched on each side of a clip
COOKIES_FROM_BROWSER = None   # set by --cookies-from-browser
BROWSERS = ["chrome", "chromium", "firefox", "brave", "edge", "opera", "vivaldi", "safari"]


def video_format():
    """Best source quality up to 4K (YouTube's 4K is VP9/AV1, so don't restrict to mp4)."""
    return "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b"


def log(msg):
    print(f"[clipper] {msg}", flush=True)


_ENCODERS = None


def h264_encoder():
    """libx264 when ffmpeg has it; otherwise libopenh264 (e.g. Fedora's ffmpeg-free ships without x264)."""
    global _ENCODERS
    if _ENCODERS is None:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        _ENCODERS = {line.split()[1] for line in out.splitlines() if len(line.split()) > 1}
    for enc in ("libx264", "libopenh264"):
        if enc in _ENCODERS:
            return enc
    sys.exit("Your ffmpeg has no H.264 encoder (libx264 or libopenh264). Install a full ffmpeg build (see README).")


def h264_args(quality):
    """Encoder arguments. quality='final' for the finished short, 'intermediate' for near-lossless cut sections."""
    if h264_encoder() == "libx264":
        if quality == "intermediate":
            return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "10"]
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18" if OUT_H >= 3840 else "19",
                "-profile:v", "high"]
    # libopenh264 has no CRF, so use bitrates: YouTube recommends ~35-45 Mbps for 4K and ~8-12 for 1080p
    mbps = {3840: 45, 2560: 24, 1920: 12}.get(OUT_H, 45)
    if quality == "intermediate":
        mbps = 80
    return ["-c:v", "libopenh264", "-profile:v", "high", "-rc_mode", "bitrate",
            "-b:v", f"{mbps}M", "-maxrate", f"{int(mbps * 1.5)}M"]


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise RuntimeError(f"Command failed: {' '.join(cmd[:4])} ...")
    return r.stdout


def probe(video: Path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height:format=duration",
               "-of", "json", str(video)])
    d = json.loads(out)
    return d["streams"][0]["width"], d["streams"][0]["height"], float(d["format"]["duration"])


def ydl(opts):
    import yt_dlp
    if COOKIES_FROM_BROWSER:   # lets YouTube see a signed-in browser (fixes "confirm you're not a bot")
        opts = {"cookiesfrombrowser": (COOKIES_FROM_BROWSER,), **opts}
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **opts})


# ----------------------------------------------------------------------------
# 1a. Video info + YouTube captions (no video download)
# ----------------------------------------------------------------------------
def get_info(url, work: Path):
    cache = work / "info.json"
    if cache.exists():
        return json.loads(cache.read_text())
    log("Reading video info from YouTube (no download) ...")
    with ydl({"skip_download": True}) as y:
        info = y.extract_info(url, download=False)
    keep = {k: info.get(k) for k in ("id", "title", "duration", "language", "webpage_url", "channel", "uploader")}
    keep["subtitles"] = sorted(k for k in (info.get("subtitles") or {}) if k != "live_chat")
    keep["automatic_captions"] = sorted((info.get("automatic_captions") or {}).keys())
    cache.write_text(json.dumps(keep, ensure_ascii=False, indent=2))
    return keep


def pick_caption_track(info, language):
    """Prefer auto-captions in the spoken language (they have per-word timing), then manual subs."""
    manual, auto = info["subtitles"], info["automatic_captions"]
    lang = None if language in (None, "auto") else language
    if lang:
        order = [(f"{lang}-orig", True), (lang, False), (lang, True)]
    else:
        vlang = info.get("language")
        # the spoken language's "-orig" track first, then any other "-orig" track
        orig = sorted((k for k in auto if k.endswith("-orig")), key=lambda k: k != f"{vlang}-orig")
        order = [(k, True) for k in orig]
        if vlang:
            order += [(vlang, False), (vlang, True)]
        # otherwise prefer English subs over whatever sorts first alphabetically (e.g. "de")
        order += [(k, False) for k in sorted(manual, key=lambda k: not k.split("-")[0] == "en")[:1]]
    for key, is_auto in order:
        if key in (auto if is_auto else manual):
            return key, is_auto
    return None, None


def parse_json3(path: Path):
    """Turn YouTube json3 captions into our word list [{w, s, e}]."""
    data = json.loads(path.read_text(encoding="utf-8"))
    words = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        t0, dur = ev["tStartMs"] / 1000, ev.get("dDurationMs", 0) / 1000
        toks = [(t0 + s.get("tOffsetMs", 0) / 1000, s.get("utf8", "")) for s in segs]
        toks = [(t, x) for t, x in toks if x.strip()]
        if not toks:
            continue
        has_offsets = any("tOffsetMs" in s for s in segs)
        if not has_offsets:            # manual subtitle line: spread words evenly over its duration
            line = " ".join(x.strip() for _, x in toks).split()
            step = (dur or 2.0) / max(1, len(line))
            toks = [(t0 + i * step, w) for i, w in enumerate(line)]
        for t, x in toks:
            for w in x.split():
                if re.fullmatch(r"\[.*\]", w):       # [Music], [Applause]
                    continue
                words.append({"w": w, "s": round(t, 2), "e": None, "_end": t0 + dur})
    words.sort(key=lambda w: w["s"])
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["_end"]
        w["e"] = round(max(w["s"] + 0.08, min(nxt, w["s"] + 0.9, w["_end"] if w["_end"] > w["s"] else 1e9)), 2)
        del w["_end"]
    return words


def words_to_transcript(words, language):
    segs, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (not nxt or nxt["s"] - w["e"] > 0.8 or w["e"] - cur[0]["s"] > 10
                or re.search(r"[.!?।]$", w["w"])):
            segs.append({"s": cur[0]["s"], "e": cur[-1]["e"],
                         "text": " ".join(x["w"] for x in cur), "words": cur})
            cur = []
    return {"language": language, "source": "youtube_captions", "segments": segs}


def youtube_transcript(url, info, work: Path, language):
    cache = work / "transcript.json"
    if cache.exists():
        log("Transcript cached, reusing it")
        return json.loads(cache.read_text())
    key, is_auto = pick_caption_track(info, language)
    if not key:
        return None
    log(f"Fetching YouTube captions ({key}{', auto-generated' if is_auto else ''}) — no video download")
    with ydl({"skip_download": True, "writesubtitles": not is_auto, "writeautomaticsub": is_auto,
              "subtitleslangs": [key], "subtitlesformat": "json3",
              "outtmpl": str(work / "captions")}) as y:
        y.download([url])
    files = list(work.glob("captions*.json3"))
    if not files:
        return None
    words = parse_json3(files[0])
    if not words:
        return None
    t = words_to_transcript(words, key.replace("-orig", "").split("-")[0])
    cache.write_text(json.dumps(t, ensure_ascii=False))
    return t


# ----------------------------------------------------------------------------
# 1b. Downloads (only what is needed)
# ----------------------------------------------------------------------------
def download_full(url, work: Path) -> Path:
    existing = list(work.glob("source.*"))
    if existing:
        return existing[0]
    log("Downloading the full video (best quality up to 4K) ...")
    with ydl({"format": video_format(), "merge_output_format": "mp4/mkv",
              "outtmpl": str(work / "source.%(ext)s")}) as y:
        y.download([url])
    return next(work.glob("source.*"))


def download_audio(url, work: Path) -> Path:
    existing = list(work.glob("audio.*"))
    if existing:
        return existing[0]
    log("Downloading audio only (for Whisper transcription) ...")
    with ydl({"format": "ba/b", "outtmpl": str(work / "audio.%(ext)s")}) as y:
        y.download([url])
    return next(work.glob("audio.*"))


def download_section(url, start, end, work: Path, idx) -> Path:
    """Download just [start, end] of the video. Returns the file path."""
    name = f"section_{idx:02d}_{int(start)}"

    def finished():   # ignore leftovers of an interrupted download (.part / .ytdl)
        return [p for p in work.glob(f"{name}.*") if p.suffix not in (".part", ".ytdl")]

    if finished():
        return finished()[0]
    import yt_dlp
    from yt_dlp.utils import download_range_func
    # Exact cuts need a re-encode (a stream copy starts at the nearest keyframe, which shifts the timing
    # by seconds and puts captions out of sync). Without explicit codecs ffmpeg may pick a very slow one
    # (libvpx: ~10 min per clip at 4K), so encode a near-lossless fast H.264 + FLAC section instead.
    opts = {"format": video_format(), "merge_output_format": "mkv",
            "outtmpl": str(work / f"{name}.%(ext)s"),
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,
            "external_downloader_args": {"ffmpeg_o": [*h264_args("intermediate"),
                                                      "-pix_fmt", "yuv420p", "-c:a", "flac"]}}
    # YouTube stream URLs occasionally fail mid-request, and after many requests YouTube may briefly
    # answer "confirm you're not a bot"; waiting a bit before retrying gets past both.
    waits = [20, 90]
    for attempt in range(len(waits) + 1):
        try:
            with ydl(opts) as y:
                y.download([url])
            break
        except yt_dlp.utils.DownloadError as e:
            for p in work.glob(f"{name}.*"):
                p.unlink()
            if attempt == len(waits):
                raise
            log(f"    download failed ({str(e).replace('ERROR: ', '').strip()[:100]}), "
                f"retrying in {waits[attempt]}s ...")
            time.sleep(waits[attempt])
    return finished()[0]


# ----------------------------------------------------------------------------
# 2. Local transcription (Whisper) — used for local files or --whisper
# ----------------------------------------------------------------------------
def whisper_transcribe(media: Path, work: Path, model_size: str, language):
    cache = work / "transcript.json"
    if cache.exists():
        log("Transcript cached, reusing it")
        return json.loads(cache.read_text())
    from faster_whisper import WhisperModel
    log(f"Transcribing with faster-whisper ({model_size}) ... this is the slow step")
    device = "cuda" if shutil.which("nvidia-smi") else "cpu"
    model = WhisperModel(model_size, device=device, compute_type="float16" if device == "cuda" else "int8")
    segments, info = model.transcribe(str(media), word_timestamps=True, vad_filter=True,
                                      language=None if language in (None, "auto") else language)
    data = {"language": info.language, "source": "whisper", "segments": []}
    for seg in segments:
        words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                 for w in (seg.words or []) if w.word.strip()]
        data["segments"].append({"s": round(seg.start, 2), "e": round(seg.end, 2),
                                 "text": seg.text.strip(), "words": words})
        print(f"\r  transcribed up to {seg.end/60:5.1f} min", end="", flush=True)
    print()
    cache.write_text(json.dumps(data, ensure_ascii=False))
    return data


def all_words(transcript):
    """Spoken words only: drops auto-caption speaker marks (">>") and [sound tags], even multi-word
    ones like "[music and cheering]", so they never show up in the burned-in captions."""
    words = []
    for seg in transcript["segments"]:
        in_tag = False
        for w in seg["words"]:
            t = w["w"]
            if t.startswith("["):
                in_tag = True
            if in_tag:
                in_tag = not t.endswith("]")
                continue
            if re.search(r"\w", t):
                words.append(w)
    return words


# ----------------------------------------------------------------------------
# 3. Find viral moments with Claude
# ----------------------------------------------------------------------------
VIRAL_PROMPT = """You are an elite short-form video editor who has grown multiple YouTube Shorts,
TikTok and Reels channels to millions of followers. Below is a timestamped transcript of a
long video{title}. Find the {n} moments with the highest potential to go viral as standalone Shorts.

A great Short:
- HOOKS in the first 1-3 seconds: a bold claim, a question, a surprising fact, conflict, or an emotional line.
  Start the clip ON the hook, never on filler ("so", "um", "anyway", greetings).
- Is fully SELF-CONTAINED: a stranger scrolling with zero context understands it.
- Has a PAYOFF: a punchline, reveal, strong opinion, useful takeaway, or emotional peak, and ends right after it.
- Triggers emotion: surprise, humor, controversy, inspiration, relatability, curiosity, or practical value.
- Is {min_len}-{max_len} seconds long. Tighter is better when the story allows.

Rules:
- Use the exact timestamps from the transcript (seconds). Start at the beginning of a sentence, end at the end of one.
  (The transcript may lack punctuation if it comes from auto-captions; infer sentence boundaries from meaning.)
- Clips must not overlap.
- Rank by virality score (1-10). Be honest; don't give everything a 9.
- title: a punchy Shorts title (max ~60 chars) in the transcript's language.
- hook_text: 2-6 word on-screen hook shown at the top for the first seconds (e.g. "He almost lost everything").
- Write description and hashtags for YouTube Shorts.

TRANSCRIPT (format: [start-end] text):
{transcript}
"""

# JSON schema for Claude's answer (structured outputs guarantee the reply matches it)
CLIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "start time in seconds"},
                    "end": {"type": "number", "description": "end time in seconds"},
                    "virality_score": {"type": "number"},
                    "title": {"type": "string"},
                    "hook_text": {"type": "string"},
                    "why_viral": {"type": "string"},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start", "end", "virality_score", "title",
                             "hook_text", "why_viral", "description", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def transcript_for_llm(transcript):
    return "\n".join(f"[{s['s']:.1f}-{s['e']:.1f}] {s['text']}" for s in transcript["segments"])


LOCAL_WINDOW_NOTE = """
IMPORTANT: this is only one part of the video. Every clip must START between {w0:.0f} and {w1:.0f}
seconds; the lines after {w1:.0f} are only there so a clip can finish. Pick the {n} strongest moment(s)
in this part, and keep why_viral to one short sentence.
"""


def local_ai_clips(prompt, model, max_clips=None):
    """Pick clips with a free local model through Ollama (model given as "ollama:<name>")."""
    import urllib.error
    import urllib.request
    name = model.split(":", 1)[1]
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    host = host if host.startswith("http") else f"http://{host}"
    # Ollama silently drops whatever doesn't fit its context window, so size it to the prompt
    # (~3.5 characters per token) plus room for the answer.
    need = len(prompt) // 3 + 3000
    num_ctx = next((c for c in (4096, 8192, 16384, 32768, 65536, 131072) if c >= need), 131072)
    # Ollama defaults to 4 threads; on a laptop about half the logical cores is ~25% faster.
    threads = int(os.environ.get("OLLAMA_NUM_THREAD") or max(4, min(12, (os.cpu_count() or 8) // 2)))
    schema = CLIPS_SCHEMA
    if max_clips:   # small models ignore "pick N" and write a dozen; the schema caps it for real
        schema = json.loads(json.dumps(CLIPS_SCHEMA))
        schema["properties"]["clips"].update(minItems=1, maxItems=max_clips)
    body = json.dumps({"model": name, "stream": False, "think": False, "format": schema,
                       "messages": [{"role": "user", "content": prompt}],
                       "options": {"num_ctx": num_ctx, "temperature": 0.3,
                                   "num_thread": threads}}).encode()
    req = urllib.request.Request(f"{host}/api/chat", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3 * 3600) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 404:
            sys.exit(f"Local model '{name}' isn't downloaded. Run: ollama pull {name}")
        sys.exit(f"Ollama error {e.code}: {detail}")
    except (urllib.error.URLError, ConnectionError) as e:
        sys.exit(f"Can't reach Ollama at {host} ({e}). Start it with: ollama serve")
    if data.get("done_reason") == "length":
        sys.exit("The local AI's answer was cut off. Try fewer --clips.")
    return json.loads(data["message"]["content"])["clips"]


def clean_clips(clips):
    """Drop malformed picks (local models occasionally return nonsense times)."""
    good = []
    for c in clips:
        try:
            c["start"], c["end"] = float(c["start"]), float(c["end"])
            c["virality_score"] = max(1.0, min(10.0, float(c["virality_score"])))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= c["start"] < c["end"] and str(c.get("title", "")).strip():
            c["hashtags"] = [str(h) for h in c.get("hashtags") or []]
            for k in ("hook_text", "why_viral", "description"):
                c[k] = str(c.get(k) or "")
            good.append(c)
    return good


def widen_short_clips(clips, transcript, min_len, max_len):
    """Small local models tend to pick a single sentence. Keep that moment as the hook and grow the
    clip along sentence boundaries (mostly forward, where the payoff is) to a proper Short length."""
    segs = transcript["segments"]
    target = min(max_len, max(min_len, (min_len + max_len) / 2))
    for c in clips:
        if c["end"] - c["start"] >= min_len:
            continue
        i = next((k for k, s in enumerate(segs) if s["e"] > c["start"]), None)
        if i is None:
            continue
        j = max(i, max((k for k, s in enumerate(segs) if s["s"] < c["end"]), default=i))
        while j + 1 < len(segs) and segs[j]["e"] - segs[i]["s"] < target \
                and segs[j + 1]["e"] - segs[i]["s"] <= max_len:
            j += 1
        while i > 0 and segs[j]["e"] - segs[i]["s"] < min_len \
                and segs[j]["e"] - segs[i - 1]["s"] <= max_len:
            i -= 1
        c["start"], c["end"] = segs[i]["s"], segs[j]["e"]
    return clips


def local_windowed_clips(transcript, n, min_len, max_len, model, work: Path, title=None):
    """Small local models given a whole long transcript only look at its first minutes, and run
    ~10x slower on a long context. So ask for the best moment(s) in each 2.5-4 minute window
    instead, and rank them together. Each window's answer is cached, so a stopped run resumes."""
    segs = transcript["segments"]
    total = segs[-1]["e"]
    windows = max(1, min(n, round(total / 150)))
    per = math.ceil(n / windows)
    span = total / windows
    name = model.split(":", 1)[1]
    log(f"Asking the local AI ({name}, free) to find the most viral moments in {windows} parts "
        f"of the video ... on a laptop CPU this takes about 1-2 minutes per part")
    clips = []
    for k in range(windows):
        w0, w1 = k * span, (k + 1) * span
        cache = work / f"local_clips_{name.replace(':', '_')}_{n}_{min_len}_{max_len}_{k}.json"
        if cache.exists():
            got = json.loads(cache.read_text())
        else:
            part = {"segments": [s for s in segs if s["e"] > w0 and s["s"] < w1 + max_len]}
            prompt = VIRAL_PROMPT.format(n=per, min_len=min_len, max_len=max_len,
                                         title=f' titled "{title}"' if title else "",
                                         transcript=transcript_for_llm(part))
            prompt += LOCAL_WINDOW_NOTE.format(w0=w0, w1=w1, n=per)
            t0 = time.time()
            got = local_ai_clips(prompt, model, max_clips=per)
            cache.write_text(json.dumps(got, ensure_ascii=False, indent=2))
            log(f"    part {k + 1}/{windows} done in {time.time() - t0:.0f}s")
        clips += [c for c in clean_clips(got) if w0 - 5 <= c["start"] < w1 + 5][:per]
    return clips


def find_viral_clips(transcript, n, min_len, max_len, model, work: Path, title=None):
    cache = work / f"claude_clips_{n}_{min_len}_{max_len}.json"
    if cache.exists():
        log("Clip selection cached, reusing it (delete work/claude_clips_*.json to re-pick)")
        return json.loads(cache.read_text())
    prompt = VIRAL_PROMPT.format(n=n, min_len=min_len, max_len=max_len,
                                 title=f' titled "{title}"' if title else "",
                                 transcript=transcript_for_llm(transcript))
    if model.startswith("ollama:"):
        clips = widen_short_clips(local_windowed_clips(transcript, n, min_len, max_len, model, work, title),
                                  transcript, min_len, max_len)
        if not clips:
            sys.exit("The local AI didn't return any usable clips. Try again or use a bigger model.")
        clips.sort(key=lambda c: -c["virality_score"])
        cache.write_text(json.dumps(clips, ensure_ascii=False, indent=2))
        return clips
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    log(f"Asking Claude ({model}) to find the most viral moments ...")
    # Structured outputs rather than a forced tool call: current models (e.g. Sonnet 5.5) reject
    # tool_choice {"type": "tool"} with a 400.
    resp = client.messages.create(
        model=model, max_tokens=16000,
        output_config={"format": {"type": "json_schema", "schema": CLIPS_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        sys.exit("Claude declined to pick clips from this video"
                 + (f" ({resp.stop_details.explanation})" if resp.stop_details else "") + ".")
    if resp.stop_reason == "max_tokens":
        sys.exit("Claude's answer was cut off (too long). Try fewer --clips.")
    clips = json.loads(next(b.text for b in resp.content if b.type == "text"))["clips"]
    clips.sort(key=lambda c: -c["virality_score"])
    cache.write_text(json.dumps(clips, ensure_ascii=False, indent=2))
    return clips


def snap_to_words(clip, words, min_len, max_len, duration):
    """Snap Claude's rough times to real word boundaries so we never cut mid-word."""
    s, e = clip["start"], clip["end"]
    inside = [w for w in words if w["s"] >= s - 0.6 and w["e"] <= e + 0.6]
    if not inside:
        return None
    start = max(0.0, inside[0]["s"] - 0.15)
    end = min(duration, inside[-1]["e"] + 0.35)
    if end - start > max_len:
        inside = [w for w in inside if w["e"] <= start + max_len]
        end = inside[-1]["e"] + 0.35
    if end - start < min_len * 0.6:
        return None
    return round(start, 2), round(end, 2)


# ----------------------------------------------------------------------------
# 4. Reframe to 9:16 (face-aware)
# ----------------------------------------------------------------------------
def face_center_x(video: Path, start, end):
    """Median horizontal face position (source pixels) in [start, end] of `video`, or None if no steady face.
    Frames are decoded by ffmpeg, so this works on 4K VP9/AV1 sources too."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    if not hasattr(cv2, "CascadeClassifier"):   # OpenCV 5 moved Haar cascades out of the main package
        log("    note: this OpenCV has no face detector (pip install \"opencv-python-headless<5\"); using blur layout")
        return None
    src_w, src_h, _ = probe(video)
    fw = 480
    fh = int(round(src_h * fw / src_w / 2) * 2)
    fps = min(2.0, 40 / max(1.0, end - start))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", str(video), "-t", f"{end - start:.2f}",
         "-vf", f"fps={fps:.3f},scale={fw}:{fh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True).stdout
    frame_size = fw * fh * 3
    frames = [np.frombuffer(raw[i:i + frame_size], np.uint8).reshape(fh, fw, 3)
              for i in range(0, len(raw) - frame_size + 1, frame_size)]
    if not frames:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    xs = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + w / 2) * src_w / fw)
    if len(xs) / len(frames) < 0.4:
        return None
    return statistics.median(xs)


def video_filter(layout, src_w, src_h, face_x):
    # High-quality scaling; a light sharpen only when we have to upscale (e.g. 1080p source -> 4K)
    def up(src_px_h):
        sharpen = ",unsharp=5:5:0.6:5:5:0.0" if src_px_h < OUT_H * 0.9 else ""
        return f"scale={OUT_W}:{OUT_H}:flags=lanczos{sharpen}"

    if layout == "crop" or (layout == "auto" and face_x is not None):
        cw = int(src_h * 9 / 16) // 2 * 2
        if cw >= src_w:
            return (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={OUT_W}:{OUT_H}"), "crop"
        cx = face_x if face_x is not None else src_w / 2
        x = int(min(max(cx - cw / 2, 0), src_w - cw))
        return f"crop={cw}:{src_h}:{x}:0,{up(src_h)}", "crop"
    # Blurred background (blurred at low res, then scaled up — fast even at 4K) + full frame in the middle
    fg_h = int(OUT_W * src_h / src_w)
    sharpen = ",unsharp=5:5:0.5:5:5:0.0" if src_h < fg_h * 0.9 else ""
    return (f"split[a][b];[a]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,"
            f"gblur=sigma=18,eq=brightness=-0.08,scale={OUT_W}:{OUT_H}:flags=bicubic[bg];"
            f"[b]scale={OUT_W}:-2:flags=lanczos{sharpen}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"), "blur"


# ----------------------------------------------------------------------------
# 5. Captions (ASS, word-by-word highlight)
# ----------------------------------------------------------------------------
def ass_time(t):
    t = max(0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def group_words(words, max_words=3, max_chars=16):
    """Split words into short one-line caption groups (never wider than ~max_chars)."""
    groups, cur = [], []
    for i, w in enumerate(words):
        if cur and sum(len(x["w"]) + 1 for x in cur) + len(w["w"]) > max_chars:
            groups.append(cur); cur = []          # this word would overflow the line
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        if len(cur) >= max_words or gap > 0.45 or re.search(r"[.!?,;:।]$", w["w"]):
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words, clip_start, clip_end, hook_text, font, highlight, layout, uppercase=True):
    hl = {"yellow": "&H0000F0FF", "green": "&H0033FF33", "cyan": "&H00FFFF00",
          "red": "&H003C3CFF", "pink": "&H00B469FF"}.get(highlight, "&H0000F0FF")
    margin_v = 560 if layout == "crop" else 420
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ASS_W}
PlayResY: {ASS_H}
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,7,3,2,60,60,{margin_v},1
Style: Hook,{font},66,&H00000000,&H00000000,&H00FFFFFF,&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,190,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook_text:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(3.2)},Hook,,0,0,0,,"
                     f"{{\\fad(150,250)}}{ass_escape(hook_text.upper() if uppercase else hook_text)}")
    clip_words = [w for w in words if w["s"] >= clip_start - 0.05 and w["e"] <= clip_end + 0.4]
    events = []
    for g in group_words(clip_words):
        tokens = [ass_escape(w["w"].upper() if uppercase else w["w"]) for w in g]
        for i, w in enumerate(g):
            start = w["s"] - clip_start
            end = (g[i + 1]["s"] if i + 1 < len(g) else g[-1]["e"] + 0.12) - clip_start
            parts = [f"{{\\c{hl}\\fscx112\\fscy112}}{tok}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}"
                     if j == i else tok for j, tok in enumerate(tokens)]
            pop = "{\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            events.append([start, end, pop + " ".join(parts)])
    # Never show two caption lines at once: libass would stack or overprint them. Each event ends
    # no later than the next one starts (the +0.12s hold above often runs into the next group).
    for ev, nxt in zip(events, events[1:]):
        ev[1] = min(ev[1], nxt[0])
    lines += [f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{text}"
              for s, e, text in events if e - s >= 0.01]
    return header + "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# 6. Render
# ----------------------------------------------------------------------------
def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "_", s)[:50] or "clip"


def safe_filename(s, max_len=70):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', "", s).strip().rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:max_len].rstrip() or "short"


def auto_clip_count(duration):
    """Roughly one short per 4 minutes of source video, between 3 and 15."""
    return max(3, min(15, round(duration / 240)))


def write_post_sheet(results, path: Path):
    lines = ["Ready-to-post Shorts — copy the title, description and hashtags when uploading.", ""]
    for r in results:
        tags = [t if t.startswith("#") else f"#{t}" for t in r.get("hashtags", [])]
        if "#shorts" not in [t.lower() for t in tags]:
            tags.insert(0, "#shorts")
        lines += [f"=== {r['file']} ===   (virality {r['virality_score']}/10, {r['duration']}s)",
                  f"TITLE: {r['title']}", "", "DESCRIPTION:", r.get("description", "").strip(), "",
                  " ".join(tags), "", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def render_clip(video, seek, length, vf, ass_path: Path, out_path: Path, fonts_dir=None):
    """Cut `length` seconds starting at `seek` (seconds into `video`), reframe, burn captions."""
    # ffmpeg runs inside the .ass file's folder so the filter only sees a plain file name
    # (absolute paths like C:\... break ffmpeg filter parsing on Windows)
    fd = Path(fonts_dir).resolve().as_posix().replace(":", "\\:") if fonts_dir else None
    ass_arg = f"ass={ass_path.name}" + (f":fontsdir='{fd}'" if fd else "")
    cmd = ["ffmpeg", "-y", "-ss", f"{seek:.2f}", "-i", str(Path(video).resolve()), "-t", f"{length:.2f}",
           "-filter_complex", f"[0:v]{vf},{ass_arg}[v]", "-map", "[v]", "-map", "0:a?",
           *h264_args("final"),
           "-pix_fmt", "yuv420p", "-fpsmax", "60",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
           "-movflags", "+faststart", str(Path(out_path).resolve())]
    run(cmd, cwd=ass_path.parent)


def fmt_ts(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Turn a long video into viral-ready Shorts with captions.")
    ap.add_argument("source", help="YouTube URL or path to a local video")
    ap.add_argument("--clips", default="auto",
                    help="how many shorts to make. 'auto' (default) = about 1 per 4 min of video, 3 to 15")
    ap.add_argument("--resolution", choices=list(RESOLUTIONS), default="4k",
                    help="output size: 4k = 2160x3840 (default), 1440 = 1440x2560, 1080 = 1080x1920")
    ap.add_argument("--min", dest="min_len", type=int, default=20, help="min clip length in seconds")
    ap.add_argument("--max", dest="max_len", type=int, default=59, help="max clip length in seconds")
    ap.add_argument("--out", default="output", help="output folder")
    ap.add_argument("--analyze-only", action="store_true",
                    help="only find the viral moments (timestamps + titles). Downloads no video at all.")
    ap.add_argument("--full-download", action="store_true",
                    help="download the whole video and transcribe locally (old behaviour)")
    ap.add_argument("--whisper", action="store_true",
                    help="transcribe locally with Whisper instead of YouTube captions (downloads audio only)")
    ap.add_argument("--whisper-model", default="small", help="tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default="auto", help="e.g. en, hi. 'auto' detects it")
    ap.add_argument("--layout", choices=["auto", "crop", "blur"], default="auto",
                    help="auto = face-crop when a speaker is visible, otherwise blurred background")
    ap.add_argument("--font", default=None, help="caption font name")
    ap.add_argument("--fonts-dir", default=None, help="folder with .ttf files to use for captions")
    ap.add_argument("--highlight", default="yellow", choices=["yellow", "green", "cyan", "red", "pink"])
    ap.add_argument("--no-upper", action="store_true", help="keep caption casing as spoken")
    ap.add_argument("--model", default=DEFAULT_CLAUDE_MODEL,
                    help="Claude model for picking clips, or ollama:<name> (e.g. ollama:qwen3:4b) "
                         "for a free local model via Ollama")
    ap.add_argument("--cookies-from-browser", choices=BROWSERS, default=None,
                    help="use this browser's YouTube login if YouTube says \"confirm you're not a bot\"")
    args = ap.parse_args()

    global OUT_W, OUT_H, COOKIES_FROM_BROWSER
    OUT_W, OUT_H = RESOLUTIONS[args.resolution]
    COOKIES_FROM_BROWSER = args.cookies_from_browser
    if args.clips != "auto" and not args.clips.isdigit():
        sys.exit("--clips must be a number or 'auto'")
    if not args.analyze_only:
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                sys.exit(f"{tool} not found. Install ffmpeg first (see README).")
    if not args.model.startswith("ollama:") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first (get one at console.anthropic.com), "
                 "or use a free local model: --model ollama:qwen3:4b")

    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    work = out / "work"; work.mkdir(exist_ok=True)
    is_local = os.path.exists(args.source)

    # ---- get a transcript --------------------------------------------------
    full_video, info, title = None, None, None
    if is_local:
        full_video = Path(args.source).resolve()
        duration = probe(full_video)[2]
        transcript = whisper_transcribe(full_video, work, args.whisper_model, args.language)
    else:
        info = get_info(args.source, work)
        duration, title = info["duration"], info.get("title")
        log(f"Video: \"{title}\" ({duration/60:.1f} min)")
        transcript = None
        if args.full_download:
            full_video = download_full(args.source, work)
            transcript = whisper_transcribe(full_video, work, args.whisper_model, args.language)
        elif not args.whisper:
            transcript = youtube_transcript(args.source, info, work, args.language)
            if not transcript:
                log("This video has no YouTube captions — falling back to Whisper on the audio only.")
        if not transcript:
            transcript = whisper_transcribe(download_audio(args.source, work), work,
                                            args.whisper_model, args.language)

    words = all_words(transcript)
    if not words:
        sys.exit("No speech found in the video.")

    # ---- pick clips ---------------------------------------------------------
    n_clips = auto_clip_count(duration) if args.clips == "auto" else int(args.clips)
    log(f"Making {n_clips} shorts from this video")
    # ask for a few extra candidates so we still have enough after overlap/length checks
    candidates = find_viral_clips(transcript, n_clips + 3, args.min_len, args.max_len,
                                  args.model, work, title)
    picked, used = [], []
    for c in candidates:
        if len(picked) >= n_clips:
            break
        snapped = snap_to_words(c, words, args.min_len, args.max_len, duration)
        if not snapped:
            continue
        start, end = snapped
        if any(start < ue and end > us for us, ue in used):
            continue
        used.append((start, end))
        c = {**c, "start": start, "end": end, "duration": round(end - start, 1)}
        if info:
            c["youtube_link"] = f"https://www.youtube.com/watch?v={info['id']}&t={int(start)}s"
            c["source_url"] = info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}"
            c["source_title"] = info.get("title")
            c["source_channel"] = info.get("channel") or info.get("uploader")
        picked.append(c)

    # ---- analyze-only: stop here, no video touched --------------------------
    if args.analyze_only:
        (out / "clips.json").write_text(json.dumps(picked, ensure_ascii=False, indent=2))
        print()
        for i, c in enumerate(picked, 1):
            print(f"{i}. [{fmt_ts(c['start'])} - {fmt_ts(c['end'])}]  score {c['virality_score']}/10  {c['title']}")
            print(f"   hook: {c['hook_text']}")
            print(f"   why:  {c['why_viral']}")
            if c.get("youtube_link"):
                print(f"   {c['youtube_link']}")
        log(f"Saved to {out / 'clips.json'} — no video was downloaded.")
        return

    # ---- render --------------------------------------------------------------
    # The output folder gets ONLY the finished MP4s + POST_THESE.txt. Everything else lives in work/.
    font = args.font or ("Noto Sans Devanagari" if transcript["language"] in ("hi", "mr", "ne")
                         else "Montserrat ExtraBold")
    log(f"Rendering {len(picked)} shorts at {OUT_W}x{OUT_H} ...")
    results = []
    for idx, c in enumerate(picked, 1):
        start, end = c["start"], c["end"]
        fname = f"{idx:02d} - {safe_filename(c['title'])}.mp4"
        log(f"[{idx}/{len(picked)}] {c['title']}  ({fmt_ts(start)}-{fmt_ts(end)}, score {c['virality_score']})")

        if full_video:
            src, seek = full_video, start
        else:
            pad_start = max(0.0, start - SECTION_PAD)
            log(f"    downloading only this {end - start:.0f}s section ...")
            src = download_section(args.source, pad_start, min(duration, end + SECTION_PAD), work, idx)
            seek = start - pad_start
        src_w, src_h, _ = probe(src)
        if src_h < 1080 or src_w < 1080:
            log(f"    note: source is only {src_w}x{src_h}; it will be upscaled to {OUT_W}x{OUT_H}")

        face_x = face_center_x(src, seek, seek + (end - start)) if args.layout != "blur" else None
        vf, mode = video_filter(args.layout, src_w, src_h, face_x)
        ass_path = work / f"{idx:02d}_{slugify(c['title'])}.ass"
        ass_path.write_text(build_ass(words, start, end, c.get("hook_text", ""), font,
                                      args.highlight, mode, uppercase=not args.no_upper), encoding="utf-8")
        mp4 = out / fname
        render_clip(src, seek, end - start, vf, ass_path, mp4, args.fonts_dir)
        log(f"    saved {fname}  (layout: {mode}, source {src_w}x{src_h})")
        results.append({**c, "file": fname, "layout": mode, "source_resolution": f"{src_w}x{src_h}"})

    (work / "clips.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    write_post_sheet(results, out / "POST_THESE.txt")
    log(f"Done! {len(results)} ready-to-post shorts in {out}")
    log("Titles, descriptions & hashtags for each one are in POST_THESE.txt")


if __name__ == "__main__":
    main()
