"""Read-only IMAP access. Built for Proton Mail Bridge (127.0.0.1:1143, STARTTLS) but works with any IMAP server.

The app never changes your mailbox: the folder is opened read-only and messages are fetched with BODY.PEEK,
so nothing is marked as read, moved or deleted.
"""
import imaplib
import re
import ssl
from email import message_from_bytes, policy

from . import config, ingest

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ImapError(RuntimeError):
    pass


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _context(host: str) -> ssl.SSLContext:
    if config.IMAP_CAFILE:
        # Pinned certificate: only the certificate you exported from Bridge is trusted.
        ctx = ssl.create_default_context(cafile=config.IMAP_CAFILE)
        ctx.check_hostname = False  # Bridge's certificate is not issued for a hostname
        return ctx
    if host in LOOPBACK:
        # Bridge uses a self-signed certificate. The traffic never leaves this machine, so skipping
        # verification is acceptable here; pin the certificate with IMAP_CAFILE if you want more.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()  # a real server: normal verification


class Session:
    def __init__(self):
        self.c = None
        self.mailbox = ""

    def __enter__(self):
        self.c = self._connect()
        return self

    def __exit__(self, *exc):
        try:
            self.c.logout()
        except Exception:
            pass

    def _connect(self):
        host, port, sec = config.IMAP_HOST, config.IMAP_PORT, config.IMAP_SECURITY.lower()
        if not host:
            raise ImapError("IMAP_HOST is not set")
        if sec == "none" and host not in LOOPBACK:
            raise ImapError("Refusing unencrypted IMAP to a non-local host. Use IMAP_SECURITY=starttls or ssl.")
        ctx = _context(host)
        try:
            if sec == "ssl":
                c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
            else:
                c = imaplib.IMAP4(host, port, timeout=30)
                if sec == "starttls":
                    c.starttls(ssl_context=ctx)
            c.login(config.IMAP_USER, config.IMAP_PASSWORD)
            return c
        except imaplib.IMAP4.error as e:
            raise ImapError(f"IMAP login failed: {e}. With Bridge, use the password Bridge shows, not your Proton password.")
        except (OSError, ssl.SSLError) as e:
            raise ImapError(f"Cannot reach the IMAP server at {host}:{port}. Is Proton Bridge running and logged in? ({e})")

    def list_mailboxes(self) -> list[str]:
        typ, data = self.c.list()
        names = []
        for raw in data or []:
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<f>[^)]*)\)\s+(?:"(?P<sep>[^"]*)"|NIL)\s+(?P<n>.+)$', line)
            if m:
                n = m.group("n").strip()
                if n.startswith('"') and n.endswith('"'):
                    n = n[1:-1].replace('\\"', '"')
                names.append(n)
        return names

    def select(self, mailbox: str) -> int:
        """Open read-only. Returns UIDVALIDITY: if it changes, the UIDs were renumbered."""
        typ, _ = self.c.select(_quote(mailbox), readonly=True)
        if typ != "OK":
            raise ImapError(f"Mailbox '{mailbox}' not found. Run `python -m tracker imap-list` to see the exact names.")
        self.mailbox = mailbox
        _, v = self.c.response("UIDVALIDITY")
        return int(v[0]) if v and v[0] else 0

    def iter_new(self, last_uid: int):
        """Yield (uid, parsed message dict) for every message with a UID above last_uid, oldest first."""
        _, data = self.c.uid("SEARCH", "UID", f"{last_uid + 1}:*")
        # IMAP quirk: "N:*" always returns the newest message even if its UID is below N, so filter again.
        uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last_uid)
        for uid in uids:
            _, md = self.c.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in md if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = message_from_bytes(raw, policy=policy.default)
            yield uid, ingest.parse_message(msg, f"imap:{self.mailbox}:{uid}")
