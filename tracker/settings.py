"""Settings you change in the dashboard instead of editing files.

They are saved in data/settings.json (not in the database, so database backups never contain your mail password)
and override the values from .env / environment variables. The password is write-only: it can be replaced but is
never sent back to the browser.
"""
import json
import os
import re
from pathlib import Path

from . import config

# key -> (type, label). Only these can be changed from the UI.
FIELDS = {
    "IMAP_HOST": str, "IMAP_PORT": int, "IMAP_SECURITY": str, "IMAP_USER": str, "IMAP_PASSWORD": str,
    "IMAP_MAILBOX": str, "EARLIEST_DATE": str, "POLL_SECONDS": int,
    "OLLAMA_URL": str, "OLLAMA_MODEL": str, "CLASSIFIER": str,
}
SECRET = {"IMAP_PASSWORD"}


class SettingsError(ValueError):
    pass


def path() -> Path:
    return config.DATA / "settings.json"


def _read() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load() -> None:
    """Apply the saved settings on top of what .env / the environment gave. Called once at start-up."""
    for k, v in _read().items():
        if k in FIELDS:
            setattr(config, k, FIELDS[k](v))


def current(include_secret: bool = False) -> dict:
    out = {k: getattr(config, k) for k in FIELDS if include_secret or k not in SECRET}
    out["has_password"] = bool(config.IMAP_PASSWORD)
    return out


def validate(v: dict) -> dict:
    """Return the cleaned values or raise SettingsError with a message meant for the person using the form."""
    if "IMAP_PORT" in v and not (1 <= v["IMAP_PORT"] <= 65535):
        raise SettingsError("The mail port must be a number between 1 and 65535.")
    if v.get("IMAP_SECURITY", "starttls") not in ("starttls", "ssl", "none"):
        raise SettingsError("Mail security must be starttls, ssl or none.")
    if v.get("IMAP_SECURITY") == "none" and v.get("IMAP_HOST", "") not in ("", "127.0.0.1", "localhost", "::1"):
        raise SettingsError("Unencrypted mail is only allowed for this computer. Use starttls or ssl for other servers.")
    d = v.get("EARLIEST_DATE", "")
    if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise SettingsError("The cutoff date must look like 2026-01-01, or be empty for no cutoff.")
    if "POLL_SECONDS" in v and v["POLL_SECONDS"] < 30:
        raise SettingsError("Checking more often than every 30 seconds is not useful.")
    if v.get("OLLAMA_URL") and not v["OLLAMA_URL"].startswith(("http://", "https://")):
        raise SettingsError("The model address must start with http:// or https://")
    if v.get("CLASSIFIER", "ollama") not in ("ollama", "keywords"):
        raise SettingsError("Classifier must be ollama or keywords.")
    return v


def save(updates: dict) -> None:
    """Validate, apply to the running app and write to disk. An empty password means 'keep the current one'.

    Changing the mail server or user while leaving the password empty is refused: otherwise a saved password
    could be sent to a server you just typed in without you re-entering it."""
    clean = {k: updates[k] for k in updates if k in FIELDS}
    if "IMAP_PASSWORD" in clean and clean["IMAP_PASSWORD"] == "":
        del clean["IMAP_PASSWORD"]
        moved = ("IMAP_HOST" in clean and clean["IMAP_HOST"] != config.IMAP_HOST) or \
                ("IMAP_USER" in clean and clean["IMAP_USER"] != config.IMAP_USER)
        if moved and config.IMAP_PASSWORD:
            raise SettingsError("You changed the mail server or user, so please enter the password again.")
    merged = {**{k: getattr(config, k) for k in FIELDS}, **clean}
    validate(merged)
    for k, val in clean.items():
        setattr(config, k, FIELDS[k](val))
    stored = {k: getattr(config, k) for k in FIELDS}
    config.ensure_dirs()
    p = path()
    p.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)  # owner only; a no-op on Windows
    except OSError:
        pass
