#!/usr/bin/env bash
# Installs the tracker as a systemd service on the laptop (inside WSL2). Run it from the app folder:
#   bash deploy/install.sh
# DRY_RUN=1 bash deploy/install.sh   prints what it would install without changing anything.
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
ENVFILE="${XDG_CONFIG_HOME:-$HOME/.config}/application-tracker.env"
DATADIR="$HOME/.local/share/application-tracker"
BACKUPDIR="$HOME/backups/application-tracker"

render() {
  sed -e "s|__USER__|$USER_NAME|g" -e "s|__APPDIR__|$APPDIR|g" \
      -e "s|__ENVFILE__|$ENVFILE|g" -e "s|__BACKUPDIR__|$BACKUPDIR|g" "$1"
}

if [ "${DRY_RUN:-0}" = "1" ]; then
  for f in application-tracker.service application-tracker-backup.service application-tracker-backup.timer; do
    echo "=== $f"; render "$APPDIR/deploy/$f"
  done
  exit 0
fi

case "$APPDIR" in
  /mnt/*) echo "Copy the app folder into your Linux home first (for example ~/application-tracker)."
          echo "Running from /mnt/c is slow and SQLite locking misbehaves there."; exit 1;;
esac
if [ ! -d /run/systemd/system ]; then
  echo "systemd is not running in this WSL. Add these two lines to /etc/wsl.conf, then run 'wsl --shutdown' in Windows:"
  echo "  [boot]"; echo "  systemd=true"; exit 1
fi

echo "Creating the Python environment..."
python3 -m venv "$APPDIR/.venv"
"$APPDIR/.venv/bin/pip" install -q -r "$APPDIR/requirements.txt"

mkdir -p "$(dirname "$ENVFILE")" "$DATADIR" "$BACKUPDIR"
if [ ! -f "$ENVFILE" ]; then
  cp "$APPDIR/deploy/env.example" "$ENVFILE"
  echo "TRACKER_DATA=$DATADIR" >> "$ENVFILE"
  chmod 600 "$ENVFILE"
  echo "Created $ENVFILE. Edit it now (IMAP_USER, IMAP_PASSWORD, OLLAMA_URL), then start the service."
else
  echo "Keeping your existing $ENVFILE"
fi

render "$APPDIR/deploy/application-tracker.service"        | sudo tee /etc/systemd/system/application-tracker.service        >/dev/null
render "$APPDIR/deploy/application-tracker-backup.service" | sudo tee /etc/systemd/system/application-tracker-backup.service >/dev/null
render "$APPDIR/deploy/application-tracker-backup.timer"   | sudo tee /etc/systemd/system/application-tracker-backup.timer   >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable application-tracker.service application-tracker-backup.timer

echo
echo "Installed. Next:"
echo "  1. nano $ENVFILE"
echo "  2. sudo systemctl start application-tracker application-tracker-backup.timer"
echo "  3. journalctl -u application-tracker -f      (watch it work)"
