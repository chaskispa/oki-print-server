#!/bin/sh

section() {
    echo
    echo "===== $1 ====="
}

section "USB devices (lsusb)"
lsusb 2>&1 || true

section "USB printer device nodes"
ls -l /dev/usb/ 2>&1 || true

section "usblp module"
lsmod 2>&1 | grep '^usblp' || echo "usblp is not loaded"
modinfo usblp 2>&1 | head -20 || true

section "Recent kernel messages"
dmesg 2>&1 | tail -50 || true

section "Service status"
systemctl status oki-print-server --no-pager 2>&1 || true

section "Recent service log"
journalctl -u oki-print-server -n 50 --no-pager 2>&1 || true

section "Local web health"
if command -v curl >/dev/null 2>&1; then
    curl --silent --show-error --max-time 3 http://127.0.0.1:8080/healthz || true
    echo
else
    echo "curl is not installed"
fi

section "Device ownership expected by service"
id oki-print-server 2>&1 || true
