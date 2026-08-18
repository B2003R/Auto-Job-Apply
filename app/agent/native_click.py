"""Native (OS-level) toolbar click via Linux `xdotool`.

This is the last-resort Autofill trigger tier and the only part of the agent
that leaves the browser, so it is deliberately isolated here and kept
shell-free:

* commands are executed with `create_subprocess_exec` and an argument list —
  never a shell string, so nothing in a coordinate, window name, or path can
  be interpreted as shell syntax;
* the `xdotool` binary is resolved to an absolute path via `which` rather
  than relying on `PATH` resolution at exec time;
* every precondition (binary present, X display present, coordinates
  explicitly calibrated) is checked *before* anything runs, and reported as
  `NativeClickUnavailable` so callers can distinguish "not attempted" from
  "attempted and failed";
* the target Chrome window is activated **and the activation verified**
  before the pointer is moved, so a calibrated toolbar pixel can never be
  clicked while some other window happens to be focused;
* children inherit only `DISPLAY`/`XAUTHORITY`, not the agent's environment
  (which holds API keys);
* every command is bounded by a timeout, and a timed-out child is killed.

`runner`, `which`, and `environ` are injectable, so tests never need
xdotool, an X display, or a subprocess.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, NoReturn, Sequence

from app.agent.errors import NativeClickFailed, NativeClickUnavailable
from app.config import Settings

XDOTOOL_BINARY = "xdotool"
DEFAULT_TIMEOUT_MS = 5_000
LEFT_BUTTON = 1
#: Variables a child xdotool process actually needs to reach the X server.
X_ENV_VARS: tuple[str, ...] = ("DISPLAY", "XAUTHORITY")
_WINDOW_ID = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class CommandResult:
    """Exit status and captured output of one native command."""

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[..., Awaitable[CommandResult]]
Which = Callable[[str], str | None]


async def run_command(
    argv: Sequence[str], timeout_s: float, env: Mapping[str, str] | None = None
) -> CommandResult:
    """Execute `argv` directly (no shell) with `env` and capture its output.

    A child that outlives `timeout_s` is killed and reaped before the
    timeout is reported, so a hung `xdotool` cannot leak a process.
    """
    argv = list(argv)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        raise NativeClickFailed(argv, f"timed out after {timeout_s:g}s") from exc

    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def resolve_display(environ: Mapping[str, str]) -> str:
    """Return the X display or raise `NativeClickUnavailable`.

    `xdotool` drives X11 specifically: a session with only `WAYLAND_DISPLAY`
    set cannot be automated this way, so an empty/absent `DISPLAY` is a
    precondition failure rather than something to attempt and let fail.
    """
    display = environ.get("DISPLAY", "")
    if not display:
        _unavailable(
            "no X display is available (DISPLAY is unset or empty); a headed X "
            "session is required for the native toolbar click tier — under a bare "
            "Wayland session or a headless host, use xvfb-run or an earlier tier"
        )
    return display


def minimal_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The smallest environment an X client needs.

    Passing the agent's whole environment to a subprocess would hand model
    API keys and database paths to a process that only needs to talk to the
    X server.
    """
    return {name: environ[name] for name in X_ENV_VARS if environ.get(name)}


def _unavailable(reason: str) -> NoReturn:
    raise NativeClickUnavailable(reason)


