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

#: Fixtures for the two places a form can hide: a same-origin child frame
#: and an open shadow root. Deliberately not in `ATS_FIXTURES` — they are
#: not ATS-detection fixtures and the stub extension's gap contract knows
#: nothing about them. They exist so the browser suite can drive the paths
#: where a scanned control and a submitted form are not in the top document.
NESTED_FIXTURES = ("iframe_host", "iframe_form", "shadow_form")

#: Fixtures whose confirmation-shaped text is *already* on the page and will
#: not hold still: a standing thank-you panel that counts, and one that is
#: rebuilt rather than edited. They exist so the browser suite can drive the
#: page a text-comparison freshness rule confirmed on every poll.
LIVE_REGION_FIXTURES = ("live_status",)


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
