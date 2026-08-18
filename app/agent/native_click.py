"""Native (OS-level) toolbar click via Linux `xdotool`.

This is the last-resort Autofill trigger tier and the only part of the agent
that leaves the browser, so it is deliberately isolated here and kept
shell-free:

* commands are executed with `create_subprocess_exec` and an argument list —
  never a shell string, so nothing in a coordinate or a path can be
  interpreted as shell syntax;
* the `xdotool` binary is resolved to an absolute path via `which` rather
  than relying on `PATH` resolution at exec time;
* every precondition (binary present, X display present, coordinates
  explicitly calibrated) is checked *before* anything runs, and reported as
  `NativeClickUnavailable` so callers can distinguish "not attempted" from
  "attempted and failed";
* every command is bounded by a timeout, and a timed-out child is killed.

`runner`, `which`, and `environ` are injectable, so tests never need
xdotool, an X display, or a subprocess.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, NoReturn, Sequence

from app.agent.errors import NativeClickFailed, NativeClickUnavailable
from app.config import Settings

XDOTOOL_BINARY = "xdotool"
DEFAULT_TIMEOUT_MS = 5_000
LEFT_BUTTON = 1


@dataclass(frozen=True)
class CommandResult:
    """Exit status and captured output of one native command."""

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], float], Awaitable[CommandResult]]
Which = Callable[[str], str | None]


async def run_command(argv: Sequence[str], timeout_s: float) -> CommandResult:
    """Execute `argv` directly (no shell) and capture its output.

    A child that outlives `timeout_s` is killed and reaped before the
    timeout is reported, so a hung `xdotool` cannot leak a process.
    """
    argv = list(argv)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
        runner: CommandRunner = run_command,
        which: Which = shutil.which,
        environ: Mapping[str, str] | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        button: int = LEFT_BUTTON,
    ) -> None:
        self.x = x
        self.y = y
        self.runner = runner
        self.which = which
        self.environ = environ if environ is not None else os.environ
        self.timeout_ms = timeout_ms
        self.button = button

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "NativeToolbarClick":
        return cls(settings.toolbar_x, settings.toolbar_y, **kwargs)

    async def click(self) -> None:
        """Move the pointer to the calibrated toolbar pixel and click it.

        Raises `NativeClickUnavailable` when a precondition is missing (in
        which case nothing was executed and the pointer never moved) and
        `NativeClickFailed` when a command actually ran and failed.
        """
        x = _validated_coordinate("TOOLBAR_X", self.x)
        y = _validated_coordinate("TOOLBAR_Y", self.y)

        binary = self.which(XDOTOOL_BINARY)
        if not binary:
            raise NativeClickUnavailable(
                "xdotool was not found on PATH; install it (e.g. apt-get install "
                "xdotool) to enable the native toolbar click tier"
            )

        resolve_display(self.environ)

        await self._run([binary, "mousemove", "--sync", str(x), str(y)])
        await self._run([binary, "click", str(self.button)])

    async def _run(self, argv: Sequence[str]) -> CommandResult:
        timeout_s = self.timeout_ms / 1000
        try:
            result = await self.runner(argv, timeout_s)
        except NativeClickFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - any launch failure is a click failure
            raise NativeClickFailed(argv, str(exc)) from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise NativeClickFailed(argv, f"exit code {result.returncode}: {detail}")
        return result
