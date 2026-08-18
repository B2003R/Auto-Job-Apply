"""Tests for preflight doctor diagnostics and error categories."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.agent import browser_session
from app.config import Settings
from scripts import doctor
from scripts.doctor import CheckStatus, DoctorCategory, run_doctor

EXTENSION_ID = "abcextensionid1234567890abcdefg"


def _settings(profile: Path, *, toolbar_x: int = 0, toolbar_y: int = 0) -> Settings:
    return Settings(
        _env_file=None,
        chrome_executable=Path("/usr/bin/google-chrome"),
        chrome_profile_path=profile,
        jobright_extension_id=EXTENSION_ID,
        toolbar_x=toolbar_x,
        toolbar_y=toolbar_y,
    )


def _write_extension(profile: Path, *, enabled: bool = True) -> None:
    default_dir = profile / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "Secure Preferences").write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        EXTENSION_ID: {
                            "state": 1 if enabled else 0,
                            "manifest": {"name": "Jobright Autofill", "version": "1.0.0"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _category_check(report: Any, category: DoctorCategory) -> Any:
    matches = [c for c in report.checks if c.category == category]
    assert matches, f"no check recorded for category {category}"
    return matches[0]


class FakeWorker:
    def __init__(
        self,
        url: str,
        *,
        evaluate_error: Exception | None = None,
        has_evaluate: bool = True,
    ) -> None:
        self.url = url
        self._evaluate_error = evaluate_error
        if has_evaluate:
            self.evaluate = self._evaluate  # type: ignore[assignment]

    async def _evaluate(self, expression: str) -> Any:
        if self._evaluate_error is not None:
            raise self._evaluate_error
        return 2


class FakeContext:
    def __init__(self, service_workers: list[FakeWorker] | None = None) -> None:
        self.service_workers = list(service_workers or [])

    def on(self, event: str, handler: Any) -> None:
        return None

    def remove_listener(self, event: str, handler: Any) -> None:
        return None

    async def new_page(self) -> Any:
        class _Page:
            async def goto(self, url: str, **_: Any) -> None:
                return None

            async def close(self) -> None:
                return None

        return _Page()


class FakeSession:
    def __init__(
        self,
        settings: Settings,
        *,
        context: FakeContext | None = None,
        start_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.settings = settings
        self._context = context
        self._start_error = start_error
        self._close_error = close_error
        self.started = False
        self.closed = False

    async def start(self) -> FakeContext:
        if self._start_error is not None:
            raise self._start_error
        self.started = True
        assert self._context is not None
        return self._context

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _factory_returning(context: FakeContext) -> Any:
    def factory(settings: Settings) -> FakeSession:
        return FakeSession(settings, context=context)

    return factory


class TestDoctorProfileMissing:
    async def test_reports_error_when_profile_directory_absent(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "missing-profile")

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.PROFILE_MISSING)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_skips_downstream_checks_when_profile_missing(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "missing-profile")

        report = await run_doctor(settings)

        categories = {c.category for c in report.checks}
        assert DoctorCategory.EXTENSION_MISSING not in categories
        assert DoctorCategory.PROFILE_LOCKED not in categories


class TestDoctorProfileLocked:
    async def test_reports_error_for_active_lock_held_by_live_process(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "hostname": "here", "acquired_at": "now"}),
            encoding="utf-8",
        )
        settings = _settings(profile)

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_reports_error_for_stale_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": 999999, "hostname": "gone", "acquired_at": "then"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)
        settings = _settings(profile)

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    def test_stale_lock_exits_nonzero_via_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": 999999, "hostname": "gone", "acquired_at": "then"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)
        settings = _settings(profile)

        exit_code = doctor.main([], settings=settings)

        assert exit_code == 1

    async def test_stale_lock_is_never_deleted_by_doctor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": 999999, "hostname": "gone", "acquired_at": "then"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)
        settings = _settings(profile)

        await run_doctor(settings)

        assert lock_path.exists()

    async def test_skips_service_worker_check_when_locked(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "hostname": "here", "acquired_at": "now"}),
            encoding="utf-8",
        )
        settings = _settings(profile)

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.WARNING
        assert "lock" in check.message.lower()


class TestDoctorProfileLockFilesystemErrors:
    async def test_lock_filesystem_error_becomes_categorized_error_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        settings = _settings(profile)

        async def _raise(self: Any) -> None:
            raise PermissionError("permission denied creating lock file")

        monkeypatch.setattr(browser_session.ProfileLock, "acquire", _raise)

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert "permission denied" in check.message.lower()
        assert report.ok is False

    async def test_singleton_check_filesystem_error_becomes_categorized_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        settings = _settings(profile)

        def _raise(profile_path: Path) -> list[Any]:
            raise PermissionError("cannot stat SingletonLock")

        monkeypatch.setattr(doctor, "describe_chrome_singleton_locks", _raise)

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False
        # Our own lock must still be released even though the singleton check failed.
        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_probe_lock_release_failure_becomes_categorized_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Doctor acquires its own throwaway probe lock to verify the
        profile is available; if *that* lock can't be released, doctor must
        report it (rather than silently leaving a lock behind that would
        make the very next real `start()` fail with ProfileLockedError)."""
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        settings = _settings(profile)

        async def _raise(self: Any) -> None:
            raise PermissionError("cannot unlink probe lock")

        monkeypatch.setattr(browser_session.ProfileLock, "release", _raise)

        report = await run_doctor(settings)

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert "cannot unlink probe lock" in check.message
        assert report.ok is False
        # Service worker check must be skipped: doctor's own probe lock is
        # still present on disk, so a real start() would collide with it.
        sw_check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert sw_check.status is CheckStatus.WARNING


