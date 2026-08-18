"""Tests for launch option construction and atomic profile locking."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.agent import browser_session
from app.agent.browser_session import (
    BrowserSession,
    ProfileLock,
    build_launch_options,
    detect_chrome_singleton_locks,
)
from app.agent.errors import ProfileInUseError, ProfileLockedError, ProfileMissingError
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


class TestDetectChromeSingletonLocks:
    def test_returns_empty_when_no_singleton_files_present(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()

        assert detect_chrome_singleton_locks(profile) == []

    def test_detects_singleton_lock_file(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").write_text("x", encoding="utf-8")

        found = detect_chrome_singleton_locks(profile)

        assert "SingletonLock" in found

    def test_detects_all_three_singleton_markers(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").write_text("x", encoding="utf-8")
        (profile / "SingletonSocket").write_text("x", encoding="utf-8")
        (profile / "SingletonCookie").write_text("x", encoding="utf-8")

        found = detect_chrome_singleton_locks(profile)

        assert set(found) == {"SingletonLock", "SingletonSocket", "SingletonCookie"}

    def test_detects_dangling_symlink_singleton_lock(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        singleton = profile / "SingletonLock"
        singleton.symlink_to(profile / "nonexistent-host-1234")

        found = detect_chrome_singleton_locks(profile)

        assert "SingletonLock" in found

    def test_does_not_detect_singleton_files_nested_under_default(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "SingletonLock").write_text("x", encoding="utf-8")

        assert detect_chrome_singleton_locks(profile) == []


# --- Fakes for BrowserSession lifecycle tests -------------------------------


class FakeContext:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_calls = 0
        self._close_error = close_error

    async def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class FakeChromium:
    def __init__(
        self,
        *,
        context: FakeContext | None = None,
        launch_error: Exception | None = None,
    ) -> None:
        self.context = context if context is not None else FakeContext()
        self.launch_error = launch_error
        self.launch_calls: list[tuple[Any, dict[str, Any]]] = []

    async def launch_persistent_context(self, user_data_dir: Any, **kwargs: Any) -> FakeContext:
        self.launch_calls.append((user_data_dir, kwargs))
        if self.launch_error is not None:
            raise self.launch_error
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium, *, stop_error: Exception | None = None) -> None:
        self.chromium = chromium
        self.stop_calls = 0
        self._stop_error = stop_error

    async def stop(self) -> None:
        self.stop_calls += 1
        if self._stop_error is not None:
            raise self._stop_error


def _playwright_factory(playwright: FakePlaywright) -> Any:
    async def factory() -> FakePlaywright:
        return playwright

    return factory


def _failing_playwright_factory(exc: Exception) -> Any:
    async def factory() -> Any:
        raise exc

    return factory


class RaisingReleaseLock:
    """A `ProfileLock`-shaped fake whose `release()` always fails without
    removing the underlying lock file, simulating a filesystem permission
    problem during teardown."""

    def __init__(self, path: Path, *, release_error: Exception) -> None:
        self._inner = ProfileLock(path)
        self._release_error = release_error
        self.release_calls = 0

    @property
    def lock_path(self) -> Path:
        return self._inner.lock_path

    async def acquire(self) -> None:
        await self._inner.acquire()

    async def release(self) -> None:
        self.release_calls += 1
        raise self._release_error


class TestBrowserSessionStart:
    async def test_raises_profile_missing_and_never_touches_playwright(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path / "missing-profile")
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        with pytest.raises(ProfileMissingError):
            await session.start()

        assert chromium.launch_calls == []

    async def test_acquires_lock_and_returns_context(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        context = await session.start()

        assert context is chromium.context
        assert (profile / browser_session.LOCK_FILENAME).exists()
        await session.close()

    async def test_start_is_idempotent_returns_same_context(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        first = await session.start()
        second = await session.start()

        assert first is second
        assert len(chromium.launch_calls) == 1
        await session.close()

    async def test_launch_failure_releases_lock(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(launch_error=RuntimeError("boom"))
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        with pytest.raises(RuntimeError, match="boom"):
            await session.start()

        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_launch_failure_stops_playwright(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(launch_error=RuntimeError("boom"))
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        with pytest.raises(RuntimeError):
            await session.start()

        assert playwright.stop_calls == 1

    async def test_playwright_factory_failure_releases_lock(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        session = BrowserSession(
            settings, playwright_factory=_failing_playwright_factory(RuntimeError("no driver"))
        )

        with pytest.raises(RuntimeError, match="no driver"):
            await session.start()

        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_launch_failure_preserves_original_error_even_if_stop_raises(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(launch_error=RuntimeError("original launch failure"))
        playwright = FakePlaywright(chromium, stop_error=RuntimeError("stop exploded"))
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        with pytest.raises(RuntimeError, match="original launch failure"):
            await session.start()

        # Lock must still be released even though playwright.stop() raised.
        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_launch_failure_preserves_original_error_even_if_lock_release_raises(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(launch_error=RuntimeError("original launch failure"))
        playwright = FakePlaywright(chromium)
        release_error = RuntimeError("release exploded")
        session = BrowserSession(
            settings,
            playwright_factory=_playwright_factory(playwright),
            lock_factory=lambda path: RaisingReleaseLock(path, release_error=release_error),
        )

        with pytest.raises(RuntimeError, match="original launch failure"):
            await session.start()

        # Playwright must still be stopped even though lock.release() raised.
        assert playwright.stop_calls == 1

    async def test_detects_chrome_singleton_lock_before_launching_and_releases_own_lock(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").write_text("x", encoding="utf-8")
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        with pytest.raises(ProfileInUseError) as excinfo:
            await session.start()

        assert "SingletonLock" in str(excinfo.value)
        assert chromium.launch_calls == []
        # Never delete Chrome's own singleton marker.
        assert (profile / "SingletonLock").exists()
        # This agent's own lock must not be leaked after the aborted start.
        assert not (profile / browser_session.LOCK_FILENAME).exists()


class TestBrowserSessionClose:
    async def test_close_before_start_is_a_no_op(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "profile")
        session = BrowserSession(settings)

        await session.close()  # must not raise

    async def test_close_closes_context_stops_playwright_and_releases_lock(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))
        await session.start()

        await session.close()

        assert chromium.context.close_calls == 1
        assert playwright.stop_calls == 1
        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))
        await session.start()
        await session.close()

        await session.close()  # must not raise, and must not double-act

        assert chromium.context.close_calls == 1
        assert playwright.stop_calls == 1

    async def test_close_still_stops_playwright_and_releases_lock_when_context_close_raises(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(context=FakeContext(close_error=RuntimeError("close boom")))
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))
        await session.start()

        with pytest.raises(RuntimeError, match="close boom"):
            await session.close()

        assert playwright.stop_calls == 1
        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_close_is_idempotent_after_a_raising_close(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(context=FakeContext(close_error=RuntimeError("close boom")))
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))
        await session.start()

        with pytest.raises(RuntimeError):
            await session.close()

        await session.close()  # second call must be a clean no-op

        assert chromium.context.close_calls == 1
        assert playwright.stop_calls == 1
