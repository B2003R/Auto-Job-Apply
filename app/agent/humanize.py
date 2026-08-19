"""Randomized-but-reproducible pacing and pointer paths.

Two audiences have to be satisfied at once. A page should see a pointer that
accelerates, wobbles, overshoots nothing, and pauses for a human length of
time between actions, because instantaneous teleport-and-click is both a
coarse automation signal and, on extension UIs that listen for real pointer
events, simply ineffective. A test should see the exact same numbers every
run, because otherwise every downstream test that clicks something becomes
slow and flaky.

Both are met by making the two sources of non-determinism injectable: the
random source (`rng`/`seed`) and the sleeper (`sleep`). Nothing here touches
the `random` module's global state or `asyncio.sleep` unless a caller lets it.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from app.config import Settings

Sleeper = Callable[[float], Awaitable[None]]
Point = tuple[float, float]

DEFAULT_MIN_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 1_500

#: Steps in a pointer path. Enough for the movement to look continuous
#: without flooding the driver with round trips.
DEFAULT_MIN_STEPS = 6
DEFAULT_MAX_STEPS = 12

#: Per-step pause bounds (seconds), i.e. pointer speed.
_STEP_PAUSE_RANGE = (0.012, 0.035)
#: How long the button stays down (seconds).
_PRESS_RANGE = (0.04, 0.09)
#: Lateral wobble applied to intermediate points (pixels).
_JITTER_RANGE = (-1.5, 1.5)
#: Where the pointer enters from, relative to the target (pixels).
_APPROACH_X_RANGE = (80.0, 220.0)
_APPROACH_Y_RANGE = (60.0, 160.0)
#: Where inside the control the click lands, as a fraction of its box. Kept
#: away from the edges so a border or a padding quirk cannot miss it.
_TARGET_FRACTION_RANGE = (0.35, 0.65)


@dataclass(frozen=True)
class MousePath:
    """The points a pointer visited, in order, ending on the target."""

    points: tuple[Point, ...]

    @property
    def target(self) -> Point:
        return self.points[-1]


class Humanizer:
    """Draws human-plausible delays and pointer paths from an injected source.

    `rng` (or `seed`) fixes every random draw and `sleep` receives every wait,
    so a test can assert exact sequences while production gets a fresh
    unpredictable stream and real `asyncio.sleep`.
    """

    def __init__(
        self,
        *,
        min_delay_ms: int = DEFAULT_MIN_DELAY_MS,
        max_delay_ms: int = DEFAULT_MAX_DELAY_MS,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        seed: int | None = None,
        min_steps: int = DEFAULT_MIN_STEPS,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        if min_delay_ms < 0 or max_delay_ms < 0:
            raise ValueError(
                "humanization delays must be non-negative; got "
                f"min={min_delay_ms}ms, max={max_delay_ms}ms"
            )
        if min_delay_ms > max_delay_ms:
            raise ValueError(
                "humanization delay window is inverted: "
                f"min={min_delay_ms}ms is greater than max={max_delay_ms}ms"
            )
        if min_steps < 1 or max_steps < min_steps:
            raise ValueError(
                "pointer step bounds must satisfy 1 <= min <= max; got "
                f"min={min_steps}, max={max_steps}"
            )
        self._min_delay_s = min_delay_ms / 1000
        self._max_delay_s = max_delay_ms / 1000
        self._sleep = sleep
        # A private Random by default: consuming the global `random` stream
        # would make unrelated seeded code non-reproducible.
        self._rng = rng if rng is not None else random.Random(seed)
        self._min_steps = min_steps
        self._max_steps = max_steps

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        seed: int | None = None,
    ) -> "Humanizer":
        return cls(
            min_delay_ms=settings.delay_min_ms,
            max_delay_ms=settings.delay_max_ms,
            sleep=sleep,
            rng=rng,
            seed=seed,
        )

    @property
    def rng(self) -> random.Random:
        return self._rng

    @property
    def sleeper(self) -> Sleeper:
        return self._sleep

    def delay_seconds(self, scale: float = 1.0) -> float:
        """Draw one pause length, in seconds."""
        if scale < 0:
            raise ValueError(f"delay scale must be non-negative; got {scale}")
        return self._rng.uniform(self._min_delay_s, self._max_delay_s) * scale

    async def sleep(self, scale: float = 1.0) -> float:
        """Pause for a drawn delay and return how long that was."""
        duration = self.delay_seconds(scale)
        await self._sleep(duration)
        return duration

    def path(
        self,
        start: Point,
        target: Point,
        steps: int | None = None,
    ) -> MousePath:
        """Build an eased, jittered path from `start` to exactly `target`.

        Smoothstep easing accelerates away from the start and decelerates
        into the target the way a hand-driven pointer does; intermediate
        points wobble, and the final point is exact so the press lands where
        the caller asked.
        """
        if steps is None:
            steps = self._rng.randint(self._min_steps, self._max_steps)
        if steps < 1:
            raise ValueError(f"a pointer path needs at least one step; got {steps}")

        start_x, start_y = start
        target_x, target_y = target
        points: list[Point] = []
        for step in range(1, steps + 1):
            progress = step / steps
            eased = progress * progress * (3 - 2 * progress)
            final = step == steps
            jitter_x = 0.0 if final else self._rng.uniform(*_JITTER_RANGE)
            jitter_y = 0.0 if final else self._rng.uniform(*_JITTER_RANGE)
            points.append(
                (
                    start_x + (target_x - start_x) * eased + jitter_x,
                    start_y + (target_y - start_y) * eased + jitter_y,
                )
            )
        return MousePath(points=tuple(points))

    def click_point(self, box: Mapping[str, float]) -> Point:
        """Pick a point inside a bounding box, away from its edges."""
        x = float(box.get("x", 0.0))
        y = float(box.get("y", 0.0))
        width = float(box.get("width", 0.0))
        height = float(box.get("height", 0.0))
        return (
            x + width * self._rng.uniform(*_TARGET_FRACTION_RANGE),
            y + height * self._rng.uniform(*_TARGET_FRACTION_RANGE),
        )

    def approach_point(self, target: Point) -> Point:
        """Pick where the pointer enters from, up and to the left of target."""
        return (
            target[0] - self._rng.uniform(*_APPROACH_X_RANGE),
            target[1] - self._rng.uniform(*_APPROACH_Y_RANGE),
        )

    async def move_to(
        self,
        mouse: Any,
        box: Mapping[str, float],
    ) -> MousePath:
        """Travel to a point inside `box`, without pressing anything.

        Separate from `move_and_click` because the safest way to click a
        control is to let the driver do it, after its own actionability and
        hit-target checks — and that leaves the approach as the only part
        worth humanising. A page's own pointer listeners see the same
        movement either way.

        `mouse` is duck-typed (`move`), so this drives a Playwright mouse, a
        frame's mouse, or a test double identically.
        """
        target = self.click_point(box)
        travelled = self.path(self.approach_point(target), target)

        for point in travelled.points:
            await mouse.move(point[0], point[1])
            await self._sleep(self._rng.uniform(*_STEP_PAUSE_RANGE))
        return travelled

    async def move_and_click(
        self,
        mouse: Any,
        box: Mapping[str, float],
    ) -> MousePath:
        """Travel to a point inside `box` and press once, with the pointer.

        For controls where an untrusted-but-realistic pointer press is the
        point — an extension's own sidebar button, which listens for pointer
        events. The final submit control is *not* one of those: see
        `PlaywrightSubmitter._press`.
        """
        travelled = await self.move_to(mouse, box)
        await mouse.down()
        await self._sleep(self._rng.uniform(*_PRESS_RANGE))
        await mouse.up()
        return travelled
