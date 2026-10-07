"""The workflow: mail -> emails table -> classification -> applications -> status.

Collecting mail and classifying it are separate steps on purpose. Mail is stored first and classified from a
queue (emails whose event_type is still 'unclassified'). If the model server is unreachable, for example
because the desktop with the GPU is asleep, the queue simply waits and is retried on the next cycle.
"""
import logging
import re
import shutil
import time

from . import config, db, imap_source, ingest, linker
from .classify import DECISIVE, KeywordClassifier, LLMUnavailable, get_classifier

log = logging.getLogger("tracker")
_KW = KeywordClassifier()
# Feedback surveys keep being read as "assessment" by small models; they are never about the application itself.
_SURVEY = re.compile(r"(how was your .{0,40}experience|recruit\w* experience|candidate experience|feedback survey|umfrage|bewerbungserlebnis)", re.I)
_NEW_APP_TYPES = DECISIVE  # emails of these types may create an application when nothing matches


# ------------------------------------------------------------------ ingest
def store_email(conn, m: dict) -> int:
    """Insert one parsed message. Returns 1 if it was new, 0 if we already had it (same Message-ID) or it is
    older than the cutoff (config.EARLIEST_DATE)."""
    if config.EARLIEST_DATE and (m.get("sent_date") or "") and m["sent_date"] < config.EARLIEST_DATE:
        return 0
    cur = conn.execute(
        """INSERT OR IGNORE INTO emails
           (message_id, refs, from_name, from_addr, subject, sent_date, sent_at, body, source_file)
           VALUES (:message_id, :refs, :from_name, :from_addr, :subject, :sent_date, :sent_at, :body, :source_file)""", m)
    return cur.rowcount


def ingest_inbox(conn) -> tuple[int, list[str]]:
    """Read every .eml/.mbox in the inbox folder. Returns (new emails, errors)."""
    new, errors = 0, []
    for path in ingest.find_files(config.INBOX):
        try:
            messages = ingest.parse_file(path)
        except Exception as e:
            errors.append(f"{path.name}: {e}")
            continue
        for m in messages:
            new += store_email(conn, m)
        conn.commit()
        dest = config.PROCESSED / path.name
        if dest.exists():
            dest = config.PROCESSED / f"{int(time.time())}-{path.name}"
        shutil.move(str(path), str(dest))
    return new, errors


def poll_imap(conn) -> int:
    """Fetch messages newer than the saved cursor from the IMAP mailbox. Safe to repeat, safe to interrupt."""
    if not config.IMAP_HOST:
        return 0
    box = config.IMAP_MAILBOX
    kv_validity, kv_uid = f"imap_uidvalidity|{box}", f"imap_last_uid|{box}"
    new = 0
    with imap_source.Session() as s:
        validity = s.select(box)
        if db.kv_get(conn, kv_validity) != str(validity):
            # first run, or the server renumbered its messages: start over. Message-ID dedupe prevents duplicates.
            db.kv_set(conn, kv_validity, validity)
            db.kv_set(conn, kv_uid, 0)
            conn.commit()
        # Lowering the cutoff means older mail now qualifies, so read the mailbox from the start again
        # (mail we already have is skipped by Message-ID). Raising it needs no re-read; use `prune` to drop old data.
        kv_cut = f"imap_earliest|{box}"
        seen = db.kv_get(conn, kv_cut)
        if seen is not None and config.EARLIEST_DATE < seen:
            db.kv_set(conn, kv_uid, 0)
        db.kv_set(conn, kv_cut, config.EARLIEST_DATE)
        conn.commit()
        last = int(db.kv_get(conn, kv_uid, 0))
        for uid, m in s.iter_new(last):
            new += store_email(conn, m)
            db.kv_set(conn, kv_uid, uid)  # cursor moves only after the mail is stored
            conn.commit()
    return new


# ---------------------------------------------------------------- classify
def classify_row(row, clf):
    """Returns (Result, review_reason). Raises LLMUnavailable when the model server is down or busy."""
    em = dict(row)
    kw = _KW.classify(em)
    reason = ""
    if clf is None:
        res = kw
    else:
        res = clf.classify(em)
        if res.error:
            if res.retryable:
                raise LLMUnavailable(res.error)  # leave the email queued instead of settling for keywords
            reason = f"LLM failed ({res.error}); keyword result used"
            res = kw
        elif not res.company:
            res.company = kw.company
        # The model's answer stands even when the keyword rules disagree. The rules are blunt (any "Einladung" looks
        # like an interview), and on real mail most disagreements were the rules being wrong.
    if _SURVEY.search(em.get("subject") or "") and res.event_type in ("assessment", "interview_invite", "follow_up", "offer", "rejection"):
        res.event_type = "other"
    if not res.company and res.event_type in DECISIVE:
        reason = reason or "no company name found"  # only matters if the mail would create or change an application
    return res, reason


