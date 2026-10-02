# Viral Shorts Clipper

Paste one YouTube link → get **several ready-to-post vertical Shorts** of its most viral-worthy moments, with bold word-by-word animated captions and a hook title. You also get a title, description and hashtags for each one, and you can post or schedule them to your YouTube channel(s) straight from the web app.

It runs **free on your own computer**: clip picking uses a local AI model through [Ollama](https://ollama.com) (`qwen3:4b` by default), so no API key or credit is needed. Claude models are an optional paid upgrade.

## How it works

1. **Read captions, no download**: the transcript comes straight from YouTube's captions, with timing for each word. Videos without captions are transcribed locally with Whisper (audio only).
2. **Pick moments**: an AI reads the timestamped transcript the way a Shorts editor would. It looks for a hook in the first seconds, a self-contained story, a payoff and an emotional trigger, and scores each moment 1–10. By default it makes about **one short per 4 minutes of video** (3 to 15 shorts), with no overlaps.
   - **Local model (free, default)**: the video is read in ~2.5-minute parts, and the best moment of each part is picked. Small models picked only the first minute when given the whole transcript, so this keeps picks spread across the video and fast on a laptop CPU.
   - **Claude (optional, paid)**: reads the whole transcript in one call.
3. **Snap**: cut points are snapped to real word boundaries, so nothing gets cut mid-word.
4. **Download only the clips**: just the chosen seconds are fetched, in the best quality available, never the whole video.
5. **Reframe to 9:16**:
   - If a speaker's face is visible, the frame is cropped around the face.
   - Otherwise the full frame sits over a blurred background, which suits screen recordings and wide shots.
6. **Captions**: 2–3 words appear at a time on one line, the spoken word pops in yellow, and a hook banner shows for the first 3 seconds.
7. **Export**: H.264 MP4 at 1080×1920, 1440×2560 or 4K 2160×3840, keeping the source frame rate up to 60fps, with audio loudness-normalised to the level Shorts expects.

## Setup (one time)

1. Install **Python 3.9+** and **ffmpeg**:
   - Windows: `winget install ffmpeg`
   - Mac: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
   - Fedora: the preinstalled `ffmpeg-free` has no x264 encoder. The clipper still works (it falls back to OpenH264), but for the best quality install the full build from RPM Fusion:
     ```
     sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
     sudo dnf swap ffmpeg-free ffmpeg --allowerasing
     ```
2. Install the Python packages in a virtual environment:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Install **Ollama** from https://ollama.com and download the free model (~2.5 GB):
   ```
   ollama pull qwen3:4b
   ```
   `qwen3:8b` (~5 GB) picks slightly better clips but is much slower and needs more RAM (see *Hardware* below).
4. (Optional) To use Claude instead, get an API key at https://console.anthropic.com (paid per use) and set it:
   - Windows (PowerShell): `$env:ANTHROPIC_API_KEY="sk-ant-..."`
   - Mac/Linux: `export ANTHROPIC_API_KEY="sk-ant-..."`
5. (Recommended) Install the free **Montserrat** font from Google Fonts. This gives captions the classic Shorts look; without it, a similar bold font is used instead. For Hindi, install **Noto Sans Devanagari**.

## Web UI (easiest)

```
./start.sh          # starts Ollama if needed, then the web app, and opens your browser
```

Or run `.venv/bin/python webui.py` for just the server. It's at http://127.0.0.1:8000 and only listens on this computer.

From there you can:
- paste a YouTube link or drag in a video file, and pick the AI model, mode, resolution, clip count, lengths and caption colour. The defaults are the free `qwen3:4b` and 1080p, which is the fastest combination on a laptop.
- follow a live progress bar and log, and stop a job
- watch each short in the browser, then **edit its title, description and hashtags** (with YouTube's limits shown)
- **connect one or more YouTube channels** (button at the top right; needs the one-time Google setup in *Auto-posting setup* below). Use **Add another account** for more channels, **Use this** to pick which one uploads go to, and **Disconnect** to sign one out. Each channel has its own posting slots.
- **post or schedule** each short: next free posting slot, a date and time you pick, publish now, or private draft. **Schedule all** does a whole run at once.
- see everything uploaded and scheduled on the **Scheduled** tab, check its live status on YouTube, and set your daily posting times and time zone

Scheduled shorts are uploaded straight away with a publish time, so YouTube publishes them even when your computer is off. Uploads made here and by the autopilot share `upload_state.json`, so nothing is posted twice.

Each run is saved in `runs/<id>/`, and past runs are listed on the left. To use Claude, enter your API key with the button at the top right. The key is kept in memory only, so you'll re-enter it after a restart, unless you set `ANTHROPIC_API_KEY` before starting the server.

## Command line

```
python clipper.py "https://www.youtube.com/watch?v=VIDEO_ID" --model ollama:qwen3:4b
```

On the command line, `--model` defaults to `claude-sonnet-5-5`, which needs an API key. Pass `--model ollama:qwen3:4b` (or `ollama:qwen3:8b`) to stay free.

Your output folder contains only what you post:

```
output/
  01 - I lost everything twice.mp4
  02 - The habit that changed everything.mp4
  ...
  POST_THESE.txt   ← title, description and hashtags (#shorts included) for each video
  work/            ← behind-the-scenes files; ignore it (it makes re-runs fast)
```

### How much gets downloaded

| Mode | Command | What's downloaded |
|---|---|---|
| **Default** | `python clipper.py URL` | Only the seconds you'll use (e.g. 5 clips × 40s ≈ 3 min of video) |
| **Analyze only** | `python clipper.py URL --analyze-only` | **Nothing.** You get timestamps, titles, hooks and `&t=` links in `clips.json` to cut yourself (CapCut, YouTube's "Create → Clip", etc.) |
| **Whisper** | `python clipper.py URL --whisper` | Audio only, for more accurate transcription, then only the clips |
| **Full** | `python clipper.py URL --full-download` | The whole video |

If a video has no captions at all, the default mode automatically switches to Whisper on the audio only.

### Useful options

| Option | What it does |
|---|---|
| `--model ollama:qwen3:4b` | Picks clips with a free local model. Any Ollama model works as `ollama:<name>`; Claude models (`claude-sonnet-5-5`, `claude-opus-5-5`) need an API key. |
| `--clips 8` | Makes exactly 8 shorts. The default `auto` makes about 1 per 4 min of video. |
| `--resolution 1080` | Sets the output size: `4k` (CLI default), `1440` or `1080`. |
| `--min 15 --max 45` | Sets the clip length range in seconds (default 20–59). |
| `--language hi` | Forces the language instead of auto-detecting it. Useful for Hindi or Hinglish. |
| `--whisper-model medium` | Uses a more accurate Whisper model with `--whisper` or local files (`large-v3` is the best but slowest). |
| `--layout blur` | Always uses the blurred-background layout. `crop` always uses face-crop. |
| `--highlight green` | Changes the active-word colour: yellow, green, cyan, red or pink. |
| `--font "Anton"` / `--fonts-dir DIR` | Uses a different caption font, or fonts from a folder. |
| `--no-upper` | Keeps the caption casing as spoken instead of ALL CAPS. |
| `--cookies-from-browser chrome` | Uses your browser's YouTube login, for videos that need sign-in or age checks. |
| `--out DIR` | Sets the output folder (default `output`). |
| Local file instead of URL | `python clipper.py podcast.mp4` |

Environment variables: `OLLAMA_HOST` (default `http://127.0.0.1:11434`) and `OLLAMA_NUM_THREAD` (CPU threads for the local model).

### About quality

- The final quality depends on the source video. For true 4K sharpness, the long video should be 4K.
- A 1080p source exported at 4K is upscaled with high-quality scaling and a light sharpen. YouTube gives 4K uploads a higher playback bitrate, so they look cleaner on phones.
- 1080p renders much faster. Use it for quick runs and on slower laptops.

## Hardware and speed

Measured on a laptop with 15 GB RAM, CPU only:

| Step | Time |
|---|---|
| Captions download | seconds |
| Clip picking, `qwen3:4b` | about 1–2 min per 2.5-min part (≈17 min for a 23-min video) |
| Clip picking, `qwen3:8b` | about 2× slower (35 min for a 19-min video) |
| Clip picking, Claude | under a minute |
| Rendering at 1080p | about 5–6 min per short |
| Whisper (`small`) | 10–20 min per hour of audio (a couple of minutes with an NVIDIA GPU, detected automatically) |

**Memory matters.** `qwen3:4b` uses about 3.5 GB of RAM while it runs, and `qwen3:8b` about 6 GB. If the computer runs out of memory, Linux kills Ollama mid-job and the run fails with *"Can't reach Ollama"*. To avoid it:
- use `qwen3:4b` on 16 GB machines
- don't run other AI apps that use Ollama at the same time
- close heavy browser tabs while a job runs

Keep the web app running in its own terminal (or as a service, see below). If the server process is stopped, any running job stops with it and shows as *interrupted*; start it again from the web app.

## Autopilot: posts while you sleep

`autopilot.py` runs the whole thing hands-free. On every run it:

1. Checks the channels you list for new long videos (it only looks; nothing is downloaded yet).
2. Clips each new video into shorts. Only the chosen seconds are downloaded.
3. Uploads the shorts to your channel and schedules each one into the next posting slot (default 12:00, 17:00 and 20:00 IST). Shorts beyond today's slots queue up for the following days.

Each description gets a credit line naming the original creator, with a link to the full video.

On its first run, it only clips each channel's newest video instead of the whole back-catalogue. Videos shorter than 4 minutes are skipped, since they're already shorts.

### 1. Set up config

Copy `config.example.json` to `config.json` and put in your channels. You can write them as `@handle` or as a full channel URL.

In the same file you can also change:
- `model`: `ollama:qwen3:4b` (free, default) or a Claude model
- the posting times and timezone
- how many clips to make per video
- the output resolution

### 2. Auto-posting setup (one time, about 10 minutes)

1. Go to https://console.cloud.google.com and create a project. This is free and doesn't need a billing account.
2. Open **APIs & Services → Library**, search for **YouTube Data API v3**, and click **Enable**.
3. Open **Google Auth Platform** (the OAuth consent screen):
   - Choose **External** and fill in an app name and your email.
   - Under **Audience → Test users**, add the Gmail address of **every** account you'll sign in with. An account that isn't listed gets *Error 403: access_denied*.
   - (Optional) Click **Publish app** to switch to *In production*. In *Testing* mode, sign-ins expire every 7 days. If *Publish app* is greyed out, first fill in **Branding** (app name, support email, developer email) and save. After publishing, Google warns that the app isn't verified when you sign in. Click *Advanced → Go to (your app)*; it's your own app.
4. Open **Credentials → Create credentials → OAuth client ID** and choose type **Desktop app**. Download the JSON file.
5. In the web app, click **Sign in with Google** and give it the JSON file (or save it as `client_secret.json` in this folder). Sign in with your channel's account.
   - Command line instead: `python uploader.py output --max 1` opens your browser once to sign in.

Sign-ins are saved per channel in `accounts/<channel id>.json`.

### ⚠️ Uploads stay private until Google audits your project

YouTube keeps videos uploaded through a new, **unaudited** API project **private**, even when they have a scheduled time.

The autopilot still does all the work: each video is uploaded with its title, description, hashtags and credit line, and scheduled. Until your project passes the audit, you publish them yourself:

- Go to **YouTube Studio → Content**.
- Tick the videos, then choose **Edit → Visibility → Public**. This takes a few seconds for a whole batch.

To make scheduling fully automatic, apply for the **YouTube API Services audit** (search "YouTube API Services audit and quota extension form"). Google doesn't publish a turnaround time for it.

### 3. Run it on a schedule

**Simplest:** leave a terminal open with

```
python autopilot.py --loop 3
```

It checks every 3 hours. Other useful commands:
- `python autopilot.py --no-upload` clips without uploading.
- `python uploader.py library` uploads anything that's queued (`--no-schedule` for private drafts, `--times "12:00,17:00"` to override the posting times).

Everything is logged to `autopilot.log`.

**Linux, with systemd (recommended):** create `~/.config/systemd/user/viral-autopilot.service`:

```ini
[Unit]
Description=Viral Clipper autopilot
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/viral-clipper
ExecStart=/path/to/viral-clipper/.venv/bin/python autopilot.py
```

and `~/.config/systemd/user/viral-autopilot.timer`:

```ini
[Unit]
Description=Run Viral Clipper autopilot every 3 hours

[Timer]
OnCalendar=*-*-* 00/3:00:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
```

Then run `systemctl --user enable --now viral-autopilot.timer`. Ollama must be running too (`ollama serve`, or its own user service).

**Windows, with Task Scheduler:**

1. Open Task Scheduler and choose **Create Basic Task → Daily**.
2. Under **Triggers**, open the task's properties and set **Repeat task every 3 hours**.
3. For **Action**, choose **Start a program**:
   - Program: `python`
   - Arguments: `autopilot.py`
   - Start in: this folder

**Mac/Linux, with cron:**

```
0 */3 * * * cd /path/to/viral-clipper && .venv/bin/python autopilot.py
```

### Before you point it at big channels

Re-uploading other creators' videos without permission gets Content ID claims and copyright strikes, and 3 strikes deletes a channel. It also triggers YouTube's "reused content" rule, which blocks monetization.

Clip creators who allow it. Many big creators run official **clipping programs** that give permission, and some even pay per view. The credit line helps, but on its own it isn't permission.

## Tips

- **Want different clips?** Delete `output/work/local_clips_*.json` (or `claude_clips_*.json`) and run again. The transcript stays cached, so the second run only repeats the clip picking and rendering.
- **Check the text**: local models sometimes invent details in titles and descriptions. Give them a quick read before posting.
- **Caption accuracy**: YouTube's auto-captions are good for English but can be weaker for Hindi/Hinglish. If words look wrong, re-run with `--whisper`.
- **Editing captions**: each short's caption file is in `output/work/` as `.ass`. You can fix a word there and re-render, or import the file into CapCut or Premiere.
- **Costs**: the local model is free. With Claude, it's one API call per video; a 1-hour transcript costs a few cents with Sonnet.
- Only clip videos you own or have permission to reuse.

## Private files

These stay on your computer and are excluded by `.gitignore`: `client_secret.json`, `accounts/`, `token.json`, `config.json`, `upload_state.json`, `runs/`, `output/`, `library/` and logs.

## Ideas to extend it

- Add dynamic face tracking that follows the speaker as they move, instead of a fixed crop per clip.
- Add a split-screen layout for 2-person podcasts.
- Add background music or emoji overlays on keywords.
