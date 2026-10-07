"""Read .eml and .mbox files into plain dicts. Standard library only."""
import hashlib
import mailbox
import re
from datetime import datetime
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config


class _Text(HTMLParser):
    """Minimal HTML to text. Drops script/style, keeps line breaks."""
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return "".join(p.parts)


def _clean(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # broken charset declarations happen in real mail
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    return _clean(text)


def _ids(*headers) -> str:
    found = []
    for h in headers:
        if h:
            found += re.findall(r"<([^<>]+)>", str(h))
    return " ".join(dict.fromkeys(found))


def parse_message(msg, source_file: str = "") -> dict:
    tz = ZoneInfo(config.LOCAL_TZ)
    try:
        dt = parsedate_to_datetime(str(msg["Date"]))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt = dt.astimezone(tz)
    except Exception:
        dt = datetime.now(tz)
    name, addr = parseaddr(str(msg.get("From", "")))
    subject = str(msg.get("Subject", "") or "")
    mid = str(msg.get("Message-ID", "") or "").strip().strip("<>").strip()
    if not mid:  # some exports drop the header; build a stable stand-in
        mid = "gen-" + hashlib.sha1(f"{addr}|{dt.isoformat()}|{subject}".encode()).hexdigest()
    return {
        "message_id": mid,
        "refs": _ids(msg.get("In-Reply-To"), msg.get("References")),
        "from_name": name,
        "from_addr": addr.lower(),
        "subject": subject,
        "sent_date": dt.date().isoformat(),
        "sent_at": dt.isoformat(),
        "body": _body(msg),
        "source_file": source_file,
    }


def parse_file(path: Path) -> list[dict]:
    """One .eml gives one message, one .mbox gives many."""
    out = []
    if path.suffix.lower() == ".mbox":
        for m in mailbox.mbox(str(path)):
            msg = message_from_bytes(m.as_bytes(), policy=policy.default)
            out.append(parse_message(msg, path.name))
    else:
        msg = message_from_bytes(path.read_bytes(), policy=policy.default)
        out.append(parse_message(msg, path.name))
    return out


def find_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".eml", ".mbox"))
