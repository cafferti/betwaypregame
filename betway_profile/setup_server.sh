#!/usr/bin/env bash
# One-time setup of the bot on an Ubuntu server (22.04 or 24.04, ARM or x86).
# Run as your normal user (NOT root) from inside the bot folder:
#     cd ~/betwaypregame && bash setup_server.sh
set -euo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(id -un)"

echo "==> Timezone: Africa/Lagos (so CSV times and the daily profit chart match your PC)"
sudo timedatectl set-timezone Africa/Lagos

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip xvfb x11vnc openbox

# Small server (under 3 GB RAM): add 2 GB swap so the browser is not killed
if [ "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" -lt 3000 ] && [ -z "$(swapon --show --noheadings)" ]; then
  echo "==> Small server: adding 2 GB swap"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Python environment and the Chromium browser"
python3 -m venv "$BOT_DIR/.venv"
"$BOT_DIR/.venv/bin/pip" install --upgrade pip
"$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt"
sudo "$BOT_DIR/.venv/bin/playwright" install-deps chromium
"$BOT_DIR/.venv/bin/playwright" install chromium

echo "==> Auto-start service (restarts the bot if it crashes or the server reboots)"
sudo tee /etc/systemd/system/betbot.service >/dev/null <<EOF
[Unit]
Description=Betway value bet bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$BOT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$BOT_DIR/.venv/bin/python -u $BOT_DIR/main.py
Restart=always
RestartSec=15
KillSignal=SIGINT
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable betbot

echo "==> Nightly backup (keeps 7 days in ~/backups)"
cat > "$BOT_DIR/backup.sh" <<'EOF'
#!/usr/bin/env bash
DEST="$HOME/backups"; mkdir -p "$DEST"
HERE="$(cd "$(dirname "$0")" && pwd)"
tar czf "$DEST/bot-$(date +%F).tgz" -C "$HERE" --exclude=.venv --exclude=__pycache__ .
ls -1t "$DEST"/bot-*.tgz | tail -n +8 | xargs -r rm --
EOF
chmod +x "$BOT_DIR/backup.sh"
( crontab -l 2>/dev/null | grep -v "backup.sh" || true; echo "0 3 * * * $BOT_DIR/backup.sh" ) | crontab -

echo "==> Helper for logging in to Betway on the server"
cat > "$BOT_DIR/login_desktop.sh" <<'EOF'
#!/usr/bin/env bash
# Starts a virtual screen you can watch through an SSH tunnel, then runs your Betway login script in it.
cd "$(dirname "$0")"
pkill -f "Xvfb :99" 2>/dev/null || true
pkill -f "x11vnc" 2>/dev/null || true
Xvfb :99 -screen 0 1366x800x24 &
sleep 1
export DISPLAY=:99
openbox &
x11vnc -display :99 -localhost -nopw -forever -quiet &
echo ">>> On your PC run:   ssh -L 5900:127.0.0.1:5900 <user>@<server-ip>"
echo ">>> then open a VNC viewer at 127.0.0.1:5900 and log in to Betway in the window you see."
.venv/bin/python launch_and_save_session.py
pkill -f "Xvfb :99" 2>/dev/null || true
pkill -f "x11vnc" 2>/dev/null || true
EOF
chmod +x "$BOT_DIR/login_desktop.sh"

cat <<MSG

==================== DONE — next steps ====================
1. Stop the bot on your PC (two bots would place every bet twice).
2. Log in to Betway on this server:      bash $BOT_DIR/login_desktop.sh
3. Start the bot:                        sudo systemctl start betbot
4. Watch it:                             journalctl -u betbot -f
5. Dashboard from your PC:   ssh -L 8765:127.0.0.1:8765 $RUN_USER@<server-ip>   then open http://127.0.0.1:8765
Stop:  sudo systemctl stop betbot      Restart: sudo systemctl restart betbot
When Betway logs you out (the bot says the token expired): stop the bot, repeat step 2, start it again.
MSG