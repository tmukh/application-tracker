"""IMAP polling and the worker, tested against a fake imaplib so no mail server is needed."""
import imaplib
import re
import ssl

import pytest
import requests

from conftest import build_eml
from test_pipeline import _Fake, fake_ollama  # noqa: F401  (fixture: a fake Ollama HTTP server)
from tracker import config, db, imap_source, pipeline, service


class FakeServer:
    """One mailbox with UIDs. Records every command so tests can prove the app only reads."""
    validity = 7
    messages = {}          # uid -> raw bytes
    log = []
    readonly_select = None

    @classmethod
    def reset(cls):
        cls.validity, cls.messages, cls.log, cls.readonly_select = 7, {}, [], None


class FakeIMAP4:
    error = imaplib.IMAP4.error  # captured before monkeypatch replaces the class

    def __init__(self, host, port, timeout=None):
        FakeServer.log.append(("connect", host, port))

    def starttls(self, ssl_context=None):
        assert isinstance(ssl_context, ssl.SSLContext)
        FakeServer.log.append(("starttls",))

    def login(self, user, password):
        if password != "bridge-pass":
            raise FakeIMAP4.error("AUTHENTICATIONFAILED")
        FakeServer.log.append(("login", user))

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasChildren) "/" "Labels"', b'(\\HasNoChildren) "/" "Labels/Bewerbung"',
                      b'(\\HasNoChildren) "/" "Labels/Mit Leerzeichen"']

    def select(self, mailbox, readonly=False):
        FakeServer.readonly_select = readonly
        FakeServer.log.append(("select", mailbox))
        return ("OK", [b"1"]) if mailbox.strip('"') == "Labels/Bewerbung" else ("NO", [b"nope"])

    def response(self, code):
        return code, [str(FakeServer.validity).encode()]

    def uid(self, command, *args):
        FakeServer.log.append(("uid", command) + args)
        if command == "SEARCH":
            low = int(re.search(r"(\d+):\*", args[-1]).group(1))
            hits = [u for u in FakeServer.messages if u >= low]
            if not hits and FakeServer.messages:
                hits = [max(FakeServer.messages)]  # the real-world quirk: N:* always returns the newest message
            return "OK", [" ".join(map(str, sorted(hits))).encode()]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(f"{uid} (BODY[] {{1}}".encode(), FakeServer.messages[uid]), b")"]
        raise AssertionError(f"unexpected command {command}")

    def logout(self):
        FakeServer.log.append(("logout",))


@pytest.fixture
def imap(monkeypatch):
    FakeServer.reset()
    monkeypatch.setattr(imap_source.imaplib, "IMAP4", FakeIMAP4)
    for k, v in dict(IMAP_HOST="127.0.0.1", IMAP_PORT=1143, IMAP_SECURITY="starttls", IMAP_USER="me@proton.me",
                     IMAP_PASSWORD="bridge-pass", IMAP_MAILBOX="Labels/Bewerbung", IMAP_CAFILE="", CLASSIFIER="keywords").items():
        monkeypatch.setattr(config, k, v)
    return FakeServer


def add(server, uid, *args, **kw):
    server.messages[uid] = build_eml(*args, **kw)[1]


