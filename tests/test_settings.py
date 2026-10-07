import json

import pytest

from conftest import make_eml
from test_imap import imap  # noqa: F401  (fixture: fake mail server)
from test_pipeline import fake_ollama  # noqa: F401  (fixture: fake Ollama)
from tracker import config, db, imap_source, pipeline, settings
from tracker.app import app


@pytest.fixture(autouse=True)
def restore_config(monkeypatch):
    for k in settings.FIELDS:  # settings.save() sets config values; put them back after every test
        monkeypatch.setattr(config, k, getattr(config, k))


@pytest.fixture
def client():
    return app.test_client()


def test_save_applies_now_persists_and_never_shows_the_password(client):
    r = client.post("/settings", data={"IMAP_HOST": "127.0.0.1", "IMAP_PORT": "1143", "IMAP_SECURITY": "starttls", "IMAP_USER": "me@proton.me",
                                       "IMAP_PASSWORD": "super-secret-pw", "IMAP_MAILBOX": "Labels/X", "EARLIEST_DATE": "2026-02-01"}, follow_redirects=True)
    assert r.status_code == 200 and b"Saved" in r.data
    assert config.IMAP_USER == "me@proton.me" and config.IMAP_PASSWORD == "super-secret-pw" and config.EARLIEST_DATE == "2026-02-01"
    page = client.get("/settings").data
    assert b"super-secret-pw" not in page and b"me@proton.me" in page and b"leave empty to keep it" in page
    on_disk = json.loads(settings.path().read_text())
    assert on_disk["IMAP_PASSWORD"] == "super-secret-pw"
    # a restart: reset the config, load from disk
    config.IMAP_PASSWORD = ""
    settings.load()
    assert config.IMAP_PASSWORD == "super-secret-pw"


def test_empty_password_keeps_the_old_one_unless_the_server_changes(client):
    settings.save({"IMAP_HOST": "127.0.0.1", "IMAP_USER": "a@b.c", "IMAP_PASSWORD": "pw"})
    settings.save({"IMAP_MAILBOX": "Labels/Y", "IMAP_PASSWORD": ""})
    assert config.IMAP_PASSWORD == "pw"
    with pytest.raises(settings.SettingsError, match="enter the password again"):
        settings.save({"IMAP_HOST": "evil.example.com", "IMAP_SECURITY": "ssl", "IMAP_PASSWORD": ""})
    assert config.IMAP_HOST == "127.0.0.1"  # nothing was applied


@pytest.mark.parametrize("bad", [{"IMAP_PORT": 0}, {"IMAP_SECURITY": "plain"}, {"EARLIEST_DATE": "last year"},
                                 {"POLL_SECONDS": 5}, {"OLLAMA_URL": "localhost:11434"}, {"CLASSIFIER": "magic"},
                                 {"IMAP_SECURITY": "none", "IMAP_HOST": "mail.example.com"}])
def test_invalid_values_are_refused_with_a_message(bad):
    before = settings.current(include_secret=True)
    with pytest.raises(settings.SettingsError):
        settings.save(bad)
    assert settings.current(include_secret=True) == before


def test_other_websites_cannot_post_to_the_dashboard(client):
    r = client.post("/settings", data={"IMAP_HOST": "evil.example.com"}, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403 and config.IMAP_HOST != "evil.example.com"
    ok = client.post("/settings", data={"POLL_SECONDS": "600"}, headers={"Origin": "http://localhost"})
    assert ok.status_code == 302 and config.POLL_SECONDS == 600


def test_connect_button_saves_and_lists_mailboxes(client, imap):
    d = client.post("/settings/mailboxes", data={"IMAP_HOST": "127.0.0.1", "IMAP_USER": "me@proton.me", "IMAP_PASSWORD": "bridge-pass"}).get_json()
    assert d["ok"] and "Labels/Bewerbung" in d["mailboxes"]
    bad = client.post("/settings/mailboxes", data={"IMAP_PASSWORD": "wrong"}).get_json()
    assert not bad["ok"] and "password" in bad["error"].lower()


def test_models_button_lists_installed_models(client, fake_ollama):
    d = client.post("/settings/models", data={"OLLAMA_URL": fake_ollama}).get_json()
    assert d == {"ok": True, "models": ["qwen2.5:7b"]}
    gone = client.post("/settings/models", data={"OLLAMA_URL": "http://127.0.0.1:9"}).get_json()
    assert not gone["ok"] and "Cannot reach Ollama" in gone["error"]


def test_prune_button_needs_confirmation_and_makes_a_backup(client):
    make_eml("Old <jobs@old.io>", "Your application to Old", "We have received your application.", "2025-05-01", msgid="o")
    make_eml("New <jobs@new.io>", "Your application to New", "We have received your application.", "2026-05-01", msgid="n")
    pipeline.sync(clf=None)
    settings.save({"EARLIEST_DATE": "2026-01-01"})  # raised after the fact: the old mail is still stored
    page = client.get("/settings").data
    assert b"1 emails" in page and b"Delete data from before 2026-01-01" in page
    assert client.post("/settings/prune", data={}).status_code == 400
    r = client.post("/settings/prune", data={"confirm": "yes"}, follow_redirects=True)
    assert b"Deleted 1 emails and 1 applications" in r.data
    assert [x["company"] for x in db.connect().execute("SELECT company FROM applications")] == ["New"]
    assert list((config.DATA / "backups").glob("tracker-*.db"))


def test_backup_download_has_no_password(client):
    settings.save({"IMAP_HOST": "127.0.0.1", "IMAP_USER": "a@b.c", "IMAP_PASSWORD": "pw-in-settings-only"})
    r = client.get("/settings/backup")
    assert r.status_code == 200 and b"SQLite format 3" in r.data[:20] and b"pw-in-settings-only" not in r.data


def test_import_button(client, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Tracker"
    ws.append(["Company", "Role / Job ID", "Date applied", "Status"])
    ws.append(["Acme", "SRE", "2026-03-01", "Applied"]); ws.append(["Ancient", "Dev", "2023-01-01", "Rejected"])
    path = tmp_path / "t.xlsx"; wb.save(path)
    settings.save({"EARLIEST_DATE": "2026-01-01"})
    with open(path, "rb") as fh:
        r = client.post("/settings/import", data={"sheet": (fh, "t.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 1 applications" in r.data


def test_welcome_banner_until_mail_is_connected(client):
    assert b"not connected yet" in client.get("/").data
    settings.save({"IMAP_HOST": "127.0.0.1", "IMAP_USER": "a@b.c", "IMAP_PASSWORD": "pw"})
    assert b"not connected yet" not in client.get("/").data