def _factory_that_must_not_be_called() -> Any:
    def factory(settings: Settings) -> Any:
        raise AssertionError("session_factory must not be invoked when profile is in use")

    return factory


class TestDoctorProfileInUseSingleton:
    async def test_reports_error_when_chrome_singleton_lock_present(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        (profile / "SingletonLock").write_text("x", encoding="utf-8")
        settings = _settings(profile)

        report = await run_doctor(settings, session_factory=_factory_that_must_not_be_called())

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert "SingletonLock" in check.message
        assert report.ok is False

    async def test_never_deletes_singleton_lock_file(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        singleton = profile / "SingletonLock"
        singleton.write_text("x", encoding="utf-8")
        settings = _settings(profile)

        await run_doctor(settings, session_factory=_factory_that_must_not_be_called())

        assert singleton.exists()

    async def test_skips_service_worker_check_when_singleton_lock_present(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        (profile / "SingletonLock").write_text("x", encoding="utf-8")
        settings = _settings(profile)

        report = await run_doctor(settings, session_factory=_factory_that_must_not_be_called())

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.WARNING

    async def test_reports_active_status_with_pid_when_singleton_owner_is_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        (profile / "SingletonLock").symlink_to("some-host-4242")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: True)
        settings = _settings(profile)

        report = await run_doctor(settings, session_factory=_factory_that_must_not_be_called())

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        assert check.status is CheckStatus.ERROR
        assert "active" in check.message
        assert "4242" in check.message

    async def test_reports_stale_status_when_singleton_owner_is_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        _write_extension(profile)
        (profile / "SingletonLock").symlink_to("gone-host-999999")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)
        settings = _settings(profile)

        report = await run_doctor(settings, session_factory=_factory_that_must_not_be_called())

        check = _category_check(report, DoctorCategory.PROFILE_LOCKED)
        # Still ERROR even though it looks stale: singleton markers are
        # never auto-deleted or downgraded.
        assert check.status is CheckStatus.ERROR
        assert "stale" in check.message


class TestDoctorExtension:
    async def test_reports_error_when_extension_missing(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        (profile / "Default").mkdir(parents=True)
        settings = _settings(profile)
        context = FakeContext(service_workers=[])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.EXTENSION_MISSING)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_reports_ok_when_extension_present(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.EXTENSION_MISSING)
        assert check.status is CheckStatus.OK

    async def test_reports_error_when_extension_present_but_disabled(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile, enabled=False)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.EXTENSION_MISSING)
        assert check.status is CheckStatus.ERROR
        assert "disabled" in check.message.lower()
        assert report.ok is False


