"""Calibrate the extension toolbar coordinates used by the native click tier.

Run this once per screen layout: hover the mouse over the Jobright Autofill
toolbar button, let the countdown finish, and paste the printed
`TOOLBAR_X`/`TOOLBAR_Y` lines into `.env`.

Nothing is guessed and nothing is written automatically — the native tier
refuses to run against uncalibrated coordinates on purpose, since a
mis-aimed OS-level click lands on whatever else happens to be under it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from app.agent.errors import NativeClickError, NativeClickUnavailable
from app.agent.native_click import (
    DEFAULT_TIMEOUT_MS,
    XDOTOOL_BINARY,
    CommandRunner,
    Which,
    resolve_display,
    run_command,
)

DEFAULT_COUNTDOWN_SECONDS = 5


@dataclass(frozen=True)
class PointerPosition:
    x: int
    y: int


def parse_pointer_position(stdout: str) -> PointerPosition | None:
    """Parse `xdotool getmouselocation --shell` output (`X=..`/`Y=..` lines).

    The output is parsed as plain key/value text and never evaluated, even
    though the `--shell` form is designed to be `eval`-ed.
    """
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()

    raw_x, raw_y = values.get("X", ""), values.get("Y", "")
    if not (raw_x.lstrip("-").isdigit() and raw_y.lstrip("-").isdigit()):
        return None
    return PointerPosition(x=int(raw_x), y=int(raw_y))


async def read_pointer_position(
    *,
    runner: CommandRunner,
    which: Which,
    environ: Mapping[str, str],
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> PointerPosition:
    binary = which(XDOTOOL_BINARY)
    if not binary:
        raise NativeClickUnavailable(
            "xdotool was not found on PATH; install it (e.g. apt-get install "
            "xdotool) before calibrating toolbar coordinates"
        )
    resolve_display(environ)

    result = await runner([binary, "getmouselocation", "--shell"], timeout_ms / 1000)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise NativeClickUnavailable(f"xdotool getmouselocation failed: {detail}")

    position = parse_pointer_position(result.stdout)
    if position is None:
        raise NativeClickUnavailable(
            "could not parse the pointer position from xdotool output: "
            f"{result.stdout.strip()!r}"
        )
    return position


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    which: Which | None = None,
    environ: Mapping[str, str] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Print calibrated TOOLBAR_X/TOOLBAR_Y for the current pointer position"
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=DEFAULT_COUNTDOWN_SECONDS,
        help="seconds to wait before sampling the pointer position",
    )
    args = parser.parse_args(argv)

    resolved_runner = runner if runner is not None else run_command
    resolved_which = which if which is not None else shutil.which
    resolved_environ = environ if environ is not None else os.environ
    resolved_sleep = sleep if sleep is not None else time.sleep

    for remaining in range(args.countdown, 0, -1):
        print(f"Hover over the Jobright Autofill toolbar button… {remaining}")
        resolved_sleep(1.0)

    try:
        position = asyncio.run(
            read_pointer_position(
                runner=resolved_runner,
                which=resolved_which,
                environ=resolved_environ,
            )
        )
    except NativeClickError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"TOOLBAR_X={position.x}")
    print(f"TOOLBAR_Y={position.y}")
    print(
        "Add the two lines above to your .env; the native trigger tier refuses "
        "to click until they are set."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
