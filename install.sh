#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root: sudo ./install.sh" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR=/opt/oki-print-server
CONFIG_DIR=/etc/oki-print-server
SERVICE_FILE=/etc/systemd/system/oki-print-server.service
SERVICE_USER=oki-print-server

echo "Installing operating-system packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv usbutils kmod udev

if ! getent group lp >/dev/null 2>&1; then
    groupadd --system lp
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --gid lp "$SERVICE_USER"
fi

install -d -o root -g root -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/server.py" "$SCRIPT_DIR/printer.py" "$SCRIPT_DIR/config.py" \
    "$SCRIPT_DIR/web_control.py" \
    "$SCRIPT_DIR/config.example.toml" "$SCRIPT_DIR/README.md" \
    "$INSTALL_DIR/"
install -o root -g root -m 0755 \
    "$SCRIPT_DIR/send_test.py" "$SCRIPT_DIR/diagnose.sh" "$INSTALL_DIR/"
python3 -m venv "$INSTALL_DIR/venv"

if [ ! -e "$CONFIG_DIR/config.toml" ]; then
    install -o root -g root -m 0644 \
        "$SCRIPT_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
    echo "Created $CONFIG_DIR/config.toml"
else
    echo "Keeping existing $CONFIG_DIR/config.toml"
fi

install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/oki-print-server.service" "$SERVICE_FILE"

cat > /etc/modules-load.d/oki-print-server.conf <<'EOF'
usblp
EOF
cat > /etc/udev/rules.d/99-oki-print-server.rules <<'EOF'
SUBSYSTEM=="usb", KERNEL=="lp[0-9]*", GROUP="lp", MODE="0660"
EOF

modprobe usblp || echo "Warning: could not load usblp; run ./diagnose.sh after connecting the printer" >&2
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb || true

systemctl daemon-reload
systemctl enable oki-print-server.service
systemctl restart oki-print-server.service

echo
echo "OKI print server installed and started."
echo "Configuration: $CONFIG_DIR/config.toml"
echo "Logs: journalctl -u oki-print-server -f"