class TestDoctorServiceWorker:
    async def test_reports_ok_when_service_worker_responsive(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.OK
        assert report.ok is True

    async def test_reports_error_when_service_worker_missing(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        context = FakeContext(service_workers=[])

        report = await run_doctor(
            settings,
            session_factory=_factory_returning(context),
            service_worker_timeout_ms=50,
        )

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_closes_session_after_check(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])
        session_holder: dict[str, FakeSession] = {}

        def factory(settings: Settings) -> FakeSession:
            session = FakeSession(settings, context=context)
            session_holder["session"] = session
            return session

        await run_doctor(settings, session_factory=factory)

        assert session_holder["session"].closed is True

    async def test_unexpected_session_factory_error_becomes_categorized_error(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)

        def _raising_factory(settings: Settings) -> Any:
            raise RuntimeError("cannot construct session")

        report = await run_doctor(settings, session_factory=_raising_factory)

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_unexpected_session_start_error_becomes_categorized_error(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)

        def factory(settings: Settings) -> FakeSession:
            return FakeSession(settings, start_error=RuntimeError("chrome crashed"))

        report = await run_doctor(settings, session_factory=factory)

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_session_close_is_still_attempted_after_start_failure(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        session_holder: dict[str, FakeSession] = {}

        def factory(settings: Settings) -> FakeSession:
            session = FakeSession(settings, start_error=RuntimeError("chrome crashed"))
            session_holder["session"] = session
            return session

        await run_doctor(settings, session_factory=factory)

        assert session_holder["session"].closed is True

    async def test_unexpected_service_worker_lookup_error_becomes_categorized_error(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)

        class _BrokenContext:
            service_workers: list[Any] = []

            def on(self, event: str, handler: Any) -> None:
                raise RuntimeError("context is not a real BrowserContext")

        report = await run_doctor(
            settings, session_factory=_factory_returning(_BrokenContext())  # type: ignore[arg-type]
        )

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False

    async def test_reports_error_when_worker_found_but_unresponsive(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(
            url=f"chrome-extension://{EXTENSION_ID}/background.js",
            evaluate_error=RuntimeError("worker terminated"),
        )
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.ERROR
        assert report.ok is False
        # Must not be reported as "not found"/"not discovered" — the worker
        # WAS discovered, it just failed to respond.
        assert "discovered" in check.message
        assert "not found" not in check.message.lower()

    async def test_session_close_error_does_not_crash_doctor_or_mask_ok_result(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        def factory(settings: Settings) -> FakeSession:
            return FakeSession(
                settings, context=context, close_error=RuntimeError("close boom")
            )

        report = await run_doctor(settings, session_factory=factory)

        check = _category_check(report, DoctorCategory.SERVICE_WORKER_DEAD)
        assert check.status is CheckStatus.OK


class TestDoctorXdotoolAndCalibration:
    async def test_reports_warning_when_xdotool_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.XDOTOOL_MISSING)
        assert check.status is CheckStatus.WARNING

    async def test_reports_ok_when_xdotool_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])
        monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/xdotool")

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.XDOTOOL_MISSING)
        assert check.status is CheckStatus.OK

    async def test_reports_warning_for_invalid_calibration(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile, toolbar_x=0, toolbar_y=0)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.CALIBRATION_INVALID)
        assert check.status is CheckStatus.WARNING

    async def test_default_sentinel_calibration_is_explained_as_uncalibrated(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.CALIBRATION_INVALID)
        assert check.status is CheckStatus.WARNING
        assert "scripts/calibrate_toolbar.py" in check.message
        assert "native" in check.message.lower()

    async def test_reports_ok_for_valid_calibration(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        _write_extension(profile)
        settings = _settings(profile, toolbar_x=1200, toolbar_y=80)
        worker = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[worker])

        report = await run_doctor(settings, session_factory=_factory_returning(context))

        check = _category_check(report, DoctorCategory.CALIBRATION_INVALID)
        assert check.status is CheckStatus.OK


class TestDoctorMain:
    def test_main_returns_zero_when_report_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_run_doctor(settings: Settings, **_: Any) -> Any:
            return doctor.DoctorReport(
                checks=[doctor.DoctorCheck(DoctorCategory.PROFILE_MISSING, CheckStatus.OK, "fine")]
            )

        monkeypatch.setattr(doctor, "run_doctor", _fake_run_doctor)

        exit_code = doctor.main([], settings=_settings(Path("/tmp/does-not-matter")))

        assert exit_code == 0

    def test_main_returns_nonzero_when_report_has_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_run_doctor(settings: Settings, **_: Any) -> Any:
            return doctor.DoctorReport(
                checks=[doctor.DoctorCheck(DoctorCategory.PROFILE_MISSING, CheckStatus.ERROR, "bad")]
            )

        monkeypatch.setattr(doctor, "run_doctor", _fake_run_doctor)

        exit_code = doctor.main([], settings=_settings(Path("/tmp/does-not-matter")))

        assert exit_code == 1
