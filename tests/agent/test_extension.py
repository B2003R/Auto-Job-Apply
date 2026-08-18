"""Tests for Chrome preference extension discovery and service worker lookup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.agent.errors import ServiceWorkerNotFoundError
from app.agent.extension import (
    ExtensionInstall,
    find_installed_extension,
    find_service_worker,
    probe_service_worker,
)

EXTENSION_ID = "abcextensionid1234567890abcdefg"


def _write_preferences(path: Path, extensions_settings: dict[str, Any]) -> None:
    path.write_text(
        json.dumps({"extensions": {"settings": extensions_settings}}),
        encoding="utf-8",
    )


def _profile_with_default_dir(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    return profile


class TestFindInstalledExtensionPreferenceFiles:
    def test_found_in_secure_preferences(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        _write_preferences(
            profile / "Default" / "Secure Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "1.2.3"}}},
        )

        result = find_installed_extension(profile, EXTENSION_ID)

        assert isinstance(result, ExtensionInstall)
        assert result.extension_id == EXTENSION_ID
        assert result.enabled is True
        assert result.name == "Jobright Autofill"
        assert result.version == "1.2.3"
        assert result.source_file.name == "Secure Preferences"

    def test_falls_back_to_preferences_when_secure_preferences_missing(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        _write_preferences(
            profile / "Default" / "Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "2.0.0"}}},
        )

        result = find_installed_extension(profile, EXTENSION_ID)

        assert result.enabled is True
        assert result.source_file.name == "Preferences"

    def test_falls_back_to_preferences_when_secure_preferences_lacks_entry(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        _write_preferences(profile / "Default" / "Secure Preferences", {})
        _write_preferences(
            profile / "Default" / "Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "2.0.0"}}},
        )

        result = find_installed_extension(profile, EXTENSION_ID)

        assert result.source_file.name == "Preferences"

    def test_disabled_state_is_reported(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        _write_preferences(
            profile / "Default" / "Secure Preferences",
            {EXTENSION_ID: {"state": 0, "manifest": {"name": "Jobright Autofill", "version": "1.0.0"}}},
        )

        result = find_installed_extension(profile, EXTENSION_ID)

        assert result.enabled is False

    def test_raises_when_absent_from_both_files(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        _write_preferences(profile / "Default" / "Secure Preferences", {})
        _write_preferences(profile / "Default" / "Preferences", {})

        with pytest.raises(Exception) as excinfo:
            find_installed_extension(profile, EXTENSION_ID)

        message = str(excinfo.value)
        assert EXTENSION_ID in message
        assert "Secure Preferences" in message
        assert "Preferences" in message

    def test_raises_when_no_preference_files_exist(self, tmp_path: Path) -> None:
        profile = tmp_path / "empty-profile"
        profile.mkdir()

        with pytest.raises(Exception):
            find_installed_extension(profile, EXTENSION_ID)

    def test_extensions_section_of_unexpected_shape_does_not_crash(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        (profile / "Default" / "Secure Preferences").write_text(
            json.dumps({"extensions": ["unexpected", "shape"]}), encoding="utf-8"
        )
        _write_preferences(
            profile / "Default" / "Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "1.0.0"}}},
        )

        result = find_installed_extension(profile, EXTENSION_ID)

        assert result.source_file.name == "Preferences"

    def test_malformed_secure_preferences_falls_back_to_preferences(self, tmp_path: Path) -> None:
        profile = _profile_with_default_dir(tmp_path)
        (profile / "Default" / "Secure Preferences").write_text("not-json", encoding="utf-8")
        _write_preferences(
            profile / "Default" / "Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "1.0.0"}}},
        )

        result = find_installed_extension(profile, EXTENSION_ID)

        assert result.source_file.name == "Preferences"


class TestFindInstalledExtensionLeafProfileDiagnostic:
    """chrome_profile_path is the Playwright user_data_dir per the approved
    spec; it must never be silently reinterpreted as a leaf Chrome profile.
    These tests only assert the *diagnostic wording* improves when a leaf
    profile (one containing Preferences directly) is misconfigured as the
    root — the lookup path (`<profile>/Default/...`) itself must not change.
    """

    def test_leaf_profile_hint_when_preferences_file_is_at_configured_root(
        self, tmp_path: Path
    ) -> None:
        # User mistakenly pointed chrome_profile_path at what should have
        # been `<user_data_dir>/Default`.
        leaf_profile = tmp_path / "leaf-profile"
        leaf_profile.mkdir()
        _write_preferences(
            leaf_profile / "Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "1.0.0"}}},
        )

        with pytest.raises(Exception) as excinfo:
            find_installed_extension(leaf_profile, EXTENSION_ID)

        message = str(excinfo.value)
        assert "user_data_dir" in message or "user-data" in message
        assert "Default" in message

    def test_leaf_profile_hint_mentions_secure_preferences_variant(self, tmp_path: Path) -> None:
        leaf_profile = tmp_path / "leaf-profile"
        leaf_profile.mkdir()
        _write_preferences(
            leaf_profile / "Secure Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "1.0.0"}}},
        )

        with pytest.raises(Exception) as excinfo:
            find_installed_extension(leaf_profile, EXTENSION_ID)

        assert "Secure Preferences" in str(excinfo.value)

    def test_no_leaf_profile_hint_when_root_has_no_stray_preferences(
        self, tmp_path: Path
    ) -> None:
        profile = _profile_with_default_dir(tmp_path)  # correctly configured root

        with pytest.raises(Exception) as excinfo:
            find_installed_extension(profile, EXTENSION_ID)

        assert "user_data_dir" not in str(excinfo.value)

    def test_still_requires_default_subdirectory_layout(self, tmp_path: Path) -> None:
        """The lookup itself must remain `<profile>/Default/...` — a stray
        root-level Preferences file must never be read as the extension
        source, even when it technically contains a matching entry."""
        leaf_profile = tmp_path / "leaf-profile"
        leaf_profile.mkdir()
        _write_preferences(
            leaf_profile / "Preferences",
            {EXTENSION_ID: {"state": 1, "manifest": {"name": "Jobright Autofill", "version": "1.0.0"}}},
        )

        with pytest.raises(Exception):
            find_installed_extension(leaf_profile, EXTENSION_ID)


class FakeWorker:
    def __init__(
        self,
        url: str,
        *,
        evaluate_result: Any = None,
        evaluate_error: Exception | None = None,
        evaluate_delay: float = 0.0,
        has_evaluate: bool = True,
    ) -> None:
        self.url = url
        self._evaluate_result = evaluate_result
        self._evaluate_error = evaluate_error
        self._evaluate_delay = evaluate_delay
        if has_evaluate:
            self.evaluate = self._evaluate  # type: ignore[assignment]

    async def _evaluate(self, expression: str) -> Any:
        if self._evaluate_delay:
            await asyncio.sleep(self._evaluate_delay)
        if self._evaluate_error is not None:
            raise self._evaluate_error
        return self._evaluate_result


class FakePage:
    def __init__(
        self,
        on_goto: Callable[[str], None] | None = None,
        *,
        goto_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._on_goto = on_goto
        self.goto_urls: list[str] = []
        self.closed = False
        self._goto_error = goto_error
        self._close_error = close_error

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_urls.append(url)
        if self._on_goto is not None:
            self._on_goto(url)
        if self._goto_error is not None:
            raise self._goto_error

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class FakeContext:
    def __init__(
        self,
        service_workers: list[FakeWorker] | None = None,
        *,
        emit_on_wake: FakeWorker | None = None,
        new_page_error: Exception | None = None,
        new_page_hangs: bool = False,
        goto_error: Exception | None = None,
        page_close_error: Exception | None = None,
        remove_listener_error: Exception | None = None,
        supports_remove_listener: bool = True,
    ) -> None:
        self.service_workers = list(service_workers or [])
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._emit_on_wake = emit_on_wake
        self.new_page_calls = 0
        self._new_page_error = new_page_error
        self._new_page_hangs = new_page_hangs
        self._goto_error = goto_error
        self._page_close_error = page_close_error
        self._remove_listener_error = remove_listener_error
        if not supports_remove_listener:
            self.remove_listener = None  # type: ignore[assignment]

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        if self._remove_listener_error is not None:
            raise self._remove_listener_error
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def _emit(self, event: str, payload: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            handler(payload)

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        if self._new_page_hangs:
            await asyncio.sleep(3600)
        if self._new_page_error is not None:
            raise self._new_page_error

        def _on_goto(_url: str) -> None:
            if self._emit_on_wake is not None:
                self._emit("serviceworker", self._emit_on_wake)

        return FakePage(on_goto=_on_goto, goto_error=self._goto_error, close_error=self._page_close_error)


class TestFindServiceWorkerUrlMatching:
    async def test_returns_already_running_worker_matching_extension_id(self) -> None:
        target = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        other = FakeWorker(url="chrome-extension://otherextension/background.js")
        context = FakeContext(service_workers=[other, target])

        result = await find_service_worker(context, EXTENSION_ID, timeout_ms=1000)

        assert result is target
        assert context.new_page_calls == 0

    async def test_ignores_workers_from_other_extensions(self) -> None:
        other = FakeWorker(url="chrome-extension://otherextension/background.js")
        context = FakeContext(service_workers=[other])

        with pytest.raises(ServiceWorkerNotFoundError):
            await find_service_worker(context, EXTENSION_ID, timeout_ms=100)

    async def test_wakes_extension_and_waits_for_new_worker_event(self) -> None:
        target = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[], emit_on_wake=target)

        result = await find_service_worker(context, EXTENSION_ID, timeout_ms=2000)

        assert result is target
        assert context.new_page_calls == 1

    async def test_matches_exact_extension_id_prefix_not_substring(self) -> None:
        decoy = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}xxx/background.js")
        context = FakeContext(service_workers=[decoy])

        with pytest.raises(ServiceWorkerNotFoundError):
            await find_service_worker(context, EXTENSION_ID, timeout_ms=100)

    async def test_timeout_raises_service_worker_not_found_with_metadata(self) -> None:
        context = FakeContext(service_workers=[])

        with pytest.raises(ServiceWorkerNotFoundError) as excinfo:
            await find_service_worker(context, EXTENSION_ID, timeout_ms=100)

        assert excinfo.value.extension_id == EXTENSION_ID
        assert excinfo.value.timeout_ms == 100

    async def test_cleans_up_event_listener_after_success(self) -> None:
        target = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(service_workers=[], emit_on_wake=target)

        await find_service_worker(context, EXTENSION_ID, timeout_ms=2000)

        assert context._handlers.get("serviceworker", []) == []

    async def test_cleans_up_event_listener_after_timeout(self) -> None:
        context = FakeContext(service_workers=[])

        with pytest.raises(ServiceWorkerNotFoundError):
            await find_service_worker(context, EXTENSION_ID, timeout_ms=50)

        assert context._handlers.get("serviceworker", []) == []


class TestFindServiceWorkerBoundedWake:
    async def test_bounds_total_time_when_new_page_hangs_forever(self) -> None:
        context = FakeContext(service_workers=[], new_page_hangs=True)

        started = asyncio.get_event_loop().time()
        with pytest.raises(ServiceWorkerNotFoundError):
            await asyncio.wait_for(
                find_service_worker(context, EXTENSION_ID, timeout_ms=100), timeout=2.0
            )
        elapsed = asyncio.get_event_loop().time() - started

        assert elapsed < 1.0

    async def test_swallows_new_page_failure_and_still_times_out(self) -> None:
        context = FakeContext(service_workers=[], new_page_error=RuntimeError("no pages allowed"))

        with pytest.raises(ServiceWorkerNotFoundError):
            await find_service_worker(context, EXTENSION_ID, timeout_ms=100)

    async def test_swallows_goto_failure_and_still_times_out(self) -> None:
        context = FakeContext(service_workers=[], goto_error=RuntimeError("net::ERR_FAILED"))

        with pytest.raises(ServiceWorkerNotFoundError):
            await find_service_worker(context, EXTENSION_ID, timeout_ms=100)

    async def test_swallows_page_close_failure_and_still_finds_worker(self) -> None:
        target = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(
            service_workers=[], emit_on_wake=target, page_close_error=RuntimeError("close boom")
        )

        result = await find_service_worker(context, EXTENSION_ID, timeout_ms=2000)

        assert result is target

    async def test_works_when_context_lacks_remove_listener(self) -> None:
        target = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(
            service_workers=[], emit_on_wake=target, supports_remove_listener=False
        )

        result = await find_service_worker(context, EXTENSION_ID, timeout_ms=2000)

        assert result is target

    async def test_swallows_remove_listener_failure(self) -> None:
        target = FakeWorker(url=f"chrome-extension://{EXTENSION_ID}/background.js")
        context = FakeContext(
            service_workers=[],
            emit_on_wake=target,
            remove_listener_error=RuntimeError("listener gone"),
        )

        result = await find_service_worker(context, EXTENSION_ID, timeout_ms=2000)

        assert result is target


class TestProbeServiceWorker:
    async def test_succeeds_when_evaluate_resolves(self) -> None:
        worker = FakeWorker(url="chrome-extension://x/background.js", evaluate_result=2)

        await probe_service_worker(worker, EXTENSION_ID, timeout_ms=1000)  # must not raise

    async def test_raises_service_worker_not_found_when_evaluate_raises(self) -> None:
        worker = FakeWorker(
            url="chrome-extension://x/background.js",
            evaluate_error=RuntimeError("worker terminated"),
        )

        with pytest.raises(ServiceWorkerNotFoundError) as excinfo:
            await probe_service_worker(worker, EXTENSION_ID, timeout_ms=1000)

        assert excinfo.value.extension_id == EXTENSION_ID

    async def test_raises_service_worker_not_found_when_evaluate_times_out(self) -> None:
        worker = FakeWorker(url="chrome-extension://x/background.js", evaluate_delay=5.0)

        started = asyncio.get_event_loop().time()
        with pytest.raises(ServiceWorkerNotFoundError):
            await probe_service_worker(worker, EXTENSION_ID, timeout_ms=50)
        elapsed = asyncio.get_event_loop().time() - started

        assert elapsed < 1.0

    async def test_raises_service_worker_not_found_when_worker_lacks_evaluate(self) -> None:
        worker = FakeWorker(url="chrome-extension://x/background.js", has_evaluate=False)

        with pytest.raises(ServiceWorkerNotFoundError):
            await probe_service_worker(worker, EXTENSION_ID, timeout_ms=1000)
