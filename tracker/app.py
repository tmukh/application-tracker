"""Local web dashboard. Bound to 127.0.0.1, so only this computer can open it."""
import csv
import io
import re
import time
from datetime import date

from urllib.parse import urlsplit

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from . import config, db, imap_source, pipeline, service, settings
from .classify import EVENT_TYPES

app = Flask(__name__)
app.secret_key = "local-only-not-a-secret"

EVENT_LABELS = {
    "application_confirmation": "Application received", "assessment": "Assessment / challenge",
    "interview_invite": "Interview invitation", "offer": "Offer", "rejection": "Rejection",
    "follow_up": "Follow-up", "other": "Other (job related)", "not_job": "Not about my applications",
}
OPEN_STATUSES = ("applied", "assessment", "interview", "offer")


@app.before_request
def refuse_other_websites():
    """A web page you have open in another tab could silently post to this local address. Browsers always send
    the page's origin with such a request, so a POST from any origin other than this dashboard is refused."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            abort(403)


@app.template_filter("days_ago")
def days_ago(d):
    try:
        return (date.today() - date.fromisoformat(d)).days
    except Exception:
        return None


@app.template_filter("nice_dt")
def nice_dt(v):
    """'2026-10-09T14:00:00+01:00' -> 'Fri 9 Oct, 14:00'. Unreadable values are shown as they are."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return v or ""
    day = f"{dt:%a} {dt.day} {dt:%b}"
    return day + (f", {dt:%H:%M}" if "T" in v else "")


@app.context_processor
def inject():
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM emails WHERE needs_review=1").fetchone()[0]
    conn.close()
    return {"review_count": n, "EVENT_LABELS": EVENT_LABELS, "STATUSES": db.STATUSES, "sync_running": service.STATE["running"],
            "mail_configured": bool(config.IMAP_HOST and config.IMAP_USER and config.IMAP_PASSWORD)}


@app.route("/")
def index():
    status, q = request.args.get("status", ""), request.args.get("q", "").strip()
    conn = db.connect()
    sql, args = "SELECT a.*, (SELECT COUNT(*) FROM emails e WHERE e.application_id=a.id) AS n_emails FROM applications a WHERE 1=1", []
    if status:
        sql += " AND a.status=?"
        args.append(status)
    if q:
        sql += " AND (a.company LIKE ? OR a.role LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY COALESCE(a.last_activity, a.applied_date, '') DESC, a.id DESC"
    apps = conn.execute(sql, args).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status")}
    today = date.today().isoformat()
    upcoming = [a for a in conn.execute("SELECT * FROM applications WHERE interview_at != '' AND status='interview'").fetchall()
                if a["interview_at"][:10] >= today and a["interview_at"][:1].isdigit()]
    upcoming.sort(key=lambda a: a["interview_at"])
    conn.close()
    return render_template("index.html", apps=apps, counts=counts, total=sum(counts.values()), status=status, q=q, upcoming=upcoming)


@app.route("/app/<int:app_id>")
def detail(app_id):
    conn = db.connect()
    a = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not a:
        return redirect(url_for("index"))
    ems = conn.execute("SELECT * FROM emails WHERE application_id=? ORDER BY sent_at", (app_id,)).fetchall()
    conn.close()
    return render_template("detail.html", a=a, emails=ems)


@app.route("/app/<int:app_id>/update", methods=["POST"])
def update(app_id):
    f = request.form
    conn = db.connect()
    cur = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    locked = cur["status_locked"]
    if f.get("unlock"):
        locked = 0
    elif f["status"] != cur["status"]:
        locked = 1  # you changed it by hand, so emails must not overwrite it
    conn.execute("""UPDATE applications SET company=?, role=?, applied_date=?, status=?, category=?, notes=?, status_locked=? WHERE id=?""",
                 (f["company"].strip() or cur["company"], f["role"].strip(), f["applied_date"] or None,
                  f["status"], f["category"].strip(), f["notes"], locked, app_id))
    pipeline.recompute(conn, app_id)
    conn.commit()
    conn.close()
    flash("Saved.")
    return redirect(url_for("detail", app_id=app_id))


@app.route("/app/<int:app_id>/delete", methods=["POST"])
def delete(app_id):
    conn = db.connect()
    pipeline.delete_application(conn, app_id)
    conn.close()
    flash("Application deleted. Its emails are back in the review queue.")
    return redirect(url_for("index"))


@app.route("/add", methods=["POST"])
def add():
    f = request.form
    if f.get("company", "").strip():
        conn = db.connect()
        conn.execute("INSERT INTO applications (company, role, applied_date, status, source) VALUES (?,?,?,?, 'manual')",
                     (f["company"].strip(), f.get("role", "").strip(), f.get("applied_date") or date.today().isoformat(), "applied"))
        conn.commit()
        conn.close()
    return redirect(url_for("index"))


@app.route("/review")
def review():
    conn = db.connect()
    ems = conn.execute("SELECT * FROM emails WHERE needs_review=1 ORDER BY sent_at DESC").fetchall()
    apps = conn.execute("SELECT id, company, role FROM applications ORDER BY company").fetchall()
    conn.close()
    return render_template("review.html", emails=ems, apps=apps, event_types=EVENT_TYPES)


@app.route("/email/<int:email_id>/resolve", methods=["POST"])
def resolve(email_id):
    f = request.form
    conn = db.connect()
    pipeline.resolve_email(conn, email_id, f["event_type"], f.get("application", "none"), f.get("company", "").strip(), f.get("role", "").strip())
    conn.close()
    return redirect(url_for("review"))


