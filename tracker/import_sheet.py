"""One-off import of your old tracker (job_search_tracker_v2.xlsx). Needs: pip install openpyxl"""
from datetime import date, datetime

from . import config, db

STATUS_MAP = {"applied": "applied", "rejected": "rejected", "interviewing": "interview", "interview": "interview",
              "offer": "offer", "withdrawn": "withdrawn", "assessment": "assessment"}


def _d(v):
    if isinstance(v, (datetime, date)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    return str(v)[:10] if v else None


def run(path: str, sheet: str = "Tracker") -> None:
    import openpyxl  # imported here so the rest of the app does not need it
    ws = openpyxl.load_workbook(path, data_only=True)[sheet]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h or "").strip().lower() for h in rows[0]]
    col = {name: head.index(name) for name in head if name}
    conn = db.connect()
    added = skipped = 0
    for r in rows[1:]:
        company = str(r[col["company"]] or "").strip()
        if not company:
            continue
        role = str(r[col.get("role / job id", 1)] or "").strip()
        applied = _d(r[col["date applied"]]) if "date applied" in col else None
        status = STATUS_MAP.get(str(r[col["status"]] or "").strip().lower(), "applied")
        cat = str(r[col["category"]] or "") if "category" in col else ""
        if applied and config.EARLIEST_DATE and applied < config.EARLIEST_DATE:
            skipped += 1  # older than the cutoff
            continue
        if conn.execute("SELECT 1 FROM applications WHERE company=? AND role=? AND applied_date IS ?", (company, role, applied)).fetchone():
            skipped += 1
            continue
        conn.execute("INSERT INTO applications (company, role, applied_date, status, category, source, last_activity) VALUES (?,?,?,?,?, 'import', ?)",
                     (company, role, applied, status, cat, _d(r[col["date closed"]]) if "date closed" in col and r[col["date closed"]] else applied))
        added += 1
    conn.commit()
    conn.close()
    print(f"Imported {added} applications, skipped {skipped} that were already there or older than the cutoff.")
    return added, skipped
