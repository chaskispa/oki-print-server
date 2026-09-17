"""Small standard-library web control panel for the print server."""

from __future__ import annotations

import html
import json
import logging
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlparse


LOGGER = logging.getLogger("oki-print-server.web")


def _format_time(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "Never"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_uptime(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def render_page(status: Dict[str, Any], csrf_token: str, notice: str = "") -> bytes:
    queue_info = status["queue"]
    printer = status["printer"]
    counts = status["counts"]
    current = status["current_job"]
    last = status["last_result"]

    if printer["device_exists"] and printer["device_writable"]:
        printer_state = "Ready"
        state_class = "good"
    elif printer["device_exists"]:
        printer_state = "Permission denied"
        state_class = "bad"
    else:
        printer_state = "Disconnected"
        state_class = "bad"

    current_text = "Idle"
    if current:
        current_text = f"{current['description']} from {current['sender']}"

    last_text = "No completed attempts"
    last_class = "muted"
    if last:
        last_text = f"{last['outcome']} — {last['description']} — {_format_time(last['timestamp'])}"
        if last.get("message"):
            last_text += f" — {last['message']}"
        last_class = "good" if last["outcome"] == "succeeded" else "bad"

    notice_html = ""
    if notice:
        notice_html = f'<div class="notice">{html.escape(notice)}</div>'

    token = html.escape(csrf_token, quote=True)
    device = html.escape(printer["device"])
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OKI Print Server</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ max-width: 920px; margin: 0 auto; padding: 24px; background: #111827; color: #e5e7eb; }}
    h1, h2 {{ color: #f9fafb; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .card {{ background: #1f2937; border: 1px solid #374151; border-radius: 10px; padding: 16px; }}
    .label {{ color: #9ca3af; font-size: .82rem; text-transform: uppercase; letter-spacing: .04em; }}
    .value {{ font-size: 1.15rem; margin-top: 5px; overflow-wrap: anywhere; }}
    .good {{ color: #6ee7b7; }} .bad {{ color: #fca5a5; }} .muted {{ color: #9ca3af; }}
    textarea {{ box-sizing: border-box; width: 100%; min-height: 170px; padding: 12px; font: 1rem monospace; }}
    button {{ border: 0; border-radius: 7px; padding: 10px 14px; margin: 4px 4px 4px 0; cursor: pointer; background: #2563eb; color: white; }}
    button.secondary {{ background: #4b5563; }} button.warning {{ background: #b45309; }}
    .notice {{ margin: 12px 0; padding: 12px; border-radius: 7px; background: #1e3a8a; }}
    code {{ color: #d1d5db; }}
    footer {{ margin-top: 28px; color: #6b7280; font-size: .85rem; text-align: center; }}
  </style>
</head>
<body>
  <h1>OKI Print Server</h1>
  {notice_html}
  <div class="grid">
    <div class="card"><div class="label">Printer</div><div class="value {state_class}">{printer_state}</div><code>{device}</code></div>
    <div class="card"><div class="label">Queue</div><div class="value">{queue_info['depth']} / {queue_info['capacity']}</div></div>
    <div class="card"><div class="label">Current job</div><div class="value">{html.escape(current_text)}</div></div>
    <div class="card"><div class="label">Uptime</div><div class="value">{_format_uptime(status['uptime_seconds'])}</div></div>
  </div>

  <h2>Print text</h2>
  <form method="post" action="/action">
    <input type="hidden" name="csrf" value="{token}">
    <input type="hidden" name="action" value="print">
    <textarea name="text" required maxlength="{status['web']['max_text_bytes']}" placeholder="Text to print"></textarea><br>
    <button type="submit">Queue print job</button>
  </form>

  <h2>Printer controls</h2>
  <div class="card">
    <form method="post" action="/action">
      <input type="hidden" name="csrf" value="{token}">
      <button class="secondary" name="action" value="line_feed">Feed 1 line</button>
      <button class="secondary" name="action" value="line_feed_2">Feed 2 lines</button>
      <button class="secondary" name="action" value="form_feed">Form feed</button>
      <button class="secondary" name="action" value="condensed_on">Condensed on</button>
      <button class="secondary" name="action" value="condensed_off">Condensed off</button>
      <button class="secondary" name="action" value="emphasized_on">Bold on</button>
      <button class="secondary" name="action" value="emphasized_off">Bold off</button>
      <button class="warning" name="action" value="initialize">Reset printer</button>
    </form>
  </div>

  <h2>Activity</h2>
  <div class="card">
    <p class="{last_class}">{html.escape(last_text)}</p>
    <p>Queued: {counts['enqueued']} &nbsp; Printed: {counts['succeeded']} &nbsp; Retry attempts: {counts['failed_attempts']} &nbsp; Dropped: {counts['dropped']}</p>
    <p class="muted">UDP {html.escape(status['udp']['host'])}:{status['udp']['port']} · Encoding {html.escape(printer['encoding'])} · <a href="/">Refresh status</a> · <a href="/healthz">JSON health</a></p>
  </div>
  <footer>Created by Chaski and Codex</footer>
</body>
</html>"""
    return page.encode("utf-8")


class WebControlServer:
    def __init__(self, controller: Any, config: Any) -> None:
        self.controller = controller
        self.config = config
        self.csrf_token = secrets.token_urlsafe(24)
        self._httpd = ThreadingHTTPServer((config.host, config.port), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="web-control",
            daemon=True,
        )

    def _handler_class(self) -> Any:
        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "OkiPrintControl/1.0"

            def log_message(self, message: str, *args: Any) -> None:
                LOGGER.info("client=%s " + message, self.client_address[0], *args)

            def _headers(self, status: int, content_type: str, length: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
                )
                self.end_headers()

            def _send_text(self, status: int, message: str) -> None:
                body = message.encode("utf-8")
                self._headers(status, "text/plain; charset=utf-8", len(body))
                self.wfile.write(body)

            def _redirect(self, message: str) -> None:
                location = "/?message=" + quote(message)
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/healthz":
                    snapshot = panel.controller.status_snapshot()
                    body = json.dumps(snapshot, sort_keys=True).encode("utf-8")
                    status = 200 if snapshot["healthy"] else 503
                    self._headers(status, "application/json; charset=utf-8", len(body))
                    self.wfile.write(body)
                    return
                if parsed.path == "/favicon.ico":
                    self._headers(204, "image/x-icon", 0)
                    return
                if parsed.path != "/":
                    self._send_text(404, "not found\n")
                    return
                notice = parse_qs(parsed.query).get("message", [""])[0]
                snapshot = panel.controller.status_snapshot()
                body = render_page(snapshot, panel.csrf_token, notice)
                self._headers(200, "text/html; charset=utf-8", len(body))
                self.wfile.write(body)

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/action":
                    self._send_text(404, "not found\n")
                    return
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("application/x-www-form-urlencoded"):
                    self._send_text(415, "form data required\n")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_text(400, "invalid content length\n")
                    return
                max_body = panel.config.max_text_bytes * 4 + 2048
                if length <= 0 or length > max_body:
                    self._send_text(413, "request too large\n")
                    return
                try:
                    raw = self.rfile.read(length).decode("utf-8")
                    fields = parse_qs(raw, encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, ValueError):
                    self._send_text(400, "invalid form data\n")
                    return
                token = fields.get("csrf", [""])[0]
                if not secrets.compare_digest(token, panel.csrf_token):
                    self._send_text(403, "invalid form token\n")
                    return

                action = fields.get("action", [""])[0]
                sender = "web:" + self.client_address[0]
                if action == "print":
                    text = fields.get("text", [""])[0]
                    if not text:
                        self._send_text(400, "text is required\n")
                        return
                    if len(text.encode("utf-8")) > panel.config.max_text_bytes:
                        self._send_text(413, "print text is too large\n")
                        return
                    accepted, message = panel.controller.enqueue_text(text, sender)
                else:
                    accepted, message = panel.controller.enqueue_control(action, sender)
                if not accepted:
                    self._send_text(503, message + "\n")
                    return
                self._redirect(message)

        return Handler

    def start(self) -> None:
        self._thread.start()
        LOGGER.info("web control listening host=%s port=%d", self.config.host, self.config.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)
        LOGGER.info("web control stopped")
