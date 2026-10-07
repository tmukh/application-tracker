from conftest import build_eml, make_eml
from tracker import config, db, pipeline


def _emails(conn):
    return [r["subject"] for r in conn.execute("SELECT subject FROM emails ORDER BY sent_at")]


def test_mail_before_the_cutoff_is_never_stored(monkeypatch):
    monkeypatch.setattr(config, "EARLIEST_DATE", "2026-01-01")
    make_eml("Old Co <jobs@oldco.io>", "Your application to OldCo", "We have received your application.", "2025-11-30", msgid="o1")
    make_eml("New Co <jobs@newco.io>", "Your application to NewCo", "We have received your application.", "2026-01-01", msgid="n1")
    res = pipeline.sync(clf=None)
    conn = db.connect()
    assert res["new_emails"] == 1 and _emails(conn) == ["Your application to NewCo"]
    assert [a["company"] for a in conn.execute("SELECT company FROM applications")] == ["NewCo"]


def test_prune_dry_run_then_apply_keeps_live_old_applications(monkeypatch):
    conn = db.connect()
    # three applications: dead (all 2025), live (applied 2025, replied 2026), and one without any date
    conn.execute("INSERT INTO applications (company, role, applied_date, last_activity, status, source) VALUES ('Dead', 'x', '2025-03-01', '2025-04-01', 'rejected', 'import')")
    conn.execute("INSERT INTO applications (company, role, applied_date, last_activity, status, source) VALUES ('Live', 'y', '2025-12-01', '2025-12-01', 'applied', 'email')")
    conn.execute("INSERT INTO applications (company, role, status, source) VALUES ('Undated', 'z', 'applied', 'import')")
    ids = {r["company"]: r["id"] for r in conn.execute("SELECT id, company FROM applications")}
    for mid, day, app in [("e1", "2025-04-01", "Dead"), ("e2", "2025-12-01", "Live"), ("e3", "2026-02-10", "Live")]:
        conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,application_id,classifier) VALUES (?,?,?,?,?,?,?,?,?)",
                     (mid, "a@b.de", mid, day, day + "T10:00:00", "b", "other", ids[app], "ollama"))
    conn.commit()
    monkeypatch.setattr(config, "EARLIEST_DATE", "2026-01-01")
    assert pipeline.prune(conn) == {"emails": 2, "applications": 1, "applied": False}
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 3  # dry run changed nothing
    assert pipeline.prune(conn, apply=True)["applications"] == 1
    assert sorted(r["company"] for r in conn.execute("SELECT company FROM applications")) == ["Live", "Undated"]
    assert _emails(conn) == ["e3"]


def test_lowering_the_cutoff_rereads_the_mailbox(monkeypatch):
    from test_imap import FakeServer, FakeIMAP4, add
    from tracker import imap_source
    FakeServer.reset()
    monkeypatch.setattr(imap_source.imaplib, "IMAP4", FakeIMAP4)
    for k, v in dict(IMAP_HOST="127.0.0.1", IMAP_PORT=1143, IMAP_SECURITY="starttls", IMAP_USER="me@proton.me",
                     IMAP_PASSWORD="bridge-pass", IMAP_MAILBOX="Labels/Bewerbung", IMAP_CAFILE="").items():
        monkeypatch.setattr(config, k, v)
    add(FakeServer, 1, "Old <jobs@old.io>", "Your application to Old", "We have received your application.", "2025-06-01", msgid="a")
    add(FakeServer, 2, "New <jobs@new.io>", "Your application to New", "We have received your application.", "2026-03-01", msgid="b")
    conn = db.connect()
    monkeypatch.setattr(config, "EARLIEST_DATE", "2026-01-01")
    assert pipeline.poll_imap(conn) == 1
    assert pipeline.poll_imap(conn) == 0
    monkeypatch.setattr(config, "EARLIEST_DATE", "2026-06-01")  # raising: nothing to re-read
    assert pipeline.poll_imap(conn) == 0
    monkeypatch.setattr(config, "EARLIEST_DATE", "2025-01-01")  # lowering: the old mail qualifies now
    assert pipeline.poll_imap(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 2
