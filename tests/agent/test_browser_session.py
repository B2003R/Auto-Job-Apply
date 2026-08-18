"""Tests for launch option construction and atomic profile locking."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.agent import browser_session
from app.agent.browser_session import ProfileLock, build_launch_options
from app.agent.errors import ProfileLockedError
from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        chrome_executable=Path("/usr/bin/google-chrome"),
        chrome_profile_path=tmp_path / "profile",
        jobright_extension_id="abcextensionid",
    )


class TestBuildLaunchOptions:
    def test_ignores_exact_playwright_extension_defaults(self, tmp_path: Path) -> None:
        options = build_launch_options(_settings(tmp_path))

        assert options["ignore_default_args"] == [
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
        ]

    def test_ignore_default_args_has_no_extra_entries(self, tmp_path: Path) -> None:
        options = build_launch_options(_settings(tmp_path))

        ignored = options["ignore_default_args"]
        assert isinstance(ignored, list)
        assert len(ignored) == 2

    def test_is_headed(self, tmp_path: Path) -> None:
        options = build_launch_options(_settings(tmp_path))

        assert options["headless"] is False

    def test_uses_configured_user_data_dir_and_executable(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        options = build_launch_options(settings)

        assert options["user_data_dir"] == str(settings.chrome_profile_path)
        assert options["executable_path"] == str(settings.chrome_executable)

    def test_returns_plain_serializable_args_list(self, tmp_path: Path) -> None:
        options = build_launch_options(_settings(tmp_path))

        assert options["args"] == []


class TestProfileLockAcquireRelease:
    async def test_acquire_creates_lock_file_with_metadata(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = ProfileLock(profile)

        await lock.acquire()
        try:
            assert lock.lock_path.exists()
            data = json.loads(lock.lock_path.read_text(encoding="utf-8"))
            assert data["pid"] == os.getpid()
            assert data["hostname"]
            assert data["acquired_at"]
        finally:
            await lock.release()

    async def test_acquire_creates_profile_directory_if_missing(self, tmp_path: Path) -> None:
        profile = tmp_path / "does-not-exist-yet"
        lock = ProfileLock(profile)

        await lock.acquire()
        try:
            assert profile.exists()
            assert lock.lock_path.exists()
        finally:
            await lock.release()

    async def test_release_removes_lock_file(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = ProfileLock(profile)
        await lock.acquire()

        await lock.release()

        assert not lock.lock_path.exists()

    async def test_release_is_idempotent(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = ProfileLock(profile)
        await lock.acquire()
        await lock.release()

        await lock.release()  # must not raise

    async def test_context_manager_acquires_and_releases(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = ProfileLock(profile)

        async with lock as acquired:
            assert acquired is lock
            assert lock.lock_path.exists()

        assert not lock.lock_path.exists()

    async def test_context_manager_releases_on_exception(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = ProfileLock(profile)

        with pytest.raises(RuntimeError):
            async with lock:
                raise RuntimeError("boom")

        assert not lock.lock_path.exists()

    async def test_reacquire_after_release_succeeds(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        first = ProfileLock(profile)
        await first.acquire()
        await first.release()

        second = ProfileLock(profile)
        await second.acquire()
        try:
            assert second.lock_path.exists()
        finally:
            await second.release()


class TestProfileLockExclusion:
    async def test_second_acquire_raises_profile_locked_error(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        first = ProfileLock(profile)
        await first.acquire()
        second = ProfileLock(profile)

        try:
            with pytest.raises(ProfileLockedError) as excinfo:
                await second.acquire()
            assert excinfo.value.metadata is not None
            assert excinfo.value.metadata.pid == os.getpid()
            assert excinfo.value.stale is False
        finally:
            await first.release()

    async def test_failed_acquire_does_not_delete_existing_lock(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        first = ProfileLock(profile)
        await first.acquire()
        second = ProfileLock(profile)

        with pytest.raises(ProfileLockedError):
            await second.acquire()

        assert first.lock_path.exists()
        await first.release()

    async def test_locked_error_message_is_actionable(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        first = ProfileLock(profile)
        await first.acquire()
        second = ProfileLock(profile)

        try:
            with pytest.raises(ProfileLockedError) as excinfo:
                await second.acquire()
            message = str(excinfo.value)
            assert str(first.lock_path) in message
            assert str(os.getpid()) in message
        finally:
            await first.release()


class TestProfileLockStaleDiagnostics:
    async def test_detects_stale_lock_from_dead_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": 999999, "hostname": "old-host", "acquired_at": "2020-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)

        lock = ProfileLock(profile)
        with pytest.raises(ProfileLockedError) as excinfo:
            await lock.acquire()

        assert excinfo.value.stale is True
        assert excinfo.value.metadata is not None
        assert excinfo.value.metadata.pid == 999999
        assert "stale" in str(excinfo.value).lower()

    async def test_active_lock_from_live_pid_is_not_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": 4242, "hostname": "some-host", "acquired_at": "2020-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: True)

        lock = ProfileLock(profile)
        with pytest.raises(ProfileLockedError) as excinfo:
            await lock.acquire()

        assert excinfo.value.stale is False

    async def test_never_automatically_removes_stale_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text(
            json.dumps({"pid": 999999, "hostname": "old-host", "acquired_at": "2020-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)

        lock = ProfileLock(profile)
        with pytest.raises(ProfileLockedError):
            await lock.acquire()

        # The stale lock file must still exist; the caller must remove it explicitly.
        assert lock_path.exists()

    async def test_corrupt_lock_file_reports_unreadable_metadata_without_crash(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock_path = profile / browser_session.LOCK_FILENAME
        lock_path.write_text("not-json", encoding="utf-8")

        lock = ProfileLock(profile)
        with pytest.raises(ProfileLockedError) as excinfo:
            await lock.acquire()

        assert excinfo.value.metadata is None
        assert excinfo.value.stale is False
        assert lock_path.exists()


class TestProfileLockReleaseSafety:
    async def test_release_only_removes_lock_owned_by_this_instance(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock = ProfileLock(profile)
        await lock.acquire()

        # Simulate the lock file being overwritten by a different owner in between.
        lock.lock_path.write_text(
            json.dumps({"pid": 555555, "hostname": "other", "acquired_at": "later"}),
            encoding="utf-8",
        )

        await lock.release()

        # Must not delete a lock file it no longer recognizes as its own.
        assert lock.lock_path.exists()
