## Mail source and deployment

The tracker reads your Proton label directly over IMAP through Proton Mail Bridge (paid plan) and keeps
running as a background worker: it polls, stores mail, and classifies it from a queue so an offline model
only delays things. Configuration is done in the dashboard; `.env` (see `.env.example`) is optional. Step-by-step install for the
desktop, the laptop and the service: see **SETUP.md**.

Everything is done in the dashboard (Settings page): connect mail, pick the folder and model, set the cutoff,
delete old data, download a backup, import an old spreadsheet. The only command you ever run is the first start
(`run.bat` on Windows, `python -m tracker` elsewhere). Advanced commands such as `sync`, `prune`, `backup` and
`imap-list` exist for scripts and the systemd timer but are never required.

# Application Tracker

Reads your application emails, works out what each one is (confirmation, interview, rejection ...),
links it to the right application and keeps a status table up to date. Everything runs on your own
computer: the emails, the database and the language model (Ollama) never leave it.

## Set up (Windows, about five minutes)

1. Install Python 3.11 or newer if you do not have it (`py --version` in a terminal shows it).
2. Make sure Ollama is running and has a model: `ollama list`. If it is empty, `ollama pull qwen2.5:7b`
   (a 7-8B model handles German and English mail well enough; bigger is better but slower).
3. Double-click `run.bat`. The first run creates a virtual environment and installs Flask, requests,
   tzdata and openpyxl. Then open http://127.0.0.1:5055.

To pin a model, set `OLLAMA_MODEL` before starting (for example `set OLLAMA_MODEL=qwen2.5:7b`).
Without it, the first model in `ollama list` is used.

## Getting your emails in (free Proton plan)

Proton's Bridge (the IMAP connector) needs a paid plan, so the app takes exported files instead:

1. In Proton Mail on the web, open your Bewerbung label, open a message, use the three-dot menu and
   look for **Export** (it saves an `.eml` file). As far as I know this works on free accounts, but I
   could not check it on yours.
2. Drag the `.eml` files onto the page ("Add emails"). `.mbox` files work too, if you have a bulk export.
3. Files you upload are read once and moved to `data/processed`. Re-uploading the same mail does nothing.

Do this after each batch of replies. It is a manual step; it is the price of not paying for Bridge.

## Bring in your old spreadsheet

```
.venv\Scripts\activate
python -m tracker import "..\job_search_tracker_v2.xlsx"
```

Imported rows are matched loosely, so a later email from the same company attaches to the imported row
instead of creating a duplicate.

## How it decides things, and why

* **Two opinions per email.** The local model returns structured JSON (type, company, role, interview
  time, one-line summary). A set of German/English keyword rules classifies the same email independently.
  If they disagree about something that matters (say the model says "follow-up" and the text says
  "we regret to inform you"), the email goes to the **Review** page instead of silently changing a status.
  Small local models are good, not perfect, and a wrongly marked rejection is worse than one extra click.
* **Thread first, then company.** A reply is linked through the email headers (`In-Reply-To`). If that
  fails, by company name, then by the sender's own domain. Recruiting platforms (Personio, Greenhouse,
  Join ...) are ignored for domain matching because they send for many employers.
* **A confirmation means a new application** unless the exact same job title is already tracked.
  This keeps "Cloud Engineer" and "Cloud Software Engineer" at the same company as two applications.
  If a reply could belong to several applications at one company, it is flagged for review.
* **Status is derived from the emails in time order.** Order of progress is applied, assessment,
  interview, offer. A rejection closes the application, and a later positive email reopens it.
* **If you change a status by hand, it is locked** and emails stop changing it, so your decision wins.
  Tick "Let emails update the status again" to undo that.
* **No answer for 14 days** is shown under "applied", because silence is usually a rejection that never came.

## Commands

| Command | Does |
|---|---|
| `python -m tracker` | start the dashboard |
| `python -m tracker sync` | read the inbox folder and classify, without a browser |
| `python -m tracker import file.xlsx` | import your old tracker |
| `python -m pytest` | run the tests (needs `pip install pytest`) |

## Where things are

```
tracker/ingest.py     reads .eml/.mbox into plain dicts
tracker/classify.py   Ollama classifier + keyword classifier
tracker/linker.py     which application an email belongs to, and the status rules
tracker/pipeline.py   the workflow that ties it together
tracker/app.py        the web dashboard
data/                 your database and emails (created on first run, keep it private)
```

## Limits you should know about

* The model prompt and the keyword rules were tested on invented emails, not on your real inbox.
  Upload ten real ones first and look at the Review page before trusting the table.
* Emails are never sent anywhere, but `data/tracker.db` contains their full text. Do not commit it.
* The dashboard only listens on 127.0.0.1. Do not change that to expose it to a network.
* If two emails from one company arrive and neither names the role, the app cannot tell the jobs apart
  and asks you.

## Your data stays on your machines

Mail is read-only (the app never sends, moves or deletes anything), classification runs on your own Ollama
model, and everything is stored in one local SQLite file. Nothing goes to a cloud service. The dashboard has
no login: it listens on 127.0.0.1 only, so expose it through something that authenticates, such as Tailscale
Serve, never to the open internet.

## Other mail providers

Any IMAP account works through the same settings. For Gmail (not tested by the author): turn on 2-step
verification, create an app password, then set `IMAP_HOST=imap.gmail.com`, `IMAP_PORT=993`,
`IMAP_SECURITY=ssl`, `IMAP_USER=you@gmail.com`, `IMAP_PASSWORD=<app password>` and `IMAP_MAILBOX=<label name>`.
Press "Connect and list my folders" in Settings to see the exact names.

## Date cutoff

`EARLIEST_DATE` (default 2026-01-01) is a hard cutoff: older mail is never stored. Change it in Settings. After
raising it, Settings shows how much is now older and a button to delete it (a backup is made first). Lowering
it makes the next check re-read the label from the start.

## Contributing / publishing checklist

Run `bash scripts/prepublish-check.sh` before every push: it fails if a database, mail file, spreadsheet or
filled-in `.env` is tracked by git.
