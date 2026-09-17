# OKI UDP Print Server

A small, dependency-free Python daemon for a Raspberry Pi connected by USB to
an OKI Microline 320/321. Each UDP datagram is one print job. A small web
control panel can also submit text and printer commands. All jobs enter one
bounded FIFO and a single worker writes them serially to `/dev/usb/lp0`.

## Why direct USB

The Linux `usblp` kernel driver exposes USB Printer Class devices as
`/dev/usb/lpN`. A blocking write to that character device is the shortest path
from already-formatted text to the printer. OKI's ML320 specification lists a
USB 2.0 Full Speed interface and ESC/P emulation, and its command reference
confirms the control bytes used by this project. There is no rasterization or
desktop printing dependency here.

The device number can change if multiple USB printers are connected. This
project assumes a dedicated Pi with one USB printer. It opens the device anew
for every job, so unplugging and reconnecting the printer does not leave a
stale file descriptor. Failed jobs remain with the worker and retry every three
seconds by default.

Useful references:

- [Linux `usblp` driver source](https://github.com/torvalds/linux/blob/master/drivers/usb/class/usblp.c)
- [OKI ML320 hardware specifications](https://www.oki.com/tw/printing/products/dotmatrix/ml320t/specifications/)
- [OKI Microline 320 Turbo manuals](https://www.oki.com/us/printing/support/user-manual/dot-matrix-printers/62411601/)
- [CUPS `lpadmin` documentation](https://openprinting.github.io/cups/doc/man-lpadmin.html)

## Installation

On Raspberry Pi OS Lite:

```sh
git clone <repository-url>
cd rpi-okiprinter/oki-print-server
sudo ./install.sh
```

The installer:

- installs Python, venv support, `usbutils`, `kmod`, and `udev`;
- installs the application under `/opt/oki-print-server`;
- creates the unprivileged `oki-print-server` user in group `lp`;
- loads `usblp` and gives group `lp` access to USB printer device nodes;
- installs, enables, and starts the systemd unit.

It does not overwrite an existing `/etc/oki-print-server/config.toml`.

Check the service and follow its logs:

```sh
systemctl status oki-print-server
journalctl -u oki-print-server -f
```

## Configuration

The defaults work without a file. Installation copies this configuration to
`/etc/oki-print-server/config.toml`:

```toml
[network]
host = "0.0.0.0"
port = 5005
max_packet_bytes = 8192

[printer]
device = "/dev/usb/lp0"
encoding = "cp437"
trailing_lines = 2
form_feed = false
retry_seconds = 3.0

[queue]
max_jobs = 100

[web]
enabled = true
host = "0.0.0.0"
port = 8080
max_text_bytes = 8192

[logging]
level = "INFO"
```

After changing it, restart the service:

```sh
sudo systemctl restart oki-print-server
```

Incoming datagrams must be UTF-8. Only trailing NUL bytes are removed. CR, LF,
and CRLF line endings are converted to CR+LF; other spacing is preserved. Text
is encoded for the printer with replacement for unsupported characters. CP437
is the default because it is ASCII-compatible and common in Epson-compatible
printer modes. If accented characters do not match the printer's configured
character set, try `latin-1` and set the printer menu to the matching code page.

Two CR+LF pairs follow each job by default. Set `form_feed = true` to append a
form feed instead; it does not add both. The server never resets the printer
between jobs, so operator-selected and application-selected modes persist.

## Sending jobs

With netcat:

```sh
echo -n "HELLO WORLD" | nc -u <PI-IP> 5005
printf 'LINE 1\nLINE 2' | nc -u <PI-IP> 5005
```

With the included Python client:

```sh
python3 send_test.py 192.168.1.50 "TEST PRINT"
printf 'LINE 1\nLINE 2\n' | python3 send_test.py 192.168.1.50
python3 send_test.py --port 5005 192.168.1.50 "TEST PRINT"
```

UDP has no delivery acknowledgement. A successful sender message means the
datagram was handed to the network stack, not that paper was printed. Inspect
the server journal for `job queued`, `print succeeded`, retry, or dropped-job
messages. Restrict UDP port 5005 to a trusted LAN; the protocol has no
authentication or encryption.

## Direct printer test

Stop the daemon first so it does not own the device, then send raw text:

```sh
sudo systemctl stop oki-print-server
printf 'DIRECT TEST\r\n\r\n' | sudo tee /dev/usb/lp0 >/dev/null
sudo systemctl start oki-print-server
```

The reusable helpers in `printer.py` provide Epson-compatible bytes for reset
(`ESC @`), condensed on/off (`SI`/`DC2`), emphasized on/off (`ESC E`/`ESC F`),
form feed, and line feeds. They are not injected into ordinary jobs.

## Web control panel

Open this URL from another computer on the same LAN:

```text
http://<PI-IP>:8080/
```

The page shows printer-device availability, the current job, queue depth,
uptime, and success/retry/drop counters. It can enqueue text and these raw
ESC/P controls through the same FIFO used by UDP:

- feed one or two lines;
- form feed;
- condensed mode on/off;
- emphasized (bold) mode on/off;
- initialize/reset printer.

Machine-readable service status is available at:

```sh
curl http://<PI-IP>:8080/healthz
curl http://<PI-IP>:8080/api/status
```

Both URLs return the same JSON. `/api/status` is provided for control-panel and
monitoring-client compatibility.

Set `web.enabled = false` to disable HTTP, or bind `web.host` to `127.0.0.1`
to make it local-only. The page uses a per-process form token to prevent blind
cross-site submissions, but it has no user login. Like the UDP endpoint, it
must only be exposed to a trusted LAN. Do not forward ports 5005 or 8080 to the
internet.

## USB detection and troubleshooting

Run the diagnostic script as root so `dmesg` is readable:

```sh
sudo ./diagnose.sh
```

The focused checks are:

```sh
lsusb
ls -l /dev/usb/
lsmod | grep usblp
sudo modprobe usblp
dmesg | grep -i -E 'usblp|printer|usb'
udevadm info /dev/usb/lp0
```

After connection, `dmesg` should show `usblp0` and `/dev/usb/lp0` should be
owned by `root:lp` with group write permission. Verify the service account:

```sh
id oki-print-server
sudo -u oki-print-server test -w /dev/usb/lp0 && echo writable
```

If no node appears, load `usblp`, reconnect the printer, try another USB cable
or port, and check that `lsusb` sees the device. Some USB-to-parallel adapters
do not implement the USB Printer Class correctly; the printer's native USB
interface is preferable.

### CUPS conflicts and fallback

CUPS/libusb and `usblp` can compete for the same USB interface. On this
dedicated appliance, stop and disable CUPS before using direct device access:

```sh
sudo systemctl disable --now cups.service cups.socket cups.path
sudo systemctl restart oki-print-server
```

Only do this if the Pi is not also serving other CUPS printers. To restore it:

```sh
sudo systemctl enable --now cups.socket
```

If direct `usblp` access is unreliable on a particular USB adapter, CUPS can be
used as an alternative spooler. Install CUPS, discover the USB URI with
`lpinfo -v`, create a raw queue where supported, and submit printer-ready data:

```sh
sudo apt install cups
sudo lpinfo -v
sudo lpadmin -p oki -E -v 'usb://the-discovered-uri' -m raw
printf 'CUPS RAW TEST\r\n\r\n' | lp -d oki -o raw
```

Raw queues are deprecated by current CUPS/OpenPrinting, so this project does
not make CUPS its default backend. If CUPS owns the interface, unload or
blacklist `usblp` according to the CUPS backend's requirements.

CUPS also has its own browser interface, normally on port 631 when enabled and
configured for remote access. It manages CUPS queues, but it cannot display or
control this daemon's in-memory UDP queue. The built-in page on port 8080 is
therefore useful even when CUPS is installed.

## Reliability behavior

- UDP and web inputs share one printing thread; writes never overlap.
- FIFO capacity is 100 waiting jobs. New jobs are logged and dropped when full.
- The current job retries indefinitely while the device is missing or errors.
- Invalid UTF-8 and oversized datagrams are logged and dropped.
- SIGINT and SIGTERM stop reception and interrupt retry waits cleanly. A kernel
  device write already in progress gets a short grace period before systemd
  completes process shutdown.
- Queued jobs are in memory only and are lost on process restart or power loss.

Because a raw device write cannot prove that ink reached paper, an unplug or
error at exactly the end of a write is inherently ambiguous. Retrying protects
against loss but, in that narrow case, can produce a duplicate job.

## Credits

Created by Chaski and Codex.
