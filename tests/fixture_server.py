"""Loopback-only HTTP server for offline ATS HTML fixtures."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
ATS_DIR = FIXTURES_DIR / "ats"
ATS_FIXTURES = ("greenhouse", "lever", "workday", "unknown")


def _pick_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LoopbackFixtureHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _start_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    port = _pick_loopback_port()
    handler = partial(_LoopbackFixtureHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture
def fixture_server() -> Iterator[str]:
    """Yield a loopback-only base URL serving ATS HTML fixtures."""
    server, thread, base_url = _start_server()
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
