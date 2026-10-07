"""Fill a data folder with a made-up job search (invented companies and people) for screenshots and demos.

    TRACKER_DATA=/tmp/demo python scripts/demo/seed.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tracker import config, db, pipeline  # noqa: E402

TODAY = date.today()


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


# company, domain, role, [(days ago/ahead, event_type, subject, summary, interview_at-offset-or-None, interview time)]
APPS = [
    ("Northwind Labs", "northwind.example", "Platform Engineer", [
        (-20, "application_confirmation", "Your application at Northwind Labs", "Application received for Platform Engineer.", None),
        (-12, "assessment", "Northwind Labs: take-home task", "Asks for a small Kubernetes take-home task within 5 days.", None),
        (-6, "interview_invite", "Interview invitation: Platform Engineer", "Invited to a 60 minute technical interview with two engineers.", (2, "14:00")),
    ]),
    ("Contoso Cloud", "contoso-cloud.example", "Site Reliability Engineer", [
        (-15, "application_confirmation", "Thanks for applying to Contoso Cloud", "Application received for Site Reliability Engineer.", None),
        (-9, "interview_invite", "Let's talk: Site Reliability Engineer", "Recruiter invites to a first call.", (4, "10:30")),
        (-8, "follow_up", "Re: Let's talk: Site Reliability Engineer", "Candidate confirmed the first call.", None),
    ]),
    ("Fabrikam Systems", "fabrikam.example", "DevOps Engineer", [
        (-30, "application_confirmation", "Ihre Bewerbung bei Fabrikam Systems", "Application received for DevOps Engineer.", None),
        (-3, "interview_invite", "Einladung zum Vorstellungsgespräch", "Invitation to a video interview with the team lead.", (9, "15:00")),
    ]),
    ("Globex Corporation", "globex.example", "Cloud Engineer", [
        (-25, "application_confirmation", "Application received: Cloud Engineer", "Application received for Cloud Engineer.", None),
        (-5, "offer", "Your offer from Globex", "Verbal offer, contract to follow.", None),
    ]),
    ("Initech", "initech.example", "Backend Engineer (Python)", [
        (-40, "application_confirmation", "Thank you for your application", "Application received for Backend Engineer.", None),
        (-21, "rejection", "Update on your application", "Position was filled by another candidate.", None),
    ]),
    ("Umbrella AI", "umbrella-ai.example", "ML Platform Engineer", [
        (-28, "application_confirmation", "We received your application", "Application received for ML Platform Engineer.", None),
        (-14, "rejection", "Your application to Umbrella AI", "Declined after the initial screening.", None),
    ]),
    ("Hooli Berlin", "hooli.example", "Software Engineer, Infrastructure", [
        (-18, "application_confirmation", "Application received", "Application received for Software Engineer, Infrastructure.", None),
    ]),
    ("Stark Industries", "stark.example", "Junior DevOps Engineer", [
        (-11, "application_confirmation", "Your application to Stark Industries", "Application received for Junior DevOps Engineer.", None),
    ]),
    ("Wayne Enterprises", "wayne.example", "Platform Engineer", [
        (-7, "application_confirmation", "Thanks for your interest", "Application received for Platform Engineer.", None),
    ]),
    ("Wonka Digital", "wonka.example", "Cloud Native Engineer", [
        (-4, "application_confirmation", "Eingangsbestätigung Ihrer Bewerbung", "Application received for Cloud Native Engineer.", None),
    ]),
    ("Cyberdyne Systems", "cyberdyne.example", "Site Reliability Engineer", [
        (-33, "application_confirmation", "Your application to Cyberdyne", "Application received for Site Reliability Engineer.", None),
        (-26, "rejection", "Application update", "Not moving forward.", None),
    ]),
    ("Soylent Software", "soylent.example", "Infrastructure Engineer", [
        (-22, "application_confirmation", "Application received", "Application received for Infrastructure Engineer.", None),
        (-10, "assessment", "Next step: coding challenge", "Asks for a 90 minute online coding challenge.", None),
    ]),
    ("Tyrell Robotics", "tyrell.example", "DevOps Engineer", [
        (-16, "application_confirmation", "Your application at Tyrell Robotics", "Application received for DevOps Engineer.", None),
    ]),
    ("Acme Cloud", "acme-cloud.example", "Cloud Platform Engineer", [
        (-9, "application_confirmation", "Application received", "Application received for Cloud Platform Engineer.", None),
        (-2, "follow_up", "A question about your availability", "Recruiter asks about the notice period and salary range.", None),
    ]),
]

BODIES = {
    "application_confirmation": "Hello,\n\nthank you for your application as {role}. We have received your documents and will get back to you soon.\n\nBest regards\nPeople Team {company}",
    "assessment": "Hello,\n\nthank you for your interest in the {role} position. As a next step we would like you to complete a short task. You have five days.\n\nBest regards\n{company}",
    "interview_invite": "Hello,\n\nwe would like to get to know you better and invite you to an interview for the {role} position.\n\nPlease confirm the time by reply.\n\nBest regards\n{company}",
    "offer": "Hello,\n\nwe are happy to offer you the {role} position. The contract will follow by email.\n\nBest regards\n{company}",
    "rejection": "Hello,\n\nthank you for your time. Unfortunately we have decided to move forward with other candidates for the {role} position.\n\nBest regards\n{company}",
    "follow_up": "Hello,\n\na quick question regarding your application for {role}: could you tell us your notice period?\n\nBest regards\n{company}",
}


def seed():
    config.ensure_dirs()
    conn = db.connect()
    n = 0
    ids = {}
    for company, domain, role, events in APPS:
        cur = conn.execute("INSERT INTO applications (company, role, status, source, domains) VALUES (?,?,?,?,?)",
                           (company, role, "applied", "email", domain))
        ids[company] = cur.lastrowid
        for off, ev, subject, summary, itv in events:
            n += 1
            when = d(off)
            at = f"{d(itv[0])}T{itv[1]}:00" if itv else ""
            conn.execute("""INSERT INTO emails (message_id, from_name, from_addr, subject, sent_date, sent_at, body, event_type,
                            company, role, interview_at, summary, classifier, application_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (f"demo{n}@example.org", f"{company} Recruiting", f"recruiting@{domain}", subject, when,
                          f"{when}T09:{n % 50:02d}:00", BODIES[ev].format(role=role, company=company), ev, company, role,
                          at, summary, "ollama:qwen2.5:7b", ids[company]))
    # a few mails that really are ambiguous, so the review page has something to show
    reviews = [
        ("Northwind Labs", "recruiting@northwind.example", "Quick question about your CV", "follow_up", "Asks which of two open roles the CV was meant for.", "2 applications at this company, please check the assignment"),
        ("", "talent@mail.example", "Einladung zum Gespräch", "interview_invite", "Invites to a call but does not name the company.", "no company name found"),
        ("Hooli Berlin", "jobs@hooli.example", "Following up on your application", "follow_up", "Generic status message without any details.", "LLM failed (bad JSON); keyword result used"),
    ]
    for company, addr, subject, ev, summary, why in reviews:
        n += 1
        conn.execute("""INSERT INTO emails (message_id, from_name, from_addr, subject, sent_date, sent_at, body, event_type, company,
                        summary, classifier, needs_review, review_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                     (f"demo{n}@example.org", company or "Talent Team", addr, subject, d(-1), f"{d(-1)}T11:{n:02d}:00",
                      "Hello,\n\nwe wanted to follow up on your application and arrange a short call.\n\nBest regards", ev, company, summary, "ollama:qwen2.5:7b", why))
    for aid in ids.values():
        pipeline.recompute(conn, aid)
    # applied dates: first confirmation; one note, to show the field
    conn.execute("UPDATE applications SET notes='Team lead is Sam. Ask about on-call rotation.' WHERE company='Contoso Cloud'")
    conn.commit()
    conn.close()
    print(f"Seeded {len(APPS)} applications and {n} emails into {config.DATA}")


if __name__ == "__main__":
    seed()
