import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import make_eml
from tracker import config, db, linker, pipeline
from tracker.classify import KeywordClassifier, OllamaClassifier


def run_sync():
    return pipeline.sync(clf=None)  # keyword classifier only


def apps(conn):
    return conn.execute("SELECT * FROM applications ORDER BY id").fetchall()


# ------------------------------------------------------------ status logic
def test_derive_status_order_and_reopen():
    d = linker.derive_status
    assert d(["application_confirmation"], "x") == "applied"
    assert d(["application_confirmation", "interview_invite"], "x") == "interview"
    assert d(["application_confirmation", "interview_invite", "rejection"], "x") == "rejected"
    assert d(["application_confirmation", "rejection", "interview_invite"], "x") == "interview"  # reopened
    assert d(["interview_invite", "assessment"], "x") == "interview"  # never goes backwards
    assert d(["follow_up", "other"], "keep") == "keep"


def test_company_and_role_matching():
    assert linker.same_company("Bundesdruckerei GmbH", "Bundesdruckerei-Gruppe")
    assert not linker.same_company("SAP", "SumUp")
    assert linker.roles_similar("DevOps Engineer (m/w/d)", "DevOps Engineer")
    assert not linker.roles_similar("Cloud Engineer", "Cloud Software Engineer", strict=True)
    assert linker.roles_similar("Cloud Engineer", "Cloud Software Engineer")  # loose match for replies


