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


class FakeWorker:
    def __init__(self, url: str) -> None:
        self.url = url


class FakePage:
    def __init__(self, on_goto: Callable[[str], None] | None = None) -> None:
        self._on_goto = on_goto
        self.goto_urls: list[str] = []
        self.closed = False

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_urls.append(url)
        if self._on_goto is not None:
            self._on_goto(url)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(
        self,
        service_workers: list[FakeWorker] | None = None,
        *,
        emit_on_wake: FakeWorker | None = None,
    ) -> None:
        self.service_workers = list(service_workers or [])
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._emit_on_wake = emit_on_wake
        self.new_page_calls = 0

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def _emit(self, event: str, payload: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            handler(payload)

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1

        def _on_goto(_url: str) -> None:
            if self._emit_on_wake is not None:
                self._emit("serviceworker", self._emit_on_wake)

        return FakePage(on_goto=_on_goto)


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
