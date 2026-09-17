#!/usr/bin/env python3
"""Send one UTF-8 print job to an OKI UDP print server."""

from __future__ import annotations

import argparse
import socket
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Raspberry Pi hostname or IP address")
    parser.add_argument(
        "text",
        nargs="*",
        help="text to print; if omitted, read the complete job from stdin",
    )
    parser.add_argument("--port", type=int, default=5005, help="UDP port (default: 5005)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        print("error: port must be from 1 to 65535", file=sys.stderr)
        return 2

    if args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        print("error: provide text as arguments or pipe it on stdin", file=sys.stderr)
        return 2

    payload = text.encode("utf-8")
    if len(payload) > 8192:
        print(f"error: UTF-8 payload is {len(payload)} bytes; maximum is 8192", file=sys.stderr)
        return 2

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sent = sock.sendto(payload, (args.host, args.port))
    except OSError as exc:
        print(f"error: could not send job: {exc}", file=sys.stderr)
        return 1

    print(f"sent {sent} bytes to {args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