# ---------------------------------------------------------- end to end
def test_full_lifecycle_german_and_english():
    make_eml("Bundesdruckerei Karriere <noreply@bundesdruckerei.de>", "Ihre Bewerbung bei der Bundesdruckerei",
             "Vielen Dank für Ihre Bewerbung als DevOps Engineer. Ihre Bewerbung ist bei uns eingegangen.", "2026-09-01", msgid="bdr1")
    make_eml("Anna Fischer <a.fischer@bundesdruckerei.de>", "Einladung zum Vorstellungsgespräch",
             "Wir laden Sie herzlich zu einem Gespräch ein. Terminvorschlag: 09.10.2026, 13:15 Uhr.", "2026-10-02", reply_to="bdr1")
    make_eml("N26 Recruiting <jobs@n26.com>", "Your application to N26",
             "Thank you for applying to N26. We have received your application for Backend Engineer.", "2026-09-07", msgid="n26a")
    make_eml("N26 Recruiting <jobs@n26.com>", "Update on your application",
             "Unfortunately, we have decided to move forward with other candidates.", "2026-09-09", reply_to="n26a")
    make_eml("Job Alerts <alerts@stepstone.de>", "Neue Stellen für Ihre Suche",
             "Neue Bewerbung möglich! unsubscribe here", "2026-09-10")
    res = run_sync()
    assert res["new_emails"] == 5 and res["classified"] == 5
    conn = db.connect()
    got = {a["company"]: a for a in apps(conn)}
    assert len(got) == 2
    bdr = next(a for c, a in got.items() if "undesdruckerei" in c)
    n26 = next(a for c, a in got.items() if "N26" in c or "n26" in c.lower())
    assert bdr["status"] == "interview" and bdr["applied_date"] == "2026-09-01"
    assert n26["status"] == "rejected" and n26["applied_date"] == "2026-09-07"
    assert conn.execute("SELECT COUNT(*) FROM emails WHERE application_id=?", (bdr["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT event_type FROM emails WHERE subject LIKE 'Neue Stellen%'").fetchone()[0] == "not_job"


def test_same_company_two_roles_stay_separate_and_ambiguous_reply_is_flagged():
    for i, role in enumerate(["Cloud Engineer", "Cloud Software Engineer"]):
        make_eml("Deloitte <no-reply@jobs.deloitte.de>", f"Ihre Bewerbung bei Deloitte: {role}",
                 f"Vielen Dank für Ihre Bewerbung als {role}. Ihre Bewerbung ist eingegangen.", "2026-08-30", msgid=f"dl{i}")
    pipeline.sync(clf=None)
    conn = db.connect()
    assert len(apps(conn)) == 2
    make_eml("Deloitte <no-reply@jobs.deloitte.de>", "Ihre Bewerbung bei Deloitte",
             "Leider müssen wir Ihnen mitteilen: Absage. Wir haben uns für andere Kandidaten entschieden.", "2026-08-31")
    pipeline.sync(clf=None)
    row = conn.execute("SELECT * FROM emails WHERE event_type='rejection'").fetchone()
    assert row["needs_review"] == 1 and "applications at this company" in row["review_reason"]


def test_reingest_is_idempotent():
    make_eml("Zenjob <hr@zenjob.com>", "Your application to Zenjob", "Thank you for applying. We have received your application.", "2026-09-07", msgid="z1", name="a.eml")
    run_sync()
    make_eml("Zenjob <hr@zenjob.com>", "Your application to Zenjob", "Thank you for applying. We have received your application.", "2026-09-07", msgid="z1", name="b.eml")
    res = run_sync()
    assert res["new_emails"] == 0
    assert len(apps(db.connect())) == 1


def test_html_only_mail_is_read():
    make_eml("Qonto <jobs@qonto.com>", "Your application to Qonto", "<html><body><p>Thanks for applying!</p><script>x()</script><p>We have received your application.</p></body></html>",
             "2026-09-07", html=True)
    run_sync()
    body = db.connect().execute("SELECT body FROM emails").fetchone()[0]
    assert "received your application" in body and "x()" not in body


def test_manual_resolution_overrides_and_locks():
    make_eml("Someone <x@example.org>", "Hello", "Just a note about nothing.", "2026-09-07")
    run_sync()
    conn = db.connect()
    e = conn.execute("SELECT * FROM emails").fetchone()
    pipeline.resolve_email(conn, e["id"], "application_confirmation", "new", "Acme GmbH", "Backend Engineer")
    a = apps(conn)[0]
    assert a["company"] == "Acme GmbH" and a["status"] == "applied"
    assert conn.execute("SELECT needs_review, classifier FROM emails").fetchone()[:] == (0, "manual")


# ------------------------------------------------------- fake Ollama server
class _Fake(BaseHTTPRequestHandler):
    reply = {"event_type": "rejection", "company": "Acme", "role": "SRE", "interview_at": "", "summary": "Declined."}
    raw = None
    default = dict(reply)

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"models": [{"name": "qwen2.5:7b"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._send({"message": {"content": _Fake.raw if _Fake.raw is not None else json.dumps(_Fake.reply)}})


@pytest.fixture
def fake_ollama():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Fake.raw = None
    _Fake.reply = dict(_Fake.default)  # no state leaks between tests
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_ollama_classifier_parses_and_picks_model(fake_ollama):
    clf = OllamaClassifier(url=fake_ollama)
    assert clf.check() == "qwen2.5:7b"
    r = clf.classify({"subject": "x", "body": "y", "from_name": "", "from_addr": "", "sent_date": "2026-09-01"})
    assert (r.event_type, r.company, r.role) == ("rejection", "Acme", "SRE") and not r.error


def test_ollama_garbage_falls_back_and_flags(fake_ollama):
    _Fake.raw = "not json at all"
    clf = OllamaClassifier(url=fake_ollama)
    clf.check()
    make_eml("Acme <jobs@acme.io>", "Your application to Acme", "Thank you for applying. We have received your application.", "2026-09-07")
    pipeline.sync(clf=clf)
    e = db.connect().execute("SELECT * FROM emails").fetchone()
    assert e["event_type"] == "application_confirmation" and e["needs_review"] == 1 and "LLM failed" in e["review_reason"]


def test_model_answer_stands_when_keywords_disagree(fake_ollama):
    # The keyword rules are blunt; a disagreement alone must not fill your Review page.
    _Fake.reply = {"event_type": "follow_up", "company": "Acme", "role": "SRE", "interview_at": "", "summary": "x"}
    clf = OllamaClassifier(url=fake_ollama)
    clf.check()
    make_eml("Acme <jobs@acme.io>", "Your application to Acme", "We regret to inform you that we will not be moving forward.", "2026-09-07")
    pipeline.sync(clf=clf)
    e = db.connect().execute("SELECT * FROM emails").fetchone()
    assert e["event_type"] == "follow_up" and e["needs_review"] == 0


def test_noise_is_quiet_but_unplaceable_decisive_mail_is_flagged(fake_ollama):
    clf = OllamaClassifier(url=fake_ollama)
    clf.check()
    _Fake.reply = {"event_type": "other", "company": "", "role": "", "interview_at": "", "summary": "alert"}
    make_eml("GitHub <noreply@github.com>", "A third-party OAuth application was added", "Something happened.", "2026-09-07")
    pipeline.sync(clf=clf)  # the fake model answers per sync, so classify the first mail before changing its reply
    _Fake.reply = {"event_type": "interview_invite", "company": "", "role": "", "interview_at": "", "summary": "invite"}
    make_eml("Someone <x@gmail.com>", "Einladung", "Wir laden Sie ein.", "2026-09-08")
    pipeline.sync(clf=clf)
    rows = {r["subject"]: r for r in db.connect().execute("SELECT * FROM emails")}
    assert rows["A third-party OAuth application was added"]["needs_review"] == 0
    assert rows["Einladung"]["needs_review"] == 1 and rows["Einladung"]["review_reason"] == "no company name found"


def test_rereview_clears_only_stale_flags():
    conn = db.connect()
    def add(i, ev, why, clf="ollama"):
        conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,needs_review,review_reason,classifier) VALUES (?,?,?,?,?,?,?,1,?,?)",
                     (f"r{i}", "a@b.de", "s", "2026-09-01", "2026-09-01T10:00:00", "b", ev, why, clf))
    add(1, "follow_up", "LLM says follow_up, keywords say interview_invite")
    add(2, "other", "could not match to an application")
    add(3, "other", "3 applications at this company, please check the assignment")
    add(4, "rejection", "3 applications at this company, please check the assignment")   # decisive: keep
    add(5, "interview_invite", "no company name found")                                    # decisive: keep
    add(6, "follow_up", "LLM failed (bad json); keyword result used")                       # real problem: keep
    add(7, "follow_up", "LLM says follow_up, keywords say interview_invite", clf="manual")  # your decision: untouched
    conn.commit()
    res = pipeline.rereview(conn)
    assert res == {"cleared": 3, "still_to_review": 4}


def test_ollama_down_gives_readable_error():
    with pytest.raises(RuntimeError, match="Cannot reach Ollama"):
        OllamaClassifier(url="http://127.0.0.1:9").check()


# ----------------------------------------------------------------- web app
def test_web_pages_and_status_lock():
    from tracker.app import app
    make_eml("N26 <jobs@n26.com>", "Your application to N26", "Thank you for applying to N26. We have received your application for Backend Engineer.", "2026-09-07", msgid="w1")
    run_sync()
    c = app.test_client()
    assert b"N26" in c.get("/").data
    assert c.get("/review").status_code == 200
    csv = c.get("/export.csv")
    assert csv.status_code == 200 and b"N26" in csv.data
    aid = apps(db.connect())[0]["id"]
    assert c.get(f"/app/{aid}").status_code == 200
    c.post(f"/app/{aid}/update", data={"company": "N26", "role": "Backend Engineer", "applied_date": "2026-09-07", "status": "withdrawn", "category": "", "notes": ""})
    make_eml("N26 <jobs@n26.com>", "Update", "Unfortunately, we have decided to move forward with other candidates.", "2026-09-09", reply_to="w1")
    run_sync()
    assert apps(db.connect())[0]["status"] == "withdrawn"  # locked: the rejection mail did not override my choice
    c.post(f"/app/{aid}/update", data={"company": "N26", "role": "Backend Engineer", "applied_date": "2026-09-07", "status": "withdrawn", "category": "", "notes": "", "unlock": "1"})
    assert apps(db.connect())[0]["status"] == "rejected"  # unlocked: emails decide again


def test_upload_rejects_other_file_types():
    from tracker.app import app
    import io
    c = app.test_client()
    r = c.post("/upload", data={"files": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data", follow_redirects=True)
    assert b"Only .eml and .mbox" in r.data
    assert not list(config.INBOX.iterdir())


def test_keyword_classifier_company_guess():
    r = KeywordClassifier().classify({"subject": "Ihre Bewerbung bei der Bundesdruckerei", "body": "Ihre Bewerbung ist bei uns eingegangen.", "from_name": "HR", "from_addr": "x@y.de"})
    assert r.event_type == "application_confirmation" and r.company == "Bundesdruckerei"


def test_confirmation_phrase_variant_and_person_sender_use_company_domain():
    # "Bewerbung als ... ist eingegangen" must count as a confirmation
    make_eml("Bundesdruckerei <noreply@bundesdruckerei.de>", "Ihre Bewerbung bei der Bundesdruckerei",
             "Ihre Bewerbung als Site Reliability Engineer ist eingegangen.", "2026-09-01", msgid="bd1")
    # a recruiter writes from a personal display name: the application must not be named after her
    make_eml("Anna Fischer <a.fischer@bundesdruckerei.de>", "Einladung", "Wir laden Sie zu einem Gespräch ein.", "2026-10-02")
    run_sync()
    conn = db.connect()
    rows = apps(conn)
    assert [r["company"] for r in rows] == ["Bundesdruckerei"] and rows[0]["status"] == "interview"
    assert linker.useful_domain("x@personio.de") == "" and linker.useful_domain("x@gmail.com") == ""


# ------------------------------------------------ real-world mix-ups (found on live data)
def test_two_job_titles_at_one_company_are_two_applications():
    # only one application exists at the company, yet an invite for another job title must not be pinned onto it
    make_eml("Bundesdruckerei <notifications@karriere.bdr.de>", "Ihre Bewerbung bei der Bundesdruckerei: DevOps Engineer",
             "Ihre Bewerbung als DevOps Engineer ist eingegangen.", "2026-09-18", msgid="b1")
    run_sync()
    conn = db.connect()
    conn.execute("INSERT INTO emails (message_id,from_name,from_addr,subject,sent_date,sent_at,body,event_type,company,role,interview_at,classifier) "
                 "VALUES ('b2','BDR','notifications@karriere.bdr.de','Einladung SRE','2026-09-23','2026-09-23T10:00:00','Einladung','interview_invite','Bundesdruckerei','Site Reliability Engineer','2026-10-09T13:15:00','test')")
    conn.commit()
    pipeline.link_email(conn, conn.execute("SELECT id FROM emails WHERE message_id='b2'").fetchone()[0])
    roles = sorted(a["role"] for a in apps(conn))
    assert len(roles) == 2 and any("Site Reliability" in r for r in roles)
    sre = next(a for a in apps(conn) if "Site Reliability" in a["role"])
    assert sre["status"] == "interview" and sre["interview_at"] == "2026-10-09T13:15:00"
    assert next(a for a in apps(conn) if "DevOps" in a["role"])["status"] == "applied"  # untouched by the SRE invite


def test_same_recruiter_ties_the_mail_to_the_application_she_wrote_about():
    # Avanade's recruiter writes from an Accenture address; the Accenture application was rejected long before
    conn = db.connect()
    def add(i, who, ev, comp, role, day, app=None):
        conn.execute("INSERT INTO emails (message_id,from_name,from_addr,subject,sent_date,sent_at,body,event_type,company,role,application_id,classifier) VALUES (?,?,?,?,?,?,?,?,?,?,?,'test')",
                     (f"a{i}", who[0], who[1], "s", day, day + "T10:00:00", "b", ev, comp, role, app))
        return conn.execute("SELECT id FROM emails WHERE message_id=?", (f"a{i}",)).fetchone()[0]
    add(1, ("Accenture", "accenture@myworkday.com"), "application_confirmation", "Accenture", "Trainee Data & AI", "2026-09-01")
    pipeline.link_email(conn, 1)
    add(2, ("Accenture", "accenture@myworkday.com"), "rejection", "Accenture", "Trainee Data & AI", "2026-09-07")
    pipeline.link_email(conn, 2)
    add(3, ("Corina", "c.traistaru@accenture.com"), "interview_invite", "Avanade", "DevOps Engineer", "2026-09-25")
    pipeline.link_email(conn, 3)
    add(4, ("Corina", "c.traistaru@accenture.com"), "interview_invite", "Accenture", "", "2026-09-25")  # "Phone call interview"
    pipeline.link_email(conn, 4)
    by = {a["company"]: a for a in apps(conn)}
    assert by["Accenture"]["status"] == "rejected" and by["Avanade"]["status"] == "interview"
    assert conn.execute("SELECT application_id FROM emails WHERE id=4").fetchone()[0] == by["Avanade"]["id"]


def test_status_earned_by_deleted_mail_does_not_linger():
    # Deloitte: applied in 2025, only a 2026 "welcome to the portal" mail remains after the cutoff removed the rest
    conn = db.connect()
    conn.execute("INSERT INTO applications (company, role, applied_date, status, interview_at, source) VALUES ('Deloitte','Trainee','2025-07-14','interview','2025-08-08T09:00:00','email')")
    conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,application_id,classifier) VALUES ('d1','recruiting@deloitte.de','Willkommen','2026-08-29','2026-08-29T10:00:00','b','follow_up',1,'test')")
    conn.commit()
    pipeline.recompute(conn, 1)
    a = apps(conn)[0]
    assert a["status"] == "applied" and a["interview_at"] == ""


def test_interview_time_prefers_a_real_appointment_over_a_bare_date():
    conn = db.connect()
    conn.execute("INSERT INTO applications (company, role, status, source) VALUES ('Acme','x','applied','email')")
    for i, (day, at) in enumerate([("2026-09-20", "2026-10-05T14:00:00+01:00"), ("2026-09-25", "2026-09-25")]):
        conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,interview_at,application_id,classifier) VALUES (?,?,?,?,?,?,'interview_invite',?,1,'test')",
                     (f"i{i}", "x@acme.io", "s", day, day + "T10:00:00", "b", at))
    pipeline.recompute(conn, 1)
    assert apps(conn)[0]["interview_at"] == "2026-10-05T14:00:00+01:00"


def test_relink_all_repairs_old_mix_ups_and_keeps_manual_work():
    conn = db.connect()
    conn.execute("INSERT INTO applications (company, role, status, source, notes) VALUES ('Bundesdruckerei','DevOps Engineer','interview','email','my note')")
    def mail(i, ev, role, day, clf="test", app=1, at=""):
        conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,company,role,interview_at,application_id,classifier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"m{i}", "notifications@karriere.bdr.de", "s", day, day + "T10:00:00", "b", ev, "Bundesdruckerei", role, at, app, clf))
    mail(1, "interview_invite", "DevOps Engineer", "2026-09-28", at="2026-10-13T15:05:00")
    mail(2, "interview_invite", "Site Reliability Engineer", "2026-09-30", at="2026-10-09T13:15:00")  # wrongly pinned on the DevOps application
    mail(3, "follow_up", "Platform Engineer", "2026-10-01", clf="manual")                              # you decided this one: stays
    conn.commit()
    out = pipeline.migrate(conn)
    assert out["emails_moved"] == 1
    rows = {a["role"]: a for a in apps(conn)}
    assert rows["DevOps Engineer"]["interview_at"] == "2026-10-13T15:05:00" and rows["DevOps Engineer"]["notes"] == "my note"
    assert rows["Site Reliability Engineer"]["interview_at"] == "2026-10-09T13:15:00"
    assert conn.execute("SELECT application_id FROM emails WHERE message_id='m3'").fetchone()[0] == 1
    assert pipeline.migrate(conn) == {}  # runs once per rule version


