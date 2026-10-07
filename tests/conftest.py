import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracker import config  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(config, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tracker.db")
    monkeypatch.setattr(config, "EARLIEST_DATE", "")  # tests that need a cutoff set their own
    config.ensure_dirs()
    return tmp_path


_counter = [0]


def build_eml(sender, subject, body, day, msgid=None, reply_to=None, html=False):
    """Return (message_id, raw bytes) of one email. day is 'YYYY-MM-DD'."""
    _counter[0] += 1
    msgid = msgid or f"m{_counter[0]}@test"
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "Tarik <tarik@example.org>"
    m["Subject"] = subject
    m["Date"] = datetime.fromisoformat(day + "T10:00:00").replace(tzinfo=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    m["Message-ID"] = f"<{msgid}>"
    if reply_to:
        m["In-Reply-To"] = f"<{reply_to}>"
        m["References"] = f"<{reply_to}>"
    if html:
        m.set_content(body, subtype="html")  # HTML only, like many ATS mails
    else:
        m.set_content(body, charset="utf-8")
    return msgid, bytes(m)


def make_eml(sender, subject, body, day, msgid=None, reply_to=None, html=False, name=None):
    """Write one .eml into the inbox folder. Returns the Message-ID."""
    msgid, raw = build_eml(sender, subject, body, day, msgid, reply_to, html)
    (config.INBOX / (name or f"{msgid}.eml")).write_bytes(raw)
    return msgid
