#!/usr/bin/env bash
# Start the Viral Clipper web UI (and the free local AI) and open it in the browser.
cd "$(dirname "$0")"
curl -s -o /dev/null http://127.0.0.1:11434/ || (nohup ollama serve >/dev/null 2>&1 &)
(sleep 2; xdg-open http://127.0.0.1:8000 >/dev/null 2>&1) &
exec .venv/bin/python webui.py --port 8000