def test_same_person_from_another_address_and_survey_ghosts():
    # Avanade's recruiter books the call from a calendar alias; a feedback survey must not create an application
    conn = db.connect()
    def add(i, addr, ev, comp, role, day, subject="s"):
        conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,company,role,classifier) VALUES (?,?,?,?,?,?,?,?,?,'test')",
                     (f"g{i}", addr, subject, day, day + "T10:00:00", "b", ev, comp, role))
        return conn.execute("SELECT id FROM emails WHERE message_id=?", (f"g{i}",)).fetchone()[0]
    ids = [add(1, "mailer@surveys.accenture.com", "assessment", "Accenture", "Trainee Data & AI", "2026-09-04", "How was your Accenture recruiting experience?"),
           add(2, "accenture@myworkday.com", "rejection", "Accenture", "IT Consultant Business Analyse", "2026-09-07"),
           add(3, "c.a.traistaru@accenture.com", "interview_invite", "Avanade", "DevOps Engineer", "2026-09-25"),
           add(4, "accentureemearecruitingcorinatraistaru@accenture.com", "interview_invite", "Accenture", "", "2026-09-25", "Confirmed: Accenture - Phone Call Interview")]
    conn.commit()
    pipeline.relink_all(conn)
    by = {a["company"] + "|" + a["role"][:6]: a for a in apps(conn)}
    assert sorted(by) == ["Accenture|IT Con", "Avanade|DevOps"]  # no ghost application from the survey
    assert by["Accenture|IT Con"]["status"] == "rejected" and by["Avanade|DevOps"]["status"] == "interview"
    assert conn.execute("SELECT application_id FROM emails WHERE id=?", (ids[3],)).fetchone()[0] == by["Avanade|DevOps"]["id"]
    assert conn.execute("SELECT event_type FROM emails WHERE id=?", (ids[0],)).fetchone()[0] == "other"


