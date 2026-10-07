"""python -m tracker                  start the dashboard and the background worker
python -m tracker sync             one cycle without the web page (drop folder, IMAP, classify)
python -m tracker imap-list        test the mail connection and list the mailbox names
python -m tracker import X.xlsx    import your old Google Sheets tracker
python -m tracker rereview         re-apply the current review rules to stored mail (changes flags only)
python -m tracker prune [--yes]   count (or with --yes delete, after a backup) data older than EARLIEST_DATE
python -m tracker backup [DIR]     copy the database to DIR (default: data/backups), keeps the newest 14
"""
import logging
import sys


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "sync":
        from . import pipeline
        print(pipeline.sync(progress=lambda i, n, s: print(f"[{i}/{n}] {s[:70]}")))
    elif cmd == "imap-list":
        from . import config, imap_source
        with imap_source.Session() as s:
            print(f"Connected to {config.IMAP_HOST}:{config.IMAP_PORT} as {config.IMAP_USER}. Mailboxes:")
            for name in s.list_mailboxes():
                print("  ", name)
        print(f"\nCurrently configured: IMAP_MAILBOX={config.IMAP_MAILBOX}")
    elif cmd == "import":
        from . import import_sheet
        import_sheet.run(sys.argv[2])
    elif cmd == "rereview":
        from . import db, pipeline
        print(pipeline.rereview(db.connect()))
    elif cmd == "prune":
        from . import config, db, pipeline
        conn = db.connect()
        go = "--yes" in sys.argv
        if go:
            print("Backup first:", db.backup(config.DATA / "backups"))
        r = pipeline.prune(conn, apply=go)
        print(f"Cutoff {config.EARLIEST_DATE or 'none'}: {r['emails']} emails and {r['applications']} applications older than that",
              "were deleted." if go else "would be deleted. Run again with --yes to do it.")
    elif cmd == "backup":
        from . import config, db
        print("Backup written:", db.backup(sys.argv[2] if len(sys.argv) > 2 else config.DATA / "backups"))
    else:
        from .app import main as web
        web()


if __name__ == "__main__":
    main()