def classify_pending(conn, clf, progress=None, stats=None) -> int:
    stats = stats if stats is not None else {}
    stats["classified"] = 0
    todo = conn.execute("SELECT * FROM emails WHERE event_type='unclassified' ORDER BY sent_at").fetchall()
    for i, row in enumerate(todo, 1):
        res, reason = classify_row(row, clf)
        conn.execute("""UPDATE emails SET event_type=?, company=?, role=?, interview_at=?, summary=?, classifier=?,
                        needs_review=?, review_reason=? WHERE id=?""",
                     (res.event_type, res.company, res.role, res.interview_at, res.summary, res.classifier,
                      1 if reason else 0, reason, row["id"]))
        link_email(conn, row["id"])
        conn.commit()  # each email is final once classified, so an interruption loses nothing
        stats["classified"] = i
        if progress:
            progress(i, len(todo), row["subject"])
    return len(todo)


# -------------------------------------------------------------------- link
def _create_application(conn, em: dict) -> int:
    status = linker.EVENT_TO_STATUS.get(em["event_type"], "rejected" if em["event_type"] == "rejection" else "applied")
    cur = conn.execute(
        "INSERT INTO applications (company, role, applied_date, status, source) VALUES (?,?,?,?, 'email')",
        (em["company"], em.get("role", ""), em["sent_date"] if em["event_type"] == "application_confirmation" else None, status))
    return cur.lastrowid


def link_email(conn, email_id: int) -> None:
    em = dict(conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone())
    if em["event_type"] in ("not_job", "unclassified"):
        conn.execute("UPDATE emails SET application_id=NULL WHERE id=?", (email_id,))
        return
    app_id, reason = linker.find_application(conn, em)
    if app_id is None:
        if em["company"] and em["event_type"] in _NEW_APP_TYPES:
            app_id = _create_application(conn, em)
    # Mail that is not about an application (newsletters, alerts, follow-ups from unknown senders) stays unlinked
    # without bothering you: there is nothing to decide.
    if em["event_type"] not in DECISIVE:
        reason = ""
    review = em["review_reason"] or reason
    conn.execute("UPDATE emails SET application_id=?, needs_review=?, review_reason=? WHERE id=?",
                 (app_id, 1 if review else 0, review, email_id))
    if app_id:
        recompute(conn, app_id)


def recompute(conn, app_id: int) -> None:
    """Rebuild status, dates and domains of one application from its emails."""
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return
    ems = conn.execute("SELECT * FROM emails WHERE application_id=? ORDER BY sent_at", (app_id,)).fetchall()
    # An application created from email has no history except its emails, so its status must come from them alone.
    # (Otherwise a status earned by mail that was later deleted, for example by the date cutoff, would stay forever.)
    base = "applied" if app["source"] == "email" else app["status"]
    status = app["status"] if app["status_locked"] else linker.derive_status([e["event_type"] for e in ems], base)
    applied = app["applied_date"]
    confs = [e["sent_date"] for e in ems if e["event_type"] == "application_confirmation"]
    if confs and (not applied or min(confs) < applied):
        applied = min(confs)
    last = max([e["sent_date"] for e in ems], default=app["last_activity"])
    invites = [e["interview_at"] for e in ems if e["event_type"] == "interview_invite" and e["interview_at"]]
    timed = [d for d in invites if "T" in d]  # a date with a time is a real appointment; a bare date is often just the sent date
    if status != "interview":
        interview_at = app["interview_at"] if app["source"] != "email" else ""
    else:
        interview_at = (timed or invites or [app["interview_at"] if app["source"] != "email" else ""])[-1]
    domains = set((app["domains"] or "").split())
    for e in ems:
        d = linker.useful_domain(e["from_addr"])
        if d:
            domains.add(d)
    role = app["role"] or next((e["role"] for e in ems if e["role"]), "")
    conn.execute("""UPDATE applications SET status=?, applied_date=?, last_activity=?, interview_at=?, domains=?, role=? WHERE id=?""",
                 (status, applied, last, interview_at, " ".join(sorted(domains)), role, app_id))


