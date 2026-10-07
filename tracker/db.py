"""SQLite storage. One file, no server."""
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY,
    company       TEXT NOT NULL,
    role          TEXT DEFAULT '',
    applied_date  TEXT,                       -- YYYY-MM-DD
    status        TEXT DEFAULT 'applied',     -- applied/assessment/interview/offer/rejected/withdrawn
    status_locked INTEGER DEFAULT 0,          -- 1 = you set it by hand, emails no longer change it
    category      TEXT DEFAULT '',
    domains       TEXT DEFAULT '',            -- sender domains seen for this company (space separated)
    notes         TEXT DEFAULT '',
    source        TEXT DEFAULT 'email',       -- email / import / manual
    last_activity TEXT,
    interview_at  TEXT DEFAULT '',
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS emails (
    id            INTEGER PRIMARY KEY,
    message_id    TEXT UNIQUE NOT NULL,
    refs          TEXT DEFAULT '',            -- Message-IDs this mail replies to, space separated
    from_name     TEXT DEFAULT '',
    from_addr     TEXT DEFAULT '',
    subject       TEXT DEFAULT '',
    sent_date     TEXT,                       -- YYYY-MM-DD in Berlin time
    sent_at       TEXT,                       -- full ISO timestamp, used for ordering
    body          TEXT DEFAULT '',
    event_type    TEXT DEFAULT 'unclassified',
    company       TEXT DEFAULT '',
    role          TEXT DEFAULT '',
    interview_at  TEXT DEFAULT '',
    summary       TEXT DEFAULT '',
    classifier    TEXT DEFAULT '',            -- which classifier produced the result
    needs_review  INTEGER DEFAULT 0,
    review_reason TEXT DEFAULT '',
    application_id INTEGER REFERENCES applications(id),
    source_file   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_emails_app ON emails(application_id);
CREATE INDEX IF NOT EXISTS idx_emails_review ON emails(needs_review);
"""

STATUSES = ["applied", "assessment", "interview", "offer", "rejected", "withdrawn"]


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def backup(dest_dir, keep: int = 14) -> Path:
    """Consistent copy of the database (safe while the service runs). Keeps the newest `keep` copies."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"tracker-{datetime.now():%Y%m%d-%H%M%S}.db"
    src, dst = connect(), sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    for old in sorted(dest.glob("tracker-*.db"))[:-keep]:
        old.unlink()
    return target