def _validated_coordinate(name: str, value: Any) -> int:
    """Accept only explicitly calibrated positive integers.

    Booleans are rejected despite being `int` subclasses, and non-integers
    are rejected outright rather than coerced, so no unexpected text can
    ever reach the argument list.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        _unavailable(
            f"toolbar coordinate {name} must be a calibrated integer, got "
            f"{type(value).__name__} ({value!r}); run scripts/calibrate_toolbar.py"
        )
    if value <= 0:
        _unavailable(
            f"toolbar coordinates are not calibrated ({name}={value}); run "
            "scripts/calibrate_toolbar.py and set TOOLBAR_X/TOOLBAR_Y"
        )
    return value


class NativeToolbarClick:
    """Clicks the extension's toolbar button at explicitly calibrated pixels."""

    def __init__(
        self,
        x: Any,
        y: Any,
        *,
        window_name: str = "Google Chrome",
        window_id: str = "",
        runner: CommandRunner = run_command,
        which: Which = shutil.which,
        environ: Mapping[str, str] | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        button: int = LEFT_BUTTON,
    ) -> None:
        self.x = x
        self.y = y
        self.window_name = window_name
        self.window_id = window_id
        self.runner = runner
        self.which = which
        self.environ = environ if environ is not None else os.environ
        self.timeout_ms = timeout_ms
        self.button = button

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "NativeToolbarClick":
        kwargs.setdefault("window_name", settings.chrome_window_name)
        kwargs.setdefault("window_id", settings.chrome_window_id)
        return cls(settings.toolbar_x, settings.toolbar_y, **kwargs)

    async def click(self) -> None:
        """Activate the Chrome window, then click the calibrated pixel.

        A screen coordinate means nothing without knowing which window is in
        front of it, so the configured window is activated and the activation
        is *verified* before the pointer moves; if the wrong window is focused
        the click never happens.

        Raises `NativeClickUnavailable` when a precondition is missing (in
        which case nothing was executed and the pointer never moved) and
        `NativeClickFailed` when a command actually ran and failed.
        """
        x = _validated_coordinate("TOOLBAR_X", self.x)
        y = _validated_coordinate("TOOLBAR_Y", self.y)
        configured_id = self._validated_window_id()

        binary = self.which(XDOTOOL_BINARY)
        if not binary:
            raise NativeClickUnavailable(
                "xdotool was not found on PATH; install it (e.g. apt-get install "
                "xdotool) to enable the native toolbar click tier"
            )

        resolve_display(self.environ)

        window = configured_id or await self._find_window(binary)
        await self._activate(binary, window)

        await self._run([binary, "mousemove", "--sync", str(x), str(y)])
        await self._run([binary, "click", str(self.button)])

    def _validated_window_id(self) -> str:
        window_id = str(self.window_id or "").strip()
        if not window_id:
            return ""
        if not _WINDOW_ID.match(window_id):
            _unavailable(
                f"CHROME_WINDOW_ID must be a decimal X window id, got {window_id!r}; "
                'find it with: xdotool search --name "Google Chrome"'
            )
        return window_id

    async def _find_window(self, binary: str) -> str:
        result = await self._run(
            [binary, "search", "--onlyvisible", "--name", self.window_name],
            check=False,
        )
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            if result.returncode not in (0, 1) or result.stderr.strip():
                detail = result.stderr.strip() or "no output"
                raise NativeClickFailed(
                    [binary, "search", "--onlyvisible", "--name", self.window_name],
                    f"exit code {result.returncode}: {detail}",
                )
            _unavailable(
                f"no visible window matching {self.window_name!r} was found, so the "
                "calibrated toolbar pixel cannot be attributed to a Chrome window; "
                "set CHROME_WINDOW_NAME (or CHROME_WINDOW_ID) to match the running "
                "browser"
            )
        if len(ids) > 1:
            _unavailable(
                f"{len(ids)} visible windows match {self.window_name!r} ({', '.join(ids)}); "
                "set CHROME_WINDOW_ID to the one holding the extension toolbar so the "
                "click is never aimed at the wrong window"
            )
        return ids[0]

    async def _activate(self, binary: str, window: str) -> None:
        await self._run([binary, "windowactivate", "--sync", window])
        active = await self._run([binary, "getactivewindow"])
        focused = active.stdout.strip()
        if focused != window:
            raise NativeClickFailed(
                [binary, "windowactivate", "--sync", window],
                f"could not activate window {window}: the active window is "
                f"{focused or 'unknown'}; refusing to click a calibrated pixel over "
                "an unknown window",
            )

    async def _run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        timeout_s = self.timeout_ms / 1000
        try:
            result = await self.runner(argv, timeout_s, minimal_env(self.environ))
        except NativeClickFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - any launch failure is a click failure
            raise NativeClickFailed(argv, str(exc)) from exc

        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise NativeClickFailed(argv, f"exit code {result.returncode}: {detail}")
        return result
