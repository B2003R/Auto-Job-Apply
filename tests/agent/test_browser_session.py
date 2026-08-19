"""Tests for launch option construction and atomic profile locking."""

from __future__ import annotations

import asyncio
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
    describe_chrome_singleton_locks,
    detect_chrome_singleton_locks,
)
from app.agent.errors import (
    ProfileInUseError,
    ProfileLockedError,
    ProfileMissingError,
    TeardownError,
)
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


class TestDescribeChromeSingletonLocks:
    """Richer diagnostics than `detect_chrome_singleton_locks`: parses the
    `SingletonLock` symlink's `<hostname>-<pid>` target and reports
    active/stale/unknown, without ever changing whether the marker is
    treated as present (that decision still belongs to the simple filename
    list returned by `detect_chrome_singleton_locks`).
    """

    def test_returns_empty_list_when_nothing_present(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()

        assert describe_chrome_singleton_locks(profile) == []

    def test_reports_active_for_symlink_target_with_live_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("some-host-4242")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: True)

        infos = describe_chrome_singleton_locks(profile)

        assert len(infos) == 1
        info = infos[0]
        assert info.filename == "SingletonLock"
        assert info.status == "active"
        assert info.hostname == "some-host"
        assert info.pid == 4242

    def test_reports_stale_for_symlink_target_with_dead_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("gone-host-999999")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)

        infos = describe_chrome_singleton_locks(profile)

        assert len(infos) == 1
        info = infos[0]
        assert info.status == "stale"
        assert info.hostname == "gone-host"
        assert info.pid == 999999

    def test_reports_unknown_for_regular_file_singleton_marker(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonSocket").write_text("x", encoding="utf-8")

        infos = describe_chrome_singleton_locks(profile)

        assert len(infos) == 1
        info = infos[0]
        assert info.filename == "SingletonSocket"
        assert info.status == "unknown"
        assert info.hostname is None
        assert info.pid is None

    def test_reports_unknown_for_malformed_symlink_target(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("not-a-hostname-pid-pair")

        infos = describe_chrome_singleton_locks(profile)

        assert infos[0].status == "unknown"
        assert infos[0].pid is None

    def test_reports_unknown_for_path_like_symlink_target(self, tmp_path: Path) -> None:
        """A target containing '/' is not a plausible `hostname-pid` pair
        (real Chrome never encodes a path); must not be misparsed."""
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to(str(profile / "nonexistent-host-1234"))

        infos = describe_chrome_singleton_locks(profile)

        assert infos[0].status == "unknown"

    def test_hostname_containing_hyphens_is_parsed_via_rightmost_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("my-host-name-7777")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: True)

        infos = describe_chrome_singleton_locks(profile)

        assert infos[0].hostname == "my-host-name"
        assert infos[0].pid == 7777

    def test_describes_all_present_markers_independently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("host-1111")
        (profile / "SingletonSocket").write_text("x", encoding="utf-8")
        (profile / "SingletonCookie").write_text("y", encoding="utf-8")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: True)

        infos = describe_chrome_singleton_locks(profile)

        by_name = {info.filename: info for info in infos}
        assert set(by_name) == {"SingletonLock", "SingletonSocket", "SingletonCookie"}
        assert by_name["SingletonLock"].status == "active"
        assert by_name["SingletonSocket"].status == "unknown"
        assert by_name["SingletonCookie"].status == "unknown"

    def test_never_deletes_any_singleton_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        lock_path = profile / "SingletonLock"
        lock_path.symlink_to("host-1111")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)

        describe_chrome_singleton_locks(profile)

        assert lock_path.is_symlink()

    def test_detect_still_returns_plain_filenames_regardless_of_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward-compatible presence check must be unaffected by the
        richer active/stale/unknown parsing added to `describe_*`."""
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("host-1111")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)

        assert detect_chrome_singleton_locks(profile) == ["SingletonLock"]


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


class FlakyReleaseLock:
    """A `ProfileLock`-shaped fake whose `release()` fails a fixed number of
    times before delegating to a real `ProfileLock`, simulating a transient
    failure that a later retry can recover from."""

    def __init__(self, path: Path, *, fail_times: int) -> None:
        self._inner = ProfileLock(path)
        self._remaining_failures = fail_times
        self.release_calls = 0

    @property
    def lock_path(self) -> Path:
        return self._inner.lock_path

    async def acquire(self) -> None:
        await self._inner.acquire()

    async def release(self) -> None:
        self.release_calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("transient release failure")
        await self._inner.release()


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

    async def test_singleton_error_message_reports_active_status_with_pid_and_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("some-host-4242")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: True)
        settings = _settings(tmp_path)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(FakePlaywright(FakeChromium())))

        with pytest.raises(ProfileInUseError) as excinfo:
            await session.start()

        message = str(excinfo.value)
        assert "active" in message
        assert "4242" in message
        assert "some-host" in message

    async def test_singleton_error_message_reports_stale_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("gone-host-999999")
        monkeypatch.setattr(browser_session, "_pid_alive", lambda pid: False)
        settings = _settings(tmp_path)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(FakePlaywright(FakeChromium())))

        with pytest.raises(ProfileInUseError) as excinfo:
            await session.start()

        assert "stale" in str(excinfo.value)

    async def test_launch_failure_attaches_stop_failure_as_note_on_primary_exception(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(launch_error=RuntimeError("original launch failure"))
        playwright = FakePlaywright(chromium, stop_error=RuntimeError("stop exploded"))
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        with pytest.raises(RuntimeError, match="original launch failure") as excinfo:
            await session.start()

        notes = getattr(excinfo.value, "__notes__", [])
        assert any("stop exploded" in note for note in notes)

    async def test_launch_failure_retains_lock_for_retry_and_close_can_recover_it(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(launch_error=RuntimeError("original launch failure"))
        playwright = FakePlaywright(chromium)
        session = BrowserSession(
            settings,
            playwright_factory=_playwright_factory(playwright),
            lock_factory=lambda path: FlakyReleaseLock(path, fail_times=1),
        )

        with pytest.raises(RuntimeError, match="original launch failure") as excinfo:
            await session.start()

        # The primary launch failure must still be preserved as the raised
        # exception, with the release failure attached as a note.
        notes = getattr(excinfo.value, "__notes__", [])
        assert any("transient release failure" in note for note in notes)
        # The lock must be retained (not silently dropped) so a later close()
        # can retry releasing it.
        assert (profile / browser_session.LOCK_FILENAME).exists()

        await session.close()  # retry succeeds this time (fail_times exhausted)

        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_singleton_error_message_reports_unknown_status(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "SingletonSocket").write_text("x", encoding="utf-8")
        settings = _settings(tmp_path)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(FakePlaywright(FakeChromium())))

        with pytest.raises(ProfileInUseError) as excinfo:
            await session.start()

        assert "unknown" in str(excinfo.value)


class ControlledChromium(FakeChromium):
    """A `FakeChromium` whose `launch_persistent_context` blocks on an
    `asyncio.Event` before proceeding, so tests can deterministically observe
    a `start()` call that is still in-flight."""

    def __init__(self, ready: asyncio.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ready = ready

    async def launch_persistent_context(self, user_data_dir: Any, **kwargs: Any) -> FakeContext:
        await self._ready.wait()
        return await super().launch_persistent_context(user_data_dir, **kwargs)


class TestBrowserSessionConcurrency:
    async def test_concurrent_starts_launch_only_once_and_share_context(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        first, second = await asyncio.gather(session.start(), session.start())

        assert first is second
        assert len(chromium.launch_calls) == 1
        await session.close()

    async def test_concurrent_starts_do_not_collide_on_own_profile_lock(
        self, tmp_path: Path
    ) -> None:
        """Two callers racing to start the *same* BrowserSession instance
        must never see the second call fail with ProfileLockedError against
        its own first call's lock."""
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        ready = asyncio.Event()
        ready.set()
        chromium = ControlledChromium(ready)
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        results = await asyncio.gather(
            session.start(), session.start(), session.start(), return_exceptions=True
        )

        assert all(not isinstance(r, BaseException) for r in results)
        await session.close()

    async def test_close_during_in_flight_start_waits_then_closes_cleanly(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        ready = asyncio.Event()
        chromium = ControlledChromium(ready)
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))

        start_task = asyncio.create_task(session.start())
        await asyncio.sleep(0)  # let start() begin and block inside launch_persistent_context

        close_task = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert not close_task.done()  # close() must wait for the in-flight start()

        ready.set()  # allow launch_persistent_context to proceed
        context = await asyncio.wait_for(start_task, timeout=2.0)
        await asyncio.wait_for(close_task, timeout=2.0)

        assert len(chromium.launch_calls) == 1
        assert context.close_calls == 1
        assert playwright.stop_calls == 1
        assert not (profile / browser_session.LOCK_FILENAME).exists()

    async def test_concurrent_close_calls_are_serialized_and_idempotent(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))
        await session.start()

        await asyncio.gather(session.close(), session.close())

        assert chromium.context.close_calls == 1
        assert playwright.stop_calls == 1


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

    async def test_close_retains_lock_for_retry_when_release_fails(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium()
        playwright = FakePlaywright(chromium)
        session = BrowserSession(
            settings,
            playwright_factory=_playwright_factory(playwright),
            lock_factory=lambda path: FlakyReleaseLock(path, fail_times=1),
        )
        await session.start()

        with pytest.raises(RuntimeError, match="transient release failure"):
            await session.close()

        # Lock must still be on disk and retained for a retry, not silently
        # dropped after the failed release attempt.
        assert (profile / browser_session.LOCK_FILENAME).exists()

        await session.close()  # retry succeeds (fail_times exhausted)

        assert not (profile / browser_session.LOCK_FILENAME).exists()
        # context/playwright must not be re-touched on the retry.
        assert chromium.context.close_calls == 1
        assert playwright.stop_calls == 1

    async def test_close_with_multiple_failures_raises_aggregate_prioritizing_lock(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(context=FakeContext(close_error=RuntimeError("context close boom")))
        playwright = FakePlaywright(chromium, stop_error=RuntimeError("stop boom"))
        release_error = RuntimeError("lock release boom")
        session = BrowserSession(
            settings,
            playwright_factory=_playwright_factory(playwright),
            lock_factory=lambda path: RaisingReleaseLock(path, release_error=release_error),
        )
        await session.start()

        with pytest.raises(TeardownError) as excinfo:
            await session.close()

        message = str(excinfo.value)
        # Every failure is surfaced, not just the first one encountered.
        assert "context close boom" in message
        assert "stop boom" in message
        assert "lock release boom" in message
        # The lock failure is called out distinctly (lock-prioritized).
        assert "lock" in message.lower()
        assert len(excinfo.value.failures) == 3
        # All three steps were still attempted despite earlier failures.
        assert chromium.context.close_calls == 1
        assert playwright.stop_calls == 1

    async def test_close_with_single_failure_still_raises_raw_exception(
        self, tmp_path: Path
    ) -> None:
        """Aggregate wrapping is reserved for genuinely multi-step failures;
        a single failing step should still raise its own exception directly
        so existing narrow `except SomeSpecificError` callers keep working."""
        profile = tmp_path / "profile"
        profile.mkdir()
        settings = _settings(tmp_path)
        chromium = FakeChromium(context=FakeContext(close_error=RuntimeError("only this fails")))
        playwright = FakePlaywright(chromium)
        session = BrowserSession(settings, playwright_factory=_playwright_factory(playwright))
        await session.start()

        with pytest.raises(RuntimeError, match="only this fails") as excinfo:
            await session.close()

        assert not isinstance(excinfo.value, TeardownError)
