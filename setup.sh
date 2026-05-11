#!/usr/bin/env bash
#
# setup.sh — first-time system setup for the repeater controller.
#
# Run as the user who will operate the repeater (not root):
#
#   bash setup.sh            # packages, udev, group, venv
#   bash setup.sh --service  # same, plus install and enable systemd service
#
# sudo is used internally only for: apt, udev, usermod, and systemd/service
# file installation.  Everything else runs as $USER in the project directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC_USER="$USER"
RC_DIR="$SCRIPT_DIR"
INSTALL_SERVICE=false
NEED_RELOGIN=false

# ── argument parsing ──────────────────────────────────────────────────────────

for arg in "$@"; do
    case "$arg" in
        --service)
            INSTALL_SERVICE=true ;;
        --help|-h)
            cat <<'HELP'
Usage: bash setup.sh [--service]

Options:
  --service   Also generate and install the systemd service unit,
              enable it at boot, and print start/stop instructions.
              Omit this to set up for manual (non-service) operation.

What this script does:
  1. Installs system packages   (sudo apt)
  2. Installs the udev rule     (sudo cp + udevadm reload)
  3. Adds $USER to audio group  (sudo usermod)
  4. Creates Python venv and installs pip requirements  (no sudo)
  5. [--service] Generates and installs systemd unit    (sudo tee + systemctl)

After running, unplug and replug the CM119 interface, then open a new
terminal (or log out/in) so the audio group membership takes effect.
HELP
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg  (try --help)" >&2
            exit 1
            ;;
    esac
done

# ── sanity checks ─────────────────────────────────────────────────────────────

if [ "$EUID" -eq 0 ]; then
    echo "Error: run as the repeater operator user, not root." >&2
    echo "       sudo is invoked internally for privileged steps." >&2
    exit 1
fi

if [ ! -f "$RC_DIR/daemon.py" ]; then
    echo "Error: must be run from the rc project directory." >&2
    exit 1
fi

# Create repeater.toml from sample if it doesn't exist yet
if [ ! -f "$RC_DIR/repeater.toml" ]; then
    cp "$RC_DIR/repeater.toml.sample" "$RC_DIR/repeater.toml"
    echo "Created repeater.toml from repeater.toml.sample — edit it for your site before starting the daemon."
    echo ""
fi

# ── banner ────────────────────────────────────────────────────────────────────

cat <<INFO
=== Repeater Controller Setup ===
User:    $RC_USER
Project: $RC_DIR
Service: $INSTALL_SERVICE

INFO

# ── 1. System packages ────────────────────────────────────────────────────────

echo "--- System packages ---"
sudo apt-get install -y python3-dev python3-venv libportaudio2
echo ""

# ── 2. udev rule ──────────────────────────────────────────────────────────────

echo "--- udev rule ---"
sudo cp "$RC_DIR/udev/99-cm119.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
echo "Installed /etc/udev/rules.d/99-cm119.rules"
echo "Unplug and replug the CM119 after this script finishes to apply permissions."
echo ""

# ── 3. audio group ────────────────────────────────────────────────────────────

echo "--- audio group ---"
if id -nG "$RC_USER" | grep -qw audio; then
    echo "$RC_USER is already in the audio group."
else
    sudo usermod -aG audio "$RC_USER"
    echo "Added $RC_USER to the audio group."
    NEED_RELOGIN=true
fi
echo ""

# ── 4. Python venv ────────────────────────────────────────────────────────────

echo "--- Python venv ---"
if [ ! -d "$RC_DIR/venv" ]; then
    python3 -m venv "$RC_DIR/venv"
    echo "Created venv at $RC_DIR/venv"
else
    echo "venv already exists at $RC_DIR/venv"
fi
"$RC_DIR/venv/bin/pip" install --quiet --upgrade pip
"$RC_DIR/venv/bin/pip" install -r "$RC_DIR/requirements.txt"
echo "pip requirements installed."
echo ""

# ── 5. systemd service (optional) ─────────────────────────────────────────────

if $INSTALL_SERVICE; then
    echo "--- systemd service ---"
    SERVICE_FILE="/etc/systemd/system/rc.service"

    sudo tee "$SERVICE_FILE" > /dev/null <<SERVICE
[Unit]
Description=Repeater Controller Daemon
After=network.target sound.target
Wants=sound.target

[Service]
Type=notify
User=$RC_USER
Group=audio
WorkingDirectory=$RC_DIR
ExecStart=$RC_DIR/venv/bin/python $RC_DIR/daemon.py $RC_DIR/repeater.toml
Restart=on-failure
RestartSec=5

# Reduce attack surface — daemon only needs audio and USB HID
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$RC_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICE

    sudo systemctl daemon-reload
    sudo systemctl enable rc
    echo "Service installed at $SERVICE_FILE and enabled at boot."
    echo ""
fi

# ── summary ───────────────────────────────────────────────────────────────────

echo "=== Setup complete ==="
echo ""

if $NEED_RELOGIN; then
    echo "IMPORTANT: Log out and open a new session (or run 'newgrp audio') for"
    echo "           the audio group change to take effect in your current shell."
    echo ""
fi

echo "After replugging the CM119 and refreshing your session:"
echo ""
if $INSTALL_SERVICE; then
    echo "  Start service:   sudo systemctl start rc"
    echo "  Check status:    sudo systemctl status rc"
    echo "  View logs:       journalctl -u rc -f"
    echo "  Connect shell:   $RC_DIR/venv/bin/python $RC_DIR/shell.py $RC_DIR/repeater.toml"
else
    echo "  Start daemon:    source $RC_DIR/venv/bin/activate"
    echo "                   python $RC_DIR/daemon.py $RC_DIR/repeater.toml"
    echo ""
    echo "  Connect shell:   python $RC_DIR/shell.py $RC_DIR/repeater.toml"
fi
echo ""
echo "  Socket:          $RC_DIR/run/rc.sock"
