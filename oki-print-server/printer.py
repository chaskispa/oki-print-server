"""Raw printer access and Epson-compatible control sequences."""

from __future__ import annotations

import os


ESC = b"\x1b"


def initialize_printer() -> bytes:
    """Return the Epson ESC/P initialize/reset command (ESC @)."""
    return ESC + b"@"


def condensed_mode(enabled: bool = True) -> bytes:
    """Return condensed mode on (SI) or off (DC2)."""
    return b"\x0f" if enabled else b"\x12"


def emphasized_mode(enabled: bool = True) -> bytes:
    """Return emphasized/bold mode on (ESC E) or off (ESC F)."""
    return ESC + (b"E" if enabled else b"F")


def form_feed() -> bytes:
    """Return a form-feed command."""
    return b"\x0c"


def line_feed(count: int = 1) -> bytes:
    """Return CR+LF pairs so the next line begins at the left margin."""
    if count < 0:
        raise ValueError("line-feed count cannot be negative")
    return b"\r\n" * count


def normalize_newlines(text: str) -> str:
    """Normalize CRLF, LF, or CR line endings to printer-friendly CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


class PrinterError(OSError):
    """An error while opening or writing the printer device."""


class Printer:
    """A raw character-device printer backend.

    A new file descriptor is used for each job. This makes unplug/replug
    recovery automatic and prevents a stale descriptor from being reused.
    """

    def __init__(
        self,
        device: str = "/dev/usb/lp0",
        encoding: str = "cp437",
        trailing_lines: int = 2,
        append_form_feed: bool = False,
    ) -> None:
        self.device = device
        self.encoding = encoding
        self.trailing_lines = trailing_lines
        self.append_form_feed = append_form_feed

    def prepare(self, text: str) -> bytes:
        body = normalize_newlines(text).encode(self.encoding, errors="replace")
        ending = form_feed() if self.append_form_feed else line_feed(self.trailing_lines)
        return body + ending

    def write_bytes(self, data: bytes) -> None:
        fd = None
        try:
            fd = os.open(self.device, os.O_WRONLY | os.O_CLOEXEC)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise PrinterError("printer write returned zero bytes")
                view = view[written:]
        except OSError as exc:
            if isinstance(exc, PrinterError):
                raise
            raise PrinterError(exc.errno, f"{self.device}: {exc.strerror}") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def print_text(self, text: str) -> int:
        data = self.prepare(text)
        self.write_bytes(data)
        return len(data)
