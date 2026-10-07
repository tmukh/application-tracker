# Setup, step by step

Three stages. Do them in order and do not move on until the stage works. Each stage has a "why" so you can explain the choice later.

## What runs where

| Machine | Runs | Why there |
|---|---|---|
| Desktop (Windows, GPU) | Ollama, Proton Bridge (stage 1 only) | The GPU is here. The model is the only heavy part. |
| Laptop (WSL2, always on) | The tracker service, Proton Bridge (stage 3) | It never sleeps, so mail is collected around the clock. |
| Tailscale | Private link between the two | Ollama has no login, so it must never face the open network. |

Mail collection and classification are separate steps. If the desktop sleeps, mail piles up in a queue and is classified when the desktop is back. Nothing is lost and nothing falls back to guessing.

## Stage 1: first run and test on the desktop (about 15 minutes)

The only thing you run in a terminal is the first start. Everything else happens in the dashboard.

1. Start Proton Bridge on the desktop and log in. In Bridge open your account and keep its username and generated password (not your Proton password) at hand.
2. Make sure Ollama is running and has a model: `ollama pull qwen2.5:7b`.
3. Double-click `run.bat`. It creates the environment, starts the app and keeps running. Open http://127.0.0.1:5055.
4. A banner asks you to open **Settings**. Fill in: server `127.0.0.1`, port `1143`, security STARTTLS, your Bridge username and password. Press **Connect and list my folders**, pick your label from the drop-down, then **Save settings**. Press **Connect and list models** and pick a model. Set the cutoff date if you want a different one.
5. Mail starts loading at once. Open Review for the few mails that need a decision.

Your password is saved in `data/settings.json` (not in the database, so backups never contain it) and is never shown again in the browser. No `.env` file is needed.

Bridge uses a self-signed certificate on 127.0.0.1. The app accepts that only for loopback. For any other host it verifies normally.

## Stage 2: let the laptop reach Ollama on the desktop

Why: Ollama has no authentication. We expose it only to your Tailscale network, never to your LAN or the internet.

1. Install Tailscale on both machines and log in to the same account. Note the desktop's tailnet name (or its 100.x address) in the Tailscale app.
2. Desktop, PowerShell as administrator:
   - `setx OLLAMA_HOST 0.0.0.0` then quit and restart Ollama from the tray. Binding to all interfaces is needed because Tailscale is a separate interface.
   - `New-NetFirewallRule -DisplayName "Ollama tailnet only" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow`
   - Check `Get-NetFirewallRule -DisplayName "*ollama*"`. Ollama's installer may have created a broader rule. If so, disable that one, otherwise the scope above is pointless.
3. Laptop, inside WSL: `curl http://<desktop-tailnet-name>:11434/api/tags`. It must list your model.

## Stage 3: run it on the laptop as a service

Why a service: it starts at boot, restarts after a crash, and logs to journald.

1. WSL needs systemd. In WSL check `ls /run/systemd/system`. If it is missing, add `[boot]` and `systemd=true` to `/etc/wsl.conf`, then `wsl --shutdown` in Windows and reopen.
2. Copy the app folder into the Linux home (`git clone <your repo URL> ~/application-tracker` (or `cp -r` the folder from Windows under /mnt/c)). Do not run it from /mnt/c: SQLite file locking is unreliable there.
3. Proton Bridge on the laptop. Two options:
   - Option A (recommended): Linux Bridge in WSL. Install the .deb from Proton's download page, plus `sudo apt install pass gnupg2`. Bridge needs a password store: create a gpg key without passphrase (`gpg --batch --passphrase '' --quick-gen-key bridge default default never`), then `pass init bridge`. Run `protonmail-bridge --cli`, use `login`, then `info` to see the IMAP password. Then `deploy/proton-bridge.service` can run it headless. I could not test this on a real WSL here, so check the binary path with `which protonmail-bridge` and read `journalctl -u proton-bridge` if it fails.
   - Option B (fallback): keep Bridge on the Windows laptop and switch WSL to mirrored networking (`networkingMode=mirrored` in `.wslconfig`, Windows 11 22H2+) so 127.0.0.1 works both ways. Risk: it can disturb k3s and Tailscale networking in WSL.
4. In the app folder: `bash deploy/install.sh`. (`DRY_RUN=1 bash deploy/install.sh` shows what it would do first.) It creates the venv, a protected env file `~/.config/application-tracker.env`, and the systemd units.
5. Nothing to edit: open the dashboard and use Settings, same as on the desktop (Ollama address `http://<desktop-tailnet-name>:11434`). The env file the installer creates is optional.
6. `sudo systemctl start application-tracker application-tracker-backup.timer`, then `journalctl -u application-tracker -f`.
7. Reach the dashboard from your devices: `tailscale serve --bg 5055` (check `tailscale serve --help`, syntax differs between versions). Use Serve, not Funnel: Serve is tailnet only, Funnel is public internet.
8. Move your data: stop the service on the desktop copy, copy `data/tracker.db` to `~/.local/share/application-tracker/`, start the service. Only one copy may run at a time, otherwise two databases drift apart.

## Daily operation

- Health: `curl http://127.0.0.1:5055/health` (200 = last cycle fine, 503 = see the error).
- Backups run daily into `~/backups/application-tracker`, newest 14 kept.
- Desktop asleep: the status line shows "N waiting for the model". It clears itself.

## Troubleshooting

- "not your Proton password": use the Bridge-generated password.
- "Is Proton Bridge running and logged in?": start Bridge, check host and port.
- Mailbox not found: Settings, press Connect and list my folders, and pick the folder from the list.
- Model unavailable: desktop asleep, Ollama closed, or firewall scope wrong. Mail stays queued.
- Wrong classifications: fix them on the Review page.

## Security checklist

- `.env` and the env file hold the Bridge password: chmod 600, never commit.
- Ollama reachable only from 100.64.0.0/10.
- Dashboard bound to 127.0.0.1, published only through Tailscale Serve.