def test_employer_spelled_differently_still_matches_when_domain_and_job_agree():
    conn = db.connect()
    conn.execute("INSERT INTO applications (company, role, applied_date, status, source, domains) VALUES ('solute GmbH oder Checkout Charlie','Software Developer Python','2026-09-01','applied','email','solute.de')")
    conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,company,role,classifier) VALUES ('s1','hr@solute.de','Absage','2026-09-20','2026-09-20T10:00:00','b','rejection','Checkout Charlie','Software Developer Python','test')")
    pipeline.link_email(conn, 1)
    assert len(apps(conn)) == 1 and apps(conn)[0]["status"] == "rejected"


def test_invite_after_a_rejection_for_a_different_title_is_a_new_application():
    conn = db.connect()
    conn.execute("INSERT INTO applications (company, role, status, source, applied_date) VALUES ('msg for banking','Software Engineer – Junior bis Senior / Banking','rejected','email','2026-09-01')")
    for i, (ev, role, day) in enumerate([("application_confirmation", "Software Engineer – Junior bis Senior / Banking", "2026-09-01"),
                                         ("rejection", "Software Engineer – Junior bis Senior / Banking", "2026-09-07")], 1):
        conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,company,role,application_id,classifier) VALUES (?,?,?,?,?,?,?,?,?,1,'t')",
                     (f"x{i}", "jobs@msg.group", "s", day, day + "T10:00:00", "b", ev, "msg systems ag", role))
    conn.execute("INSERT INTO emails (message_id,from_addr,subject,sent_date,sent_at,body,event_type,company,role,classifier) VALUES ('x3','jobs@msg.group','s','2026-09-11','2026-09-11T10:00:00','b','interview_invite','msg systems ag','Software Engineer Public Sector','t')")
    conn.commit()
    pipeline.link_email(conn, 3)
    assert len(apps(conn)) == 2 and apps(conn)[0]["status"] == "rejected" and apps(conn)[1]["status"] == "interview"
