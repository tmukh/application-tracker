"""Settings. Everything can be overridden with environment variables or a .env file."""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """Tiny .env reader so Windows users need no `set` commands. Real environment variables win."""
    path = Path(os.environ.get("TRACKER_ENV_FILE", BASE / ".env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file()

DATA = Path(os.environ.get("TRACKER_DATA", BASE / "data"))
INBOX = DATA / "inbox"          # drop .eml / .mbox files here (or use the upload box)
PROCESSED = DATA / "processed"  # files move here after they were read
DB_PATH = DATA / "tracker.db"

# --- the language model (Ollama) -------------------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Empty = use the first model that `ollama list` shows. Set e.g. OLLAMA_MODEL=qwen2.5:7b to pin one.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
# "ollama" (LLM + keyword cross-check) or "keywords" (no LLM, works offline, less accurate)
CLASSIFIER = os.environ.get("TRACKER_CLASSIFIER", "ollama")
LLM_TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT_S", "180"))
MAX_BODY_CHARS = 6000           # how much of each email the model sees

# --- mail source: IMAP, normally Proton Mail Bridge --------------------------------------------
IMAP_HOST = os.environ.get("IMAP_HOST", "")            # empty = IMAP switched off (files only)
IMAP_PORT = int(os.environ.get("IMAP_PORT", "1143"))   # Bridge default for IMAP
IMAP_SECURITY = os.environ.get("IMAP_SECURITY", "starttls")  # starttls (Bridge) | ssl | none (loopback only)
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")    # the password BRIDGE shows, not your Proton password
IMAP_MAILBOX = os.environ.get("IMAP_MAILBOX", "INBOX")
IMAP_CAFILE = os.environ.get("IMAP_CAFILE", "")        # optional: Bridge's exported certificate, pins it
# Hard cutoff: mail sent before this date is never stored. Empty means no cutoff. Format YYYY-MM-DD.
EARLIEST_DATE = os.environ.get("EARLIEST_DATE", "2026-01-01").strip()
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))

LOCAL_TZ = "Europe/Berlin"
HOST = os.environ.get("TRACKER_HOST", "127.0.0.1")   # local only; never expose this to the network
PORT = int(os.environ.get("TRACKER_PORT", "5055"))


def ensure_dirs() -> None:
    for p in (DATA, INBOX, PROCESSED):
        p.mkdir(parents=True, exist_ok=True)
