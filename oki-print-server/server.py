#!/usr/bin/env python3
"""Receive UTF-8 UDP datagrams and serialize them to a raw printer device."""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config import Config, ConfigError, DEFAULT_CONFIG_PATH, load_config
from printer import (
    Printer,
    PrinterError,
    condensed_mode,
    emphasized_mode,
    form_feed,
    initialize_printer,
    line_feed,
)
from web_control import WebControlServer


LOGGER = logging.getLogger("oki-print-server")


@dataclass(frozen=True)
class PrintJob:
    text: Optional[str]
    sender_ip: str
    control_bytes: Optional[bytes] = None
    description: str = "text"

    @property
    def text_length(self) -> int:
        return len(self.text) if self.text is not None else 0


class ServiceStatus:
    """Thread-safe, in-memory status for logs and the control page."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._current: Optional[Dict[str, Any]] = None
        self._last: Optional[Dict[str, Any]] = None
        self.enqueued = 0
        self.succeeded = 0
        self.failed_attempts = 0
        self.dropped = 0

    def job_enqueued(self) -> None:
        with self._lock:
            self.enqueued += 1

    def job_dropped(self) -> None:
        with self._lock:
            self.dropped += 1

    def job_started(self, job: PrintJob) -> None:
        with self._lock:
            self._current = {
                "description": job.description,
                "sender": job.sender_ip,
                "text_length": job.text_length,
                "started_at": time.time(),
            }

    def job_failed(self, job: PrintJob, error: str) -> None:
        with self._lock:
            self.failed_attempts += 1
            self._last = {
                "outcome": "retrying",
                "description": job.description,
                "sender": job.sender_ip,
                "text_length": job.text_length,
                "message": error,
                "timestamp": time.time(),
            }

    def job_succeeded(self, job: PrintJob, byte_count: int) -> None:
        with self._lock:
            self.succeeded += 1
            self._last = {
                "outcome": "succeeded",
                "description": job.description,
                "sender": job.sender_ip,
                "text_length": job.text_length,
                "bytes_written": byte_count,
                "timestamp": time.time(),
            }
            self._current = None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": int(time.time() - self.started_at),
                "current_job": dict(self._current) if self._current else None,
                "last_result": dict(self._last) if self._last else None,
                "counts": {
                    "enqueued": self.enqueued,
                    "succeeded": self.succeeded,
                    "failed_attempts": self.failed_attempts,
                    "dropped": self.dropped,
                },
            }


class PrintWorker(threading.Thread):
    def __init__(
        self,
        jobs: "queue.Queue[PrintJob]",
        printer: Printer,
        stop_event: threading.Event,
        retry_seconds: float,
        status: ServiceStatus,
    ) -> None:
        # Daemon status is a final safety valve if a kernel device write stays
        # blocked during shutdown. Normal shutdown still joins this thread.
        super().__init__(name="printer-worker", daemon=True)
        self.jobs = jobs
        self.printer = printer
        self.stop_event = stop_event
        self.retry_seconds = retry_seconds
        self.status = status

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.status.job_started(job)
                self._print_with_retry(job)
            finally:
                self.jobs.task_done()

    def _print_with_retry(self, job: PrintJob) -> None:
        while not self.stop_event.is_set():
            try:
                if job.control_bytes is not None:
                    self.printer.write_bytes(job.control_bytes)
                    byte_count = len(job.control_bytes)
                else:
                    assert job.text is not None
                    byte_count = self.printer.print_text(job.text)
            except (PrinterError, OSError) as exc:
                self.status.job_failed(job, str(exc))
                LOGGER.warning(
                    "print failed; will retry sender=%s job=%s text_length=%d error=%s",
                    job.sender_ip,
                    job.description,
                    job.text_length,
                    exc,
                )
                self.stop_event.wait(self.retry_seconds)
                continue
            except Exception:
                self.status.job_failed(job, "unexpected print error")
                LOGGER.exception(
                    "unexpected print error; will retry sender=%s job=%s text_length=%d",
                    job.sender_ip,
                    job.description,
                    job.text_length,
                )
                self.stop_event.wait(self.retry_seconds)
                continue

            self.status.job_succeeded(job, byte_count)
            LOGGER.info(
                "print succeeded sender=%s job=%s text_length=%d bytes_written=%d",
                job.sender_ip,
                job.description,
                job.text_length,
                byte_count,
            )
            return


class PrintServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.jobs: "queue.Queue[PrintJob]" = queue.Queue(config.queue.max_jobs)
        self.status = ServiceStatus()
        self.printer = Printer(
            device=config.printer.device,
            encoding=config.printer.encoding,
            trailing_lines=config.printer.trailing_lines,
            append_form_feed=config.printer.form_feed,
        )
        self.worker = PrintWorker(
            self.jobs,
            self.printer,
            self.stop_event,
            config.printer.retry_seconds,
            self.status,
        )
        self.sock: Optional[socket.socket] = None
        self.web: Optional[WebControlServer] = None

    def request_stop(self, signum: int, _frame: object) -> None:
        LOGGER.info("shutdown requested signal=%s", signal.Signals(signum).name)
        self.stop_event.set()

    def _receive(self) -> Tuple[bytes, Tuple[str, int]]:
        assert self.sock is not None
        limit = self.config.network.max_packet_bytes
        return self.sock.recvfrom(limit + 1)

    def serve(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.worker.start()

        try:
            if self.config.web.enabled:
                self.web = WebControlServer(self, self.config.web)
                self.web.start()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                self.sock = sock
                sock.settimeout(1.0)
                sock.bind((self.config.network.host, self.config.network.port))
                LOGGER.info(
                    "listening host=%s port=%d device=%s queue_capacity=%d",
                    self.config.network.host,
                    self.config.network.port,
                    self.config.printer.device,
                    self.config.queue.max_jobs,
                )
                while not self.stop_event.is_set():
                    try:
                        packet, address = self._receive()
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if self.stop_event.is_set():
                            break
                        LOGGER.error("UDP receive failed error=%s", exc)
                        continue
                    self._accept_packet(packet, address[0])
        finally:
            self.sock = None
            self.stop_event.set()
            if self.web is not None:
                self.web.stop()
            self.worker.join(timeout=self.config.printer.retry_seconds + 2.0)
            if self.worker.is_alive():
                LOGGER.warning("printer worker did not stop promptly")
            LOGGER.info("server stopped queued_jobs=%d", self.jobs.qsize())

    def _accept_packet(self, packet: bytes, sender_ip: str) -> None:
        limit = self.config.network.max_packet_bytes
        if len(packet) > limit:
            LOGGER.warning(
                "packet dropped: too large sender=%s bytes_received_at_least=%d limit=%d",
                sender_ip,
                len(packet),
                limit,
            )
            return

        packet = packet.rstrip(b"\x00")
        try:
            text = packet.decode("utf-8")
        except UnicodeDecodeError as exc:
            LOGGER.warning(
                "packet dropped: invalid UTF-8 sender=%s bytes=%d error=%s",
                sender_ip,
                len(packet),
                exc,
            )
            return

        self.enqueue_text(text, sender_ip)

    def enqueue_text(self, text: str, sender_ip: str) -> Tuple[bool, str]:
        job = PrintJob(text=text, sender_ip=sender_ip)
        return self._enqueue(job)

    def enqueue_control(self, name: str, sender_ip: str) -> Tuple[bool, str]:
        commands = {
            "initialize": initialize_printer(),
            "form_feed": form_feed(),
            "line_feed": line_feed(1),
            "line_feed_2": line_feed(2),
            "condensed_on": condensed_mode(True),
            "condensed_off": condensed_mode(False),
            "emphasized_on": emphasized_mode(True),
            "emphasized_off": emphasized_mode(False),
        }
        data = commands.get(name)
        if data is None:
            return False, "unknown printer command"
        return self._enqueue(
            PrintJob(
                text=None,
                sender_ip=sender_ip,
                control_bytes=data,
                description=name,
            )
        )

    def _enqueue(self, job: PrintJob) -> Tuple[bool, str]:
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            self.status.job_dropped()
            LOGGER.error(
                "job dropped: queue full sender=%s job=%s text_length=%d capacity=%d",
                job.sender_ip,
                job.description,
                job.text_length,
                self.config.queue.max_jobs,
            )
            return False, "print queue is full"
        self.status.job_enqueued()
        LOGGER.info(
            "job queued sender=%s job=%s text_length=%d queue_size=%d",
            job.sender_ip,
            job.description,
            job.text_length,
            self.jobs.qsize(),
        )
        return True, "job queued"

    def status_snapshot(self) -> Dict[str, Any]:
        snapshot = self.status.snapshot()
        snapshot.update(
            {
                "healthy": self.worker.is_alive() and not self.stop_event.is_set(),
                "queue": {
                    "depth": self.jobs.qsize(),
                    "capacity": self.config.queue.max_jobs,
                },
                "printer": {
                    "device": self.config.printer.device,
                    "device_exists": os.path.exists(self.config.printer.device),
                    "device_writable": os.access(self.config.printer.device, os.W_OK),
                    "encoding": self.config.printer.encoding,
                },
                "udp": {
                    "host": self.config.network.host,
                    "port": self.config.network.port,
                },
                "web": {
                    "host": self.config.web.host,
                    "port": self.config.web.port,
                    "max_text_bytes": self.config.web.max_text_bytes,
                },
            }
        )
        return snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        LOGGER.error("configuration error: %s", exc)
        return 2

    logging.basicConfig(
        level=config.logging.level.upper(),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    try:
        PrintServer(config).serve()
    except OSError as exc:
        LOGGER.error("server stopped by network error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
