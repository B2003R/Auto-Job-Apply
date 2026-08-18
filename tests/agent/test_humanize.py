"""Tests for randomized-but-injectable delays and mouse paths.

Nothing here sleeps for real: the sleeper is injected and records what it was
asked to wait for, and the random source is seeded. That is the whole point of
the module — humanization must be indistinguishable from a person to a page,
yet fully reproducible to a test, or every downstream test that clicks
something becomes slow and flaky.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
from typing import Any

import pytest

from app.agent.humanize import DEFAULT_MAX_STEPS, DEFAULT_MIN_STEPS, Humanizer, MousePath
from app.config import Settings

BOX = {"x": 100.0, "y": 40.0, "width": 120.0, "height": 32.0}


class RecordingSleeper:
    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


class RecordingMouse:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def move(self, x: float, y: float, **kwargs: Any) -> None:
        self.events.append(("move", (x, y)))

    async def down(self, **kwargs: Any) -> None:
        self.events.append(("down", None))

    async def up(self, **kwargs: Any) -> None:
        self.events.append(("up", None))


def humanizer(seed: int = 7, sleeper: RecordingSleeper | None = None, **kwargs: Any) -> Humanizer:
    return Humanizer(sleep=sleeper or RecordingSleeper(), seed=seed, **kwargs)


def moves(mouse: RecordingMouse) -> list[tuple[float, float]]:
    return [payload for name, payload in mouse.events if name == "move"]


class TestDeterminism:
    async def test_same_seed_produces_identical_delays(self) -> None:
        first_sleeper, second_sleeper = RecordingSleeper(), RecordingSleeper()
        first = humanizer(seed=42, sleeper=first_sleeper)
        second = humanizer(seed=42, sleeper=second_sleeper)

        for _ in range(5):
            await first.sleep()
            await second.sleep()

        assert first_sleeper.durations == second_sleeper.durations
        assert len(set(first_sleeper.durations)) > 1

    async def test_different_seeds_produce_different_delays(self) -> None:
        first_sleeper, second_sleeper = RecordingSleeper(), RecordingSleeper()

        for _ in range(5):
            await humanizer(seed=1, sleeper=first_sleeper).sleep()
            await humanizer(seed=2, sleeper=second_sleeper).sleep()

        assert first_sleeper.durations != second_sleeper.durations

    async def test_same_seed_produces_identical_mouse_paths(self) -> None:
        first_mouse, second_mouse = RecordingMouse(), RecordingMouse()

        await humanizer(seed=11).move_and_click(first_mouse, BOX)
        await humanizer(seed=11).move_and_click(second_mouse, BOX)

        assert first_mouse.events == second_mouse.events

    async def test_an_injected_random_source_is_used_instead_of_the_global_one(self) -> None:
        shared = random.Random(99)
        injected = RecordingSleeper()
        separate = RecordingSleeper()

        human = Humanizer(sleep=injected, rng=shared)
        await human.sleep()
        await Humanizer(sleep=separate, rng=random.Random(99)).sleep()

        assert human.rng is shared
        assert injected.durations == separate.durations

    async def test_the_global_random_module_is_never_consumed(self) -> None:
        random.seed(1234)
        before = random.random()
        random.seed(1234)

        sleeper = RecordingSleeper()
        await Humanizer(sleep=sleeper, seed=5).sleep()
        await Humanizer(sleep=sleeper, seed=5).move_and_click(RecordingMouse(), BOX)

        assert random.random() == before

    def test_path_is_pure_and_repeatable_for_one_humanizer_seed(self) -> None:
        first = humanizer(seed=5).path((0.0, 0.0), (200.0, 100.0))
        second = humanizer(seed=5).path((0.0, 0.0), (200.0, 100.0))

        assert first == second
        assert isinstance(first, MousePath)
        assert isinstance(first.points, tuple)


class TestDelayBounds:
    async def test_delays_stay_within_the_configured_window(self) -> None:
        sleeper = RecordingSleeper()
        human = Humanizer(min_delay_ms=200, max_delay_ms=400, sleep=sleeper, seed=3)

        for _ in range(50):
            await human.sleep()

        assert all(0.2 <= duration <= 0.4 for duration in sleeper.durations)

    async def test_sleep_returns_the_duration_it_waited(self) -> None:
        sleeper = RecordingSleeper()
        human = Humanizer(min_delay_ms=100, max_delay_ms=300, sleep=sleeper, seed=3)

        waited = await human.sleep()

        assert sleeper.durations == [waited]

    async def test_scale_multiplies_the_drawn_delay(self) -> None:
        plain, scaled = RecordingSleeper(), RecordingSleeper()

        await Humanizer(min_delay_ms=100, max_delay_ms=300, sleep=plain, seed=8).sleep()
        await Humanizer(min_delay_ms=100, max_delay_ms=300, sleep=scaled, seed=8).sleep(2.0)

        assert scaled.durations[0] == pytest.approx(plain.durations[0] * 2)

    async def test_a_fixed_window_still_sleeps_exactly_that_long(self) -> None:
        sleeper = RecordingSleeper()
        human = Humanizer(min_delay_ms=250, max_delay_ms=250, sleep=sleeper, seed=1)

        await human.sleep()

        assert sleeper.durations == [0.25]

    @pytest.mark.parametrize(
        "min_ms,max_ms",
        [(500, 400), (-1, 100), (100, -1), (-5, -1)],
    )
    def test_impossible_delay_windows_are_rejected(self, min_ms: int, max_ms: int) -> None:
        with pytest.raises(ValueError):
            Humanizer(min_delay_ms=min_ms, max_delay_ms=max_ms)

    async def test_negative_scale_is_rejected(self) -> None:
        sleeper = RecordingSleeper()

        with pytest.raises(ValueError):
            await Humanizer(sleep=sleeper, seed=1).sleep(-1.0)

        assert sleeper.durations == []


class TestSettingsWiring:
    async def test_from_settings_uses_the_configured_delay_window(self) -> None:
        sleeper = RecordingSleeper()
        settings = Settings(_env_file=None, delay_min_ms=700, delay_max_ms=900)
        human = Humanizer.from_settings(settings, sleep=sleeper, seed=4)

        for _ in range(20):
            await human.sleep()

        assert all(0.7 <= duration <= 0.9 for duration in sleeper.durations)

    def test_the_default_sleeper_is_asyncio_sleep(self) -> None:
        assert Humanizer().sleeper is asyncio.sleep


class TestMousePaths:
    async def test_click_moves_in_many_steps_then_presses_and_releases(self) -> None:
        mouse = RecordingMouse()

        await humanizer(seed=13).move_and_click(mouse, BOX)

        names = [name for name, _ in mouse.events]
        assert names.count("move") >= DEFAULT_MIN_STEPS
        assert names[-2:] == ["down", "up"]
        assert names.index("down") == len(names) - 2

    @pytest.mark.parametrize("seed", list(range(15)))
    async def test_the_final_point_is_always_inside_the_target_box(self, seed: int) -> None:
        mouse = RecordingMouse()

        await humanizer(seed=seed).move_and_click(mouse, BOX)

        x, y = moves(mouse)[-1]
        assert BOX["x"] <= x <= BOX["x"] + BOX["width"]
        assert BOX["y"] <= y <= BOX["y"] + BOX["height"]

    async def test_a_degenerate_box_still_yields_a_point_on_the_control(self) -> None:
        mouse = RecordingMouse()
        box = {"x": 10.0, "y": 20.0, "width": 0.0, "height": 0.0}

        await humanizer(seed=2).move_and_click(mouse, box)

        assert moves(mouse)[-1] == (10.0, 20.0)

    async def test_the_path_does_not_start_at_the_target(self) -> None:
        mouse = RecordingMouse()

        await humanizer(seed=6).move_and_click(mouse, BOX)

        first, last = moves(mouse)[0], moves(mouse)[-1]
        assert abs(first[0] - last[0]) > 1.0 or abs(first[1] - last[1]) > 1.0

    async def test_intermediate_points_are_jittered_off_the_straight_line(self) -> None:
        human = humanizer(seed=21)
        path = human.path((0.0, 0.0), (300.0, 300.0), steps=10)

        interior = path.points[:-1]
        assert any(abs(x - y) > 1e-9 for x, y in interior)
        assert path.points[-1] == (300.0, 300.0)

    async def test_movement_and_press_durations_are_slept_between_events(self) -> None:
        sleeper = RecordingSleeper()
        mouse = RecordingMouse()

        await humanizer(seed=9, sleeper=sleeper).move_and_click(mouse, BOX)

        assert len(sleeper.durations) == len(moves(mouse)) + 1
        assert all(0.0 < duration < 1.0 for duration in sleeper.durations)

    async def test_step_count_stays_within_the_configured_bounds(self) -> None:
        for seed in range(20):
            mouse = RecordingMouse()
            await humanizer(seed=seed).move_and_click(mouse, BOX)
            assert DEFAULT_MIN_STEPS <= len(moves(mouse)) <= DEFAULT_MAX_STEPS

    async def test_returns_the_path_it_travelled(self) -> None:
        mouse = RecordingMouse()

        path = await humanizer(seed=4).move_and_click(mouse, BOX)

        assert isinstance(path, MousePath)
        assert list(path.points) == moves(mouse)
        assert path.target == path.points[-1]

    def test_paths_are_immutable(self) -> None:
        path = humanizer(seed=4).path((0.0, 0.0), (10.0, 10.0))

        with pytest.raises(dataclasses.FrozenInstanceError):
            path.points = ()  # type: ignore[misc]

    def test_explicit_step_counts_are_honoured(self) -> None:
        path = humanizer(seed=4).path((0.0, 0.0), (10.0, 10.0), steps=3)

        assert len(path.points) == 3

    @pytest.mark.parametrize("steps", [0, -3])
    def test_non_positive_step_counts_are_rejected(self, steps: int) -> None:
        with pytest.raises(ValueError):
            humanizer(seed=4).path((0.0, 0.0), (10.0, 10.0), steps=steps)
