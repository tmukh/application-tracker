"""Run the dashboard on made-up data, without touching any mail server or model. For screenshots and demos.

    TRACKER_DATA=/tmp/demo python scripts/demo/seed.py
    TRACKER_DATA=/tmp/demo python scripts/demo/serve_demo.py 5077
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("TRACKER_ENV_FILE", "/nonexistent")
from tracker import config, service  # noqa: E402
from tracker.app import app  # noqa: E402

config.IMAP_HOST, config.IMAP_USER, config.IMAP_PASSWORD = "127.0.0.1", "you@example.org", "demo"  # looks connected
config.IMAP_MAILBOX = "Labels/Applications"
config.OLLAMA_MODEL = "qwen2.5:7b"
service.STATE.update(last_cycle=datetime.now().strftime("%Y-%m-%d %H:%M"), error="")
service.start = lambda: None  # no background worker: nothing to fetch or classify

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 5077)
