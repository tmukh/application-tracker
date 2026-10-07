import datetime
import openpyxl

from conftest import make_eml
from tracker import db, import_sheet, pipeline


def make_sheet(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tracker"
    ws.append(["Company", "Role / Job ID", "Category", "Status", "Date Applied", "Date Closed", "Days", "Link"])
    ws.append(["Bundesdruckerei", "Site Reliability Engineer", "Public", "Applied", datetime.datetime(2026, 9, 1), None, 7, ""])
    ws.append(["Deloitte", "Cloud Software Engineer", "Consultancy", "Rejected", datetime.datetime(2026, 8, 30), datetime.datetime(2026, 8, 30), 0, ""])
    ws.append(["AWS (ESC Managed Ops)", "Systems Engineer", "BigTech", "Interviewing", datetime.datetime(2026, 8, 20), None, 19, ""])
    ws.append([None, None, None, None, None, None, None, None])
    wb.save(path)


def test_import_then_email_links_to_imported_row(tmp_path):
    p = tmp_path / "t.xlsx"
    make_sheet(p)
    import_sheet.run(str(p))
    import_sheet.run(str(p))  # second run must not duplicate
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 3
    assert conn.execute("SELECT status FROM applications WHERE company='AWS (ESC Managed Ops)'").fetchone()[0] == "interview"
    # a later interview mail for Bundesdruckerei lands on the imported row instead of creating a second one
    make_eml("Anna <a@bundesdruckerei.de>", "Einladung zum Vorstellungsgespräch bei der Bundesdruckerei",
             "Wir laden Sie zu einem Gespräch ein.", "2026-10-02")
    # and a confirmation with a differently worded role still matches the imported row
    make_eml("Bundesdruckerei <noreply@bundesdruckerei.de>", "Ihre Bewerbung bei der Bundesdruckerei",
             "Ihre Bewerbung als Site Reliability Engineer (m/w/d) ist eingegangen.", "2026-09-01")
    pipeline.sync(clf=None)
    rows = conn.execute("SELECT company, status FROM applications WHERE company LIKE 'Bundes%'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "interview"