@app.route("/upload", methods=["POST"])
def upload():
    config.ensure_dirs()
    saved = 0
    for f in request.files.getlist("files"):
        name = re.sub(r"[^\w.\- ]", "_", f.filename or "")
        if name.lower().endswith((".eml", ".mbox")):
            f.save(config.INBOX / f"{int(time.time() * 1000)}-{name}")
            saved += 1
    if saved:
        flash(f"{saved} file(s) added. Reading them now.")
        service.wake()
    else:
        flash("Only .eml and .mbox files are accepted.")
    return redirect(url_for("index"))


@app.route("/sync", methods=["POST"])
def sync():
    if not service.wake():
        flash("A sync is already running.")
    return redirect(request.referrer or url_for("index"))


@app.route("/sync/status")
def sync_status():
    return jsonify(service.STATE)


@app.route("/health")
def health():
    """For monitoring: 200 when the worker has run and nothing is wrong, 503 otherwise."""
    st = service.STATE
    ok = bool(st["last_cycle"]) and not st["error"]
    return jsonify(ok=ok, last_cycle=st["last_cycle"], waiting=st["waiting"], error=st["error"]), (200 if ok else 503)


# ---------------------------------------------------------------- settings
def _form_values() -> dict:
    f = request.form
    vals = {}
    for key, typ in settings.FIELDS.items():
        if key in f:
            raw = f[key].strip() if key != "IMAP_PASSWORD" else f[key]
            try:
                vals[key] = typ(raw) if raw != "" or typ is str else getattr(config, key)
            except ValueError:
                raise settings.SettingsError(f"{key.replace('_', ' ').title()} must be a number.")
    return vals


def _save_form() -> bool:
    try:
        settings.save(_form_values())
        return True
    except settings.SettingsError as e:
        flash(str(e))
        return False


@app.route("/settings")
def settings_page():
    conn = db.connect()
    preview = pipeline.prune(conn)
    conn.close()
    return render_template("settings.html", s=settings.current(), preview=preview)


@app.route("/settings", methods=["POST"])
def settings_save():
    if _save_form():
        flash("Saved. Checking mail with the new settings now.")
        service.wake()
    return redirect(url_for("settings_page"))


@app.route("/settings/mailboxes", methods=["POST"])
def settings_mailboxes():
    """Save the mail fields, connect, and return the mailbox names so you can pick one instead of typing it."""
    try:
        settings.save(_form_values())
        with imap_source.Session() as s:
            return jsonify(ok=True, mailboxes=s.list_mailboxes())
    except (settings.SettingsError, imap_source.ImapError) as e:
        return jsonify(ok=False, error=str(e))
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}")


@app.route("/settings/models", methods=["POST"])
def settings_models():
    """Save the model fields, ask Ollama what it has installed, and return the names for the drop-down."""
    import requests
    try:
        settings.save(_form_values())
        r = requests.get(f"{config.OLLAMA_URL.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        return jsonify(ok=True, models=[m["name"] for m in r.json().get("models", [])])
    except settings.SettingsError as e:
        return jsonify(ok=False, error=str(e))
    except Exception as e:
        return jsonify(ok=False, error=f"Cannot reach Ollama at {config.OLLAMA_URL}. Is it running? ({e})")


@app.route("/settings/prune", methods=["POST"])
def settings_prune():
    if request.form.get("confirm") != "yes":
        abort(400)
    db.backup(config.DATA / "backups")  # always keep a way back
    conn = db.connect()
    r = pipeline.prune(conn, apply=True)
    conn.close()
    flash(f"Deleted {r['emails']} emails and {r['applications']} applications from before {config.EARLIEST_DATE}. A backup was made first.")
    return redirect(url_for("settings_page"))


@app.route("/settings/backup")
def settings_backup():
    """Download a consistent copy of the database. It never contains the mail password."""
    target = db.backup(config.DATA / "backups")
    return send_file(target, as_attachment=True, download_name=target.name)


@app.route("/settings/import", methods=["POST"])
def settings_import():
    f = request.files.get("sheet")
    if not f or not (f.filename or "").lower().endswith(".xlsx"):
        flash("Choose an .xlsx file.")
        return redirect(url_for("settings_page"))
    config.ensure_dirs()
    tmp = config.DATA / f"import-{int(time.time())}.xlsx"
    f.save(tmp)
    try:
        from . import import_sheet
        added, skipped = import_sheet.run(str(tmp))
        flash(f"Imported {added} applications ({skipped} skipped: already there or older than the cutoff).")
    except Exception as e:
        flash(f"Could not read that spreadsheet: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    return redirect(url_for("index"))


@app.route("/export.csv")
def export():
    conn = db.connect()
    rows = conn.execute("SELECT company, role, category, status, applied_date, last_activity, interview_at, notes FROM applications ORDER BY applied_date").fetchall()
    conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Company", "Role", "Category", "Status", "Date applied", "Last activity", "Interview", "Notes"])
    w.writerows([list(r) for r in rows])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",  # BOM so Excel reads umlauts correctly
                    headers={"Content-Disposition": "attachment; filename=applications.csv"})


def main():
    config.ensure_dirs()
    settings.load()  # what you saved in the dashboard wins over .env
    conn = db.connect()
    pipeline.rereview(conn)  # re-apply the current review rules to older mail (flags only; harmless to repeat)
    pipeline.migrate(conn)   # one-time: re-link mail with improved rules after an update (backs up first)
    conn.close()
    service.start()  # collects mail and classifies in the background
    print(f"Application tracker: http://{config.HOST}:{config.PORT}   (Ctrl+C to stop)")
    try:
        from waitress import serve  # a proper server for long-running use
        serve(app, host=config.HOST, port=config.PORT, threads=4)
    except ImportError:
        print("waitress is not installed, falling back to Flask's development server")
        app.run(host=config.HOST, port=config.PORT, debug=False)