# -------------------------------------------------------------------- sync
def sync(progress=None, clf="auto") -> dict:
    """One full cycle: read the drop folder, poll IMAP, classify everything queued. Safe to run repeatedly.

    Problems do not abort the cycle. A failing mailbox does not stop classification of mail already stored,
    and an unreachable model does not lose mail: it stays queued. Everything that went wrong is in 'errors'."""
    conn = db.connect()
    errors: list[str] = []
    try:
        new, file_errors = ingest_inbox(conn)
        errors += file_errors
        try:
            new += poll_imap(conn)
        except Exception as e:
            log.warning("mail check failed: %s", e)
            errors.append(f"mail: {e}")
        stats = {"classified": 0}
        try:
            if clf == "auto":
                clf = get_classifier()  # raises LLMUnavailable with a readable message if Ollama is down
            classify_pending(conn, clf, progress, stats)
        except LLMUnavailable as e:
            log.warning("model unavailable, mail stays queued: %s", e)
            errors.append(f"model unavailable (mail stays queued and will be retried): {e}")
        waiting = conn.execute("SELECT COUNT(*) FROM emails WHERE event_type='unclassified'").fetchone()[0]
        review = conn.execute("SELECT COUNT(*) FROM emails WHERE needs_review=1").fetchone()[0]
        return {"new_emails": new, "classified": stats["classified"], "waiting": waiting, "needs_review": review, "errors": errors}
    finally:
        conn.close()


def rereview(conn) -> dict:
    """Apply the current review rules to mail that is already stored. Only the 'needs review' flags change:
    no email is reclassified or relinked, so no application can be created or duplicated by this. Manual
    decisions are never touched."""
    rows = conn.execute("SELECT id, event_type, review_reason FROM emails WHERE needs_review=1 AND classifier != 'manual'").fetchall()
    cleared = 0
    for r in rows:
        why, decisive = r["review_reason"], r["event_type"] in DECISIVE
        drop = (why.startswith("LLM says")                       # keyword disagreement is no longer a reason
                or why == "could not match to an application"
                or (not decisive and ("applications at this company" in why or why == "no company name found")))
        if drop:
            conn.execute("UPDATE emails SET needs_review=0, review_reason='' WHERE id=?", (r["id"],))
            cleared += 1
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM emails WHERE needs_review=1").fetchone()[0]
    return {"cleared": cleared, "still_to_review": left}


def prune(conn, apply: bool = False) -> dict:
    """Remove stored data older than the cutoff. Without apply=True it only counts what would go.

    An email goes if it was sent before the cutoff. An application goes if it has no email from the cutoff onwards
    and its latest known date is before the cutoff. An old application that got a reply this year stays: it is live.
    Applications with no date at all stay, since there is nothing to judge them by."""
    cut = config.EARLIEST_DATE
    if not cut:
        return {"emails": 0, "applications": 0, "applied": False}
    old_emails = conn.execute("SELECT COUNT(*) FROM emails WHERE sent_date != '' AND sent_date < ?", (cut,)).fetchone()[0]
    stale = [r["id"] for r in conn.execute(
        """SELECT a.id FROM applications a
           WHERE NOT EXISTS (SELECT 1 FROM emails e WHERE e.application_id=a.id AND e.sent_date >= ?)
             AND COALESCE(MAX(COALESCE(a.last_activity,''), COALESCE(a.applied_date,'')), '') != ''
             AND MAX(COALESCE(a.last_activity,''), COALESCE(a.applied_date,'')) < ?""", (cut, cut))]
    if apply:
        conn.execute("DELETE FROM emails WHERE sent_date != '' AND sent_date < ?", (cut,))
        for aid in stale:
            conn.execute("DELETE FROM applications WHERE id=?", (aid,))
        for r in conn.execute("SELECT id FROM applications").fetchall():
            recompute(conn, r["id"])
        conn.commit()
    return {"emails": old_emails, "applications": len(stale), "applied": apply}


LINKING_VERSION = "2"


