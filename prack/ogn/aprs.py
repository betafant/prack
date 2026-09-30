"""Minimal APRS-IS client for the OGN network (read only)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .. import __version__
from ..db import utcnow

log = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 240
READ_TIMEOUT_SECONDS = 90  # servers send a comment line every ~20 s


@dataclass
class LinkStatus:
    state: str = "idle"  # idle | connecting | connected | error | demo
    server: str = ""
    connected_since: str | None = None
    lines: int = 0
    last_line_at: str | None = None
    last_error: str | None = None
    reconnects: int = 0


class AprsClient:
    def __init__(
        self,
        host: str,
        port: int,
        callsign: str,
        filter_expr: str,
        on_line: Callable[[str], None],
    ) -> None:
        self.host = host
        self.port = port
        self.callsign = callsign
        self.filter_expr = filter_expr
        self.on_line = on_line
        self.status = LinkStatus()
        self._sock: socket.socket | None = None

    def login_line(self) -> str:
        return f"user {self.callsign} pass -1 vers prack {__version__} filter {self.filter_expr}\r\n"

    def run(self, stop: threading.Event) -> None:
        backoff = 5.0
        while not stop.is_set():
            try:
                self._session(stop)
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001 - keep the feed alive whatever happens
                self.status.state = "error"
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("OGN connection problem: %s (retry in %.0fs)", exc, backoff)
            finally:
                self._close()
            if stop.wait(backoff):
                break
            self.status.reconnects += 1
            backoff = min(backoff * 2, 300.0)

    def stop(self) -> None:
        self._close()

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _session(self, stop: threading.Event) -> None:
        self.status.state = "connecting"
        log.info("Connecting to %s:%s filter=%s", self.host, self.port, self.filter_expr)
        sock = socket.create_connection((self.host, self.port), timeout=30)
        self._sock = sock
        sock.settimeout(READ_TIMEOUT_SECONDS)
        sock.sendall(self.login_line().encode("ascii"))
        self.status.state = "connected"
        self.status.connected_since = utcnow().isoformat() + "Z"
        self.status.last_error = None
        last_keepalive = time.monotonic()
        buffer = b""
        while not stop.is_set():
            if time.monotonic() - last_keepalive > KEEPALIVE_SECONDS:
                sock.sendall(b"#keepalive\r\n")
                last_keepalive = time.monotonic()
            chunk = sock.recv(16384)
            if not chunk:
                raise ConnectionError("server closed the connection")
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if text.startswith("#"):
                    if "aprsc" in text or "logresp" in text:
                        self.status.server = text[1:].strip()[:120]
                    continue
                self.status.lines += 1
                self.status.last_line_at = utcnow().isoformat() + "Z"
                try:
                    self.on_line(text)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to process line: %s", text)
