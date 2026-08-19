"""Prerequisites for the one test suite in this repository that needs Chrome.

Everything here answers a single question: *can this machine run a headed
Chromium with an unpacked extension, and if not, exactly why not?* A browser
test that silently does not run is worse than one that fails, and a skip
message reading "browser unavailable" sends the reader to a search engine
rather than to the missing thing. Each refusal below names the executable,
the environment variable, or the binary that would fix it.

The display is handled here too. The browser is always headed — that is a
project-wide decision, not a detail of these tests — so a machine with no X
display gets a virtual one from `Xvfb` if it has that, and a skip naming
`xvfb` if it does not.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fake_extension"

#: Where a browser is looked for, in order. The environment variable comes
#: first so a machine with an unusual Chrome can run these without editing
#: anything, and Playwright's own download comes before a system Chrome
#: because it is the one a `playwright install` was meant to provide.
CHROME_ENV_VARS: tuple[str, ...] = ("CHROME_EXECUTABLE", "JOB_APPLY_CHROME")
SYSTEM_CHROME_NAMES: tuple[str, ...] = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

_XVFB_SCREEN = "1280x1024x24"


@dataclass(frozen=True)
class BrowserPrerequisites:
    """Either an executable path, or the reason there is not one."""

    executable: str = ""
    skip_reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.executable)


#: Asks Playwright where its Chromium is, and prints only that.
_CHROMIUM_PATH_PROBE = """
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    print(playwright.chromium.executable_path)
"""


def _playwright_chromium() -> str:
    """The Chromium a `playwright install` would have put on this machine.

    Asked in a subprocess, because asking starts a driver. The sync API's
    connection is torn down by the interpreter rather than by a running
    event loop, so it leaves a cancelled task and an unretrieved
    `TargetClosedError` printed to stderr — which lands in the middle of
    every browser run, deterministically, and reads exactly like a browser
    test having crashed. In a child process that noise is the child's, and
    the child's output is ours to discard.
    """
    try:
        import playwright  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed is one of the answers
        return ""
    try:
        answered = subprocess.run(
            [sys.executable, "-c", _CHROMIUM_PATH_PROBE],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a driver that will not start is another
        return ""
    lines = [line.strip() for line in answered.stdout.splitlines() if line.strip()]
    path = lines[-1] if lines else ""
    return path if path and Path(path).exists() else ""


def find_browser() -> BrowserPrerequisites:
    """Resolve a browser to drive, or explain what to install."""
    try:
        import playwright  # noqa: F401
    except Exception:  # noqa: BLE001
        return BrowserPrerequisites(
            skip_reason=(
                "playwright is not importable, so no browser can be driven: "
                "pip install -r requirements.txt"
            )
        )

    for variable in CHROME_ENV_VARS:
        configured = os.environ.get(variable, "").strip()
        if configured:
            if Path(configured).exists():
                return BrowserPrerequisites(executable=configured)
            return BrowserPrerequisites(
                skip_reason=f"{variable}={configured!r} does not exist on this machine"
            )

    bundled = _playwright_chromium()
    if bundled:
        return BrowserPrerequisites(executable=bundled)

    for name in SYSTEM_CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return BrowserPrerequisites(executable=found)

    return BrowserPrerequisites(
        skip_reason=(
            "no browser was found: run `python -m playwright install chromium`, "
            "or set CHROME_EXECUTABLE to a Chrome or Chromium binary "
            f"(looked for {', '.join(SYSTEM_CHROME_NAMES)} on PATH)"
        )
    )


class VirtualDisplay:
    """An `Xvfb` for a machine with no display, or a no-op for one with.

    Started here rather than by wrapping `pytest` in `xvfb-run` so that the
    rest of the suite — which needs no display at all — is not affected, and
    so that a machine without `Xvfb` produces a named skip rather than a
    browser that fails to launch for reasons nobody can read.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self.display = ""
        self.skip_reason = ""

    def start(self) -> None:
        existing = os.environ.get("DISPLAY", "").strip()
        if existing:
            self.display = existing
            return

        binary = shutil.which("Xvfb")
        if binary is None:
            self.skip_reason = (
                "the browser is always headed and this machine has no DISPLAY: "
                "install xvfb (Debian/Ubuntu: apt-get install xvfb), or run the "
                "suite inside `xvfb-run -a`"
            )
            return

        number = self._free_display_number()
        self._process = subprocess.Popen(
            [binary, number, "-screen", "0", _XVFB_SCREEN, "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not self._wait_for_socket(number):
            self.stop()
            self.skip_reason = f"Xvfb was started on {number} and never came up"
            return
        self.display = number

    @staticmethod
    def _free_display_number() -> str:
        for candidate in range(90, 130):
            if not Path(f"/tmp/.X11-unix/X{candidate}").exists():
                return f":{candidate}"
        return ":99"

    @staticmethod
    def _wait_for_socket(number: str, timeout_s: float = 10.0) -> bool:
        socket_path = Path(f"/tmp/.X11-unix/X{number.lstrip(':')}")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if socket_path.exists():
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - it ignored us
            self._process.kill()
        self._process = None


def launch_arguments(extension_dir: Path = EXTENSION_DIR) -> list[str]:
    """The flags that load the unpacked MV3 stub and nothing else.

    `--disable-extensions-except` is what keeps this honest: whatever else
    is in the profile, the only extension running is the stub in this
    repository, so a passing run cannot be crediting a real Jobright
    installation.
    """
    path = str(extension_dir)
    return [f"--disable-extensions-except={path}", f"--load-extension={path}"]


#: Playwright's own defaults switch extensions off; a persistent context that
#: kept them would load the stub and then ignore it.
IGNORED_DEFAULT_ARGS: tuple[str, ...] = (
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
)


def loopback_only(url: str) -> str:
    """Refuse any URL that is not on this machine.

    These tests type into forms and click submit buttons. That is only ever
    acceptable against a socket bound to 127.0.0.1 here, and the check is
    cheap enough to make unconditionally.
    """
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    try:
        resolved = socket.gethostbyname(host)
    except OSError:  # pragma: no cover - a hostname that does not resolve
        resolved = ""
    if not resolved.startswith("127."):
        raise AssertionError(
            f"refusing to drive a browser against {url!r}: these tests submit "
            "forms, and only a loopback fixture may be submitted to"
        )
    return url