def relink_all(conn) -> dict:
    """Re-decide which application each email belongs to, with the current linking rules.

    Emails you resolved by hand keep their link, and so do confirmations (they define the applications). Everything else
    is unlinked and linked again in time order, so each decision only sees the mail before it. Applications that end up
    with no email, no notes and no manual edits are removed. Statuses and dates are then rebuilt from the emails."""
    rows = conn.execute("""SELECT id, application_id FROM emails WHERE classifier != 'manual'
                           AND event_type NOT IN ('not_job','unclassified','application_confirmation')
                           ORDER BY sent_at, id""").fetchall()
    for r in conn.execute("SELECT id, subject, event_type FROM emails WHERE classifier != 'manual'").fetchall():
        if _SURVEY.search(r["subject"] or "") and r["event_type"] in ("assessment", "interview_invite", "follow_up", "offer", "rejection"):
            conn.execute("UPDATE emails SET event_type='other' WHERE id=?", (r["id"],))
    rows = conn.execute("""SELECT id, application_id FROM emails WHERE classifier != 'manual'
                           AND event_type NOT IN ('not_job','unclassified','application_confirmation')
                           ORDER BY sent_at, id""").fetchall()
    before = {r["id"]: r["application_id"] for r in rows}
    for r in rows:
        conn.execute("""UPDATE emails SET application_id=NULL,
                        needs_review=CASE WHEN review_reason LIKE '%applications at this company%' THEN 0 ELSE needs_review END,
                        review_reason=CASE WHEN review_reason LIKE '%applications at this company%' THEN '' ELSE review_reason END
                        WHERE id=?""", (r["id"],))
    for r in rows:
        link_email(conn, r["id"])
    # second pass: a follow-up that arrived before the application it belongs to was created can find it now
    for r in rows:
        if conn.execute("SELECT application_id FROM emails WHERE id=?", (r["id"],)).fetchone()[0] is None:
            link_email(conn, r["id"])
    moved = sum(1 for r in rows if conn.execute("SELECT application_id FROM emails WHERE id=?", (r["id"],)).fetchone()[0] != before[r["id"]])
    # An application made from mail is born from a decisive mail (confirmation, invite, rejection ...). If none is left
    # and nothing but surveys or notices remains, it was a ghost made from a mislabeled mail. Drop it unless you touched
    # it. One with a real follow-up stays: such a mail is often a confirmation the model read as a follow-up.
    orphans = conn.execute("""SELECT id FROM applications a WHERE source='email' AND status_locked=0 AND notes='' AND category=''
                              AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.application_id=a.id AND e.event_type IN
                                              ('application_confirmation','assessment','interview_invite','offer','rejection','follow_up'))""").fetchall()
    for o in orphans:
        conn.execute("UPDATE emails SET application_id=NULL WHERE application_id=?", (o["id"],))
        conn.execute("DELETE FROM applications WHERE id=?", (o["id"],))
    for a in conn.execute("SELECT id FROM applications").fetchall():
        recompute(conn, a["id"])
    db.kv_set(conn, "linking_version", LINKING_VERSION)
    conn.commit()
    return {"emails_moved": moved, "applications_removed": len(orphans)}


def migrate(conn) -> dict:
    """Run one-time data fixes when the linking rules changed. Safe to call on every start."""
    if db.kv_get(conn, "linking_version") == LINKING_VERSION:
        return {}
    if not conn.execute("SELECT 1 FROM emails LIMIT 1").fetchone():
        db.kv_set(conn, "linking_version", LINKING_VERSION)
        conn.commit()
        return {}
    db.backup(config.DATA / "backups")  # a way back before rewriting links
    return relink_all(conn)


# ---------------------------------------------------------- manual changes
def resolve_email(conn, email_id: int, event_type: str, app_choice: str, company: str = "", role: str = "") -> None:
    """Your decision overrides the classifier. app_choice: an application id, 'new', or 'none'."""
    old = conn.execute("SELECT application_id FROM emails WHERE id=?", (email_id,)).fetchone()["application_id"]
    new_id = None
    if event_type != "not_job":
        if app_choice == "new":
            em = dict(conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone())
            em.update(event_type=event_type, company=company or em["company"], role=role or em["role"])
            new_id = _create_application(conn, em)
        elif app_choice not in ("none", ""):
            new_id = int(app_choice)
    conn.execute("UPDATE emails SET event_type=?, application_id=?, needs_review=0, review_reason='', classifier='manual' WHERE id=?",
                 (event_type, new_id, email_id))
    for aid in {old, new_id} - {None}:
        recompute(conn, aid)
    conn.commit()


def delete_application(conn, app_id: int) -> None:
    conn.execute("UPDATE emails SET application_id=NULL, needs_review=1, review_reason='application was deleted' WHERE application_id=?", (app_id,))
    conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
    conn.commit()