def test_first_poll_reads_everything_then_only_new_mail(imap):
    add(imap, 1, "N26 <jobs@n26.com>", "Your application to N26", "We have received your application for Backend Engineer.", "2026-09-07", msgid="a")
    add(imap, 2, "N26 <jobs@n26.com>", "Update", "Unfortunately, we have decided to move forward with other candidates.", "2026-09-09", reply_to="a")
    conn = db.connect()
    assert pipeline.poll_imap(conn) == 2
    assert pipeline.poll_imap(conn) == 0  # nothing new, and the N:* quirk did not re-import UID 2
    add(imap, 3, "Qonto <jobs@qonto.com>", "Your application to Qonto", "Thanks for applying. We have received your application.", "2026-09-20", msgid="c")
    assert pipeline.poll_imap(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 3


def test_app_only_reads_and_never_changes_the_mailbox(imap):
    add(imap, 1, "N26 <jobs@n26.com>", "Your application to N26", "We have received your application.", "2026-09-07")
    pipeline.poll_imap(db.connect())
    assert imap.readonly_select is True
    for entry in imap.log:
        if entry[0] == "uid" and entry[1] == "FETCH":
            assert entry[3] == "(BODY.PEEK[])"  # PEEK: does not mark mail as read
        assert entry[0] in ("connect", "starttls", "login", "select", "uid", "logout")
        assert entry[:2] != ("uid", "STORE") and entry[:2] != ("uid", "COPY") and entry[:2] != ("uid", "EXPUNGE")
    assert ("starttls",) in imap.log


def test_uidvalidity_change_restarts_without_duplicates(imap):
    add(imap, 1, "N26 <jobs@n26.com>", "Your application to N26", "We have received your application.", "2026-09-07", msgid="a")
    conn = db.connect()
    assert pipeline.poll_imap(conn) == 1
    imap.validity = 8  # the server renumbered everything
    assert pipeline.poll_imap(conn) == 0  # re-read from the start, but Message-ID dedupe keeps one copy
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1


def test_wrong_password_and_missing_mailbox_have_clear_errors(imap, monkeypatch):
    monkeypatch.setattr(config, "IMAP_PASSWORD", "wrong")
    with pytest.raises(imap_source.ImapError, match="not your Proton password"):
        pipeline.poll_imap(db.connect())
    monkeypatch.setattr(config, "IMAP_PASSWORD", "bridge-pass")
    monkeypatch.setattr(config, "IMAP_MAILBOX", "Labels/Nope")
    with pytest.raises(imap_source.ImapError, match="imap-list"):
        pipeline.poll_imap(db.connect())


def test_list_mailboxes_parses_names_with_spaces(imap):
    with imap_source.Session() as s:
        assert "Labels/Bewerbung" in s.list_mailboxes() and "Labels/Mit Leerzeichen" in s.list_mailboxes()


def test_plaintext_to_remote_host_is_refused(imap, monkeypatch):
    monkeypatch.setattr(config, "IMAP_HOST", "mail.example.com")
    monkeypatch.setattr(config, "IMAP_SECURITY", "none")
    with pytest.raises(imap_source.ImapError, match="unencrypted"):
        pipeline.poll_imap(db.connect())


def test_imap_off_when_host_is_empty(imap, monkeypatch):
    monkeypatch.setattr(config, "IMAP_HOST", "")
    assert pipeline.poll_imap(db.connect()) == 0


# ------------------------------------------------- worker and the sleeping desktop
def test_unreachable_model_keeps_mail_queued_then_catches_up(imap, fake_ollama, monkeypatch):
    add(imap, 1, "N26 <jobs@n26.com>", "Your application to N26", "We have received your application for Backend Engineer.", "2026-09-07", msgid="a")
    monkeypatch.setattr(config, "CLASSIFIER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_URL", "http://127.0.0.1:9")  # desktop asleep
    res = service.cycle()
    assert res["new_emails"] == 1 and res["classified"] == 0 and res["waiting"] == 1
    assert "stays queued" in service.STATE["error"] and service.STATE["waiting"] == 1
    conn = db.connect()
    assert conn.execute("SELECT event_type FROM emails").fetchone()[0] == "unclassified"  # NOT downgraded to keywords
    # desktop wakes up
    monkeypatch.setattr(config, "OLLAMA_URL", fake_ollama)
    _Fake.reply = {"event_type": "application_confirmation", "company": "N26", "role": "Backend Engineer", "interview_at": "", "summary": "ok"}
    res = service.cycle()
    assert res["classified"] == 1 and res["waiting"] == 0 and service.STATE["error"] == ""
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "applied"


def test_model_dying_mid_batch_keeps_finished_work(imap, fake_ollama, monkeypatch):
    for i in range(3):
        add(imap, i + 1, f"Firma{i} <jobs@firma{i}.de>", f"Your application to Firma{i}", "We have received your application.", f"2026-09-0{i + 1}", msgid=f"x{i}")
    calls = {"n": 0}
    real_post = requests.post

    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise requests.ConnectionError("desktop went to sleep")
        return real_post(url, **kw)
    monkeypatch.setattr(requests, "post", flaky)
    monkeypatch.setattr(config, "CLASSIFIER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_URL", fake_ollama)
    _Fake.reply = {"event_type": "application_confirmation", "company": "Firma0", "role": "", "interview_at": "", "summary": "ok"}
    res = service.cycle()
    assert res["classified"] == 1 and res["waiting"] == 2  # the first email is final, two wait
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM emails WHERE event_type='unclassified'").fetchone()[0] == 2


def test_health_endpoint_reports_state(imap, monkeypatch):
    from tracker.app import app
    monkeypatch.setattr(config, "IMAP_HOST", "")
    service.cycle()
    r = app.test_client().get("/health")
    assert r.status_code in (200, 503) and "last_cycle" in r.get_json()


def test_backup_is_a_usable_copy(imap, tmp_path):
    add(imap, 1, "N26 <jobs@n26.com>", "Your application to N26", "We have received your application.", "2026-09-07")
    pipeline.sync(clf=None)
    target = db.backup(tmp_path / "bk", keep=2)
    import sqlite3
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
