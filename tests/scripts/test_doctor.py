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


def _settings(profile: Path, *, toolbar_x: int = 1200, toolbar_y: int = 80) -> Settings:
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
    def __init__(self, url: str) -> None:
        self.url = url


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
    def __init__(self, settings: Settings, *, context: FakeContext) -> None:
        self.settings = settings
        self._context = context
        self.started = False
        self.closed = False

    async def start(self) -> FakeContext:
        self.started = True
        return self._context

    async def close(self) -> None:
        self.closed = True


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

    async def test_reports_warning_for_stale_lock(
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
        assert check.status is CheckStatus.WARNING

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
