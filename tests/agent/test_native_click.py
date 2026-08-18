"""Tests for the isolated, shell-free native (xdotool) toolbar click.

No test here needs xdotool, an X display, or any subprocess: the command
runner, the `xdotool` lookup, and the environment are all injected.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Sequence

import pytest

from app.agent import native_click as native_click_module
from app.agent.errors import NativeClickFailed, NativeClickUnavailable
from app.agent.native_click import CommandResult, NativeToolbarClick
from app.config import Settings
from scripts import calibrate_toolbar

XDOTOOL_PATH = "/usr/bin/xdotool"


class RecordingRunner:
    """Captures every argv it is asked to run and replays scripted results."""

    def __init__(self, results: Sequence[Any] | None = None) -> None:
        self.calls: list[tuple[Sequence[str], float]] = []
        self._results = list(results) if results is not None else []

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.calls.append((argv, timeout_s))
        if not self._results:
            return CommandResult(returncode=0, stdout="", stderr="")
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def argvs(self) -> list[list[str]]:
        return [list(argv) for argv, _ in self.calls]


def which_found(name: str) -> str | None:
    return XDOTOOL_PATH if name == "xdotool" else None


def which_missing(name: str) -> str | None:
    return None


def build_click(**overrides: Any) -> NativeToolbarClick:
    kwargs: dict[str, Any] = {
        "x": 1200,
        "y": 80,
        "runner": RecordingRunner(),
        "which": which_found,
        "environ": {"DISPLAY": ":0"},
    }
    kwargs.update(overrides)
    return NativeToolbarClick(**kwargs)


class TestNativeToolbarClickCommands:
    async def test_moves_then_clicks_with_calibrated_coordinates(self) -> None:
        runner = RecordingRunner()
        clicker = build_click(runner=runner)

        await clicker.click()

        assert runner.argvs == [
            [XDOTOOL_PATH, "mousemove", "--sync", "1200", "80"],
            [XDOTOOL_PATH, "click", "1"],
        ]

    async def test_uses_the_resolved_absolute_binary_path(self) -> None:
        runner = RecordingRunner()
        await build_click(runner=runner, which=lambda name: "/opt/bin/xdotool").click()

        assert all(argv[0] == "/opt/bin/xdotool" for argv in runner.argvs)

    async def test_arguments_are_a_sequence_never_a_shell_string(self) -> None:
        runner = RecordingRunner()

        await build_click(runner=runner).click()

        for argv, _ in runner.calls:
            assert not isinstance(argv, (str, bytes))
            assert all(isinstance(part, str) for part in argv)

    async def test_passes_a_bounded_timeout_to_every_command(self) -> None:
        runner = RecordingRunner()

        await build_click(runner=runner, timeout_ms=2500).click()

        assert [timeout for _, timeout in runner.calls] == [2.5, 2.5]

    async def test_from_settings_reads_calibrated_toolbar_coordinates(self) -> None:
        settings = Settings(_env_file=None, toolbar_x=640, toolbar_y=42)
        runner = RecordingRunner()

        clicker = NativeToolbarClick.from_settings(
            settings, runner=runner, which=which_found, environ={"DISPLAY": ":1"}
        )
        await clicker.click()

        assert runner.argvs[0] == [XDOTOOL_PATH, "mousemove", "--sync", "640", "42"]


class TestNativeToolbarClickPreconditions:
    async def test_missing_xdotool_is_unavailable_and_runs_nothing(self) -> None:
        runner = RecordingRunner()

        with pytest.raises(NativeClickUnavailable) as excinfo:
            await build_click(runner=runner, which=which_missing).click()

        assert "xdotool" in str(excinfo.value)
        assert runner.calls == []

    @pytest.mark.parametrize("environ", [{}, {"DISPLAY": ""}])
    async def test_missing_display_is_unavailable_and_runs_nothing(
        self, environ: dict[str, str]
    ) -> None:
        runner = RecordingRunner()

        with pytest.raises(NativeClickUnavailable) as excinfo:
            await build_click(runner=runner, environ=environ).click()

        assert "DISPLAY" in str(excinfo.value)
        assert runner.calls == []

    @pytest.mark.parametrize("coordinates", [(0, 80), (1200, 0), (-5, 80), (1200, -1)])
    async def test_uncalibrated_coordinates_are_unavailable(
        self, coordinates: tuple[int, int]
    ) -> None:
        runner = RecordingRunner()
        x, y = coordinates

        with pytest.raises(NativeClickUnavailable) as excinfo:
            await build_click(runner=runner, x=x, y=y).click()

        assert "calibrat" in str(excinfo.value).lower()
        assert runner.calls == []

    @pytest.mark.parametrize("bad", ["1200; rm -rf /", "80 && reboot", 12.5, None, True])
    async def test_non_integer_coordinates_are_rejected_before_execution(
        self, bad: Any
    ) -> None:
        runner = RecordingRunner()

        with pytest.raises(NativeClickUnavailable):
            await build_click(runner=runner, x=bad).click()

        assert runner.calls == []


class TestNativeToolbarClickFailures:
    async def test_non_zero_exit_reports_the_failing_command_and_stderr(self) -> None:
        runner = RecordingRunner(
            [CommandResult(returncode=1, stdout="", stderr="Cannot open display")]
        )

        with pytest.raises(NativeClickFailed) as excinfo:
            await build_click(runner=runner).click()

        message = str(excinfo.value)
        assert "Cannot open display" in message
        assert "mousemove" in message
        assert excinfo.value.command[0] == XDOTOOL_PATH

    async def test_click_step_failure_is_reported_after_a_successful_move(self) -> None:
        runner = RecordingRunner(
            [
                CommandResult(returncode=0, stdout="", stderr=""),
                CommandResult(returncode=3, stdout="", stderr="no button"),
            ]
        )

        with pytest.raises(NativeClickFailed) as excinfo:
            await build_click(runner=runner).click()

        assert "click" in str(excinfo.value)
        assert len(runner.calls) == 2

    async def test_runner_errors_surface_as_native_click_failures(self) -> None:
        runner = RecordingRunner([OSError("xdotool vanished")])

        with pytest.raises(NativeClickFailed) as excinfo:
            await build_click(runner=runner).click()

        assert "xdotool vanished" in str(excinfo.value)


class TestNativeIsolation:
    def test_module_never_uses_a_shell(self) -> None:
        source = Path(native_click_module.__file__).read_text(encoding="utf-8")

        assert "create_subprocess_shell" not in source
        assert "shell=True" not in source
        assert "os.system" not in source

    def test_default_runner_execs_the_binary_directly(self) -> None:
        source = inspect.getsource(native_click_module.run_command)

        assert "create_subprocess_exec" in source

    def test_default_runner_is_wired_as_the_default(self) -> None:
        clicker = NativeToolbarClick(x=1, y=1)

        assert clicker.runner is native_click_module.run_command


class TestCalibrationScript:
    def test_prints_env_lines_for_the_current_pointer_position(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = RecordingRunner(
            [CommandResult(returncode=0, stdout="X=1211\nY=84\nSCREEN=0\nWINDOW=12\n", stderr="")]
        )

        exit_code = calibrate_toolbar.main(
            ["--countdown", "0"],
            runner=runner,
            which=which_found,
            environ={"DISPLAY": ":0"},
            sleep=lambda _seconds: None,
        )

        output = capsys.readouterr().out
        assert exit_code == 0
        assert "TOOLBAR_X=1211" in output
        assert "TOOLBAR_Y=84" in output
        assert runner.argvs == [[XDOTOOL_PATH, "getmouselocation", "--shell"]]

    def test_counts_down_before_sampling_the_pointer(self) -> None:
        slept: list[float] = []
        runner = RecordingRunner(
            [CommandResult(returncode=0, stdout="X=1\nY=2\n", stderr="")]
        )

        calibrate_toolbar.main(
            ["--countdown", "3"],
            runner=runner,
            which=which_found,
            environ={"DISPLAY": ":0"},
            sleep=slept.append,
        )

        assert slept == [1.0, 1.0, 1.0]

    def test_reports_missing_xdotool_without_running_anything(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = RecordingRunner()

        exit_code = calibrate_toolbar.main(
            ["--countdown", "0"],
            runner=runner,
            which=which_missing,
            environ={"DISPLAY": ":0"},
            sleep=lambda _seconds: None,
        )

        assert exit_code == 1
        assert "xdotool" in capsys.readouterr().err
        assert runner.calls == []

    def test_reports_missing_display(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = calibrate_toolbar.main(
            ["--countdown", "0"],
            runner=RecordingRunner(),
            which=which_found,
            environ={},
            sleep=lambda _seconds: None,
        )

        assert exit_code == 1
        assert "DISPLAY" in capsys.readouterr().err

    def test_reports_unparseable_pointer_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = RecordingRunner(
            [CommandResult(returncode=0, stdout="nothing useful", stderr="")]
        )

        exit_code = calibrate_toolbar.main(
            ["--countdown", "0"],
            runner=runner,
            which=which_found,
            environ={"DISPLAY": ":0"},
            sleep=lambda _seconds: None,
        )

        assert exit_code == 1
        assert "pointer" in capsys.readouterr().err.lower()

    def test_reports_a_failing_xdotool_invocation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = RecordingRunner(
            [CommandResult(returncode=1, stdout="", stderr="Cannot open display")]
        )

        exit_code = calibrate_toolbar.main(
            ["--countdown", "0"],
            runner=runner,
            which=which_found,
            environ={"DISPLAY": ":0"},
            sleep=lambda _seconds: None,
        )

        assert exit_code == 1
        assert "Cannot open display" in capsys.readouterr().err
