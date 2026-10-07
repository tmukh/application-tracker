"""The background worker. One thread owns all mail collecting and classifying, so two syncs never collide.

The dashboard's "Sync now" button and uploads only wake this thread up; they do not do the work themselves.
"""
import logging
import threading
from datetime import datetime

from . import config, db, pipeline

log = logging.getLogger("tracker")

STATE = {"running": False, "done": 0, "total": 0, "message": "", "error": "", "result": None,
         "last_cycle": "", "waiting": 0}
WAKE = threading.Event()
_started = False


def _progress(done: int, total: int, subject: str) -> None:
    STATE.update(done=done, total=total, message=subject)


def cycle() -> dict:
    """One pass: collect mail, classify the queue. Never raises."""
    STATE.update(running=True, done=0, total=0, message="Checking mail...", error="")
    result = {}
    try:
        result = pipeline.sync(progress=_progress)
        STATE["error"] = "; ".join(result["errors"])
    except Exception as e:  # a bug must not kill the worker thread
        log.exception("cycle failed")
        STATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn = db.connect()
        STATE["waiting"] = conn.execute("SELECT COUNT(*) FROM emails WHERE event_type='unclassified'").fetchone()[0]
        conn.close()
        STATE.update(running=False, result=result, last_cycle=datetime.now().strftime("%Y-%m-%d %H:%M"))
    return result


def _loop() -> None:
    while True:
        cycle()
        WAKE.wait(timeout=config.POLL_SECONDS)  # a button press or upload wakes it early
        WAKE.clear()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="tracker-worker", daemon=True).start()


def wake() -> bool:
    """Ask for a cycle now. Returns False if one is already running."""
    if STATE["running"]:
        return False
    WAKE.set()
    return True
