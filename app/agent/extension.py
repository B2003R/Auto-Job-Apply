"""Chrome profile extension verification and service worker discovery.

Playwright itself is never imported here; `find_service_worker` operates on a
duck-typed `context` (matching Playwright's `BrowserContext`: `.service_workers`,
`.on`/`.remove_listener`, `.new_page()`), so tests can supply lightweight fakes
without Playwright or a browser binary being installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.agent.errors import (
    ExtensionNotFoundError,
    ServiceWorkerNotFoundError,
    ServiceWorkerUnresponsiveError,
)

_PROFILE_DIRECTORY = "Default"
#: Checked in this order: Secure Preferences is the tamper-evident source of
#: truth in modern Chrome; Preferences is checked as a fallback.
_PREFERENCE_FILENAMES = ("Secure Preferences", "Preferences")
_STATE_ENABLED = 1


@dataclass(frozen=True)
class ExtensionInstall:
    """Extension installation facts read from Chrome profile preferences."""

    extension_id: str
    name: str | None
    version: str | None
    enabled: bool
    source_file: Path


def find_installed_extension(profile: Path, extension_id: str) -> ExtensionInstall:
    """Verify `extension_id` is installed by reading Chrome profile preferences.

    Checks both `Secure Preferences` and `Preferences` under the profile's
    `Default` directory, since Chrome's extension bookkeeping may live in
    either file depending on version and installation method.

    `profile` is always treated as the Playwright `user_data_dir` (per the
    approved spec) — it is never reinterpreted as a leaf Chrome profile
    directory. If a leaf profile (one containing `Preferences` directly) is
    misconfigured as `profile`, the raised diagnostic explicitly names the
    problem instead of silently trying to read it as the profile root.
    """
    profile = Path(profile)
    profile_dir = profile / _PROFILE_DIRECTORY
    diagnostics: list[str] = []

    for filename in _PREFERENCE_FILENAMES:
        pref_path = profile_dir / filename
        if not pref_path.exists():
            diagnostics.append(f"{filename} not found at {pref_path}")
            continue

        try:
            data = json.loads(pref_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            diagnostics.append(f"{filename} unreadable: {exc}")
            continue

        extensions_section = data.get("extensions") if isinstance(data, dict) else None
        settings_map = (
            extensions_section.get("settings") if isinstance(extensions_section, dict) else None
        )
        entry = settings_map.get(extension_id) if isinstance(settings_map, dict) else None
        if entry is None:
            diagnostics.append(f"extension {extension_id} absent from {filename}")
            continue

        manifest = entry.get("manifest") if isinstance(entry, dict) else None
        manifest = manifest if isinstance(manifest, dict) else {}
        state = entry.get("state") if isinstance(entry, dict) else None
        return ExtensionInstall(
            extension_id=extension_id,
            name=manifest.get("name"),
            version=manifest.get("version"),
            enabled=state == _STATE_ENABLED,
            source_file=pref_path,
        )

    leaf_hint = _leaf_profile_hint(profile)
    if leaf_hint is not None:
        diagnostics.append(leaf_hint)

    reason = "; ".join(diagnostics) if diagnostics else "no preference files found"
    raise ExtensionNotFoundError(profile, extension_id, reason)


def _leaf_profile_hint(profile: Path) -> str | None:
    """Detect the common misconfiguration of pointing `chrome_profile_path`
    at a leaf Chrome profile (e.g. `.../Default`) instead of the Playwright
    `user_data_dir` root that must *contain* a `Default` subdirectory.

    This never changes lookup behavior — `profile/Default/...` is still the
    only path read — it only improves the error message when that lookup
    fails and a leaf-profile marker is found directly at `profile`.
    """
    for filename in _PREFERENCE_FILENAMES:
        if (profile / filename).exists():
            return (
                f"{profile} appears to be a Chrome profile directory itself "
                f"(it contains {filename} directly), not a user_data_dir root. "
                f"chrome_profile_path must be the Playwright user_data_dir "
                f"whose '{_PROFILE_DIRECTORY}' subdirectory contains "
                f"{filename} — point it one level up at the user-data root, "
                "not at the leaf profile."
            )
    return None


def _matches_extension(worker: Any, target_prefix: str) -> bool:
    return str(getattr(worker, "url", "")).startswith(target_prefix)


async def _wake_extension(context: Any, extension_id: str) -> None:
    """Best-effort attempt to wake a dormant MV3 service worker.

    Navigating a page to the extension's own manifest URL is enough to make
    Chrome spin the extension's service worker back up if it had gone idle.
    Every step (`new_page`, `goto`, `close`) is independently exception-safe:
    any failure here is swallowed so the caller's overall timeout is the only
    thing that determines success or failure (surfaced as
    `ServiceWorkerNotFoundError`), never a raw exception from this helper.
    """
    try:
        page = await context.new_page()
    except Exception:
        return

    try:
        await page.goto(f"chrome-extension://{extension_id}/manifest.json")
    except Exception:
        pass
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def find_service_worker(context: Any, extension_id: str, timeout_ms: int) -> Any:
    """Find (or wake and wait for) the extension's MV3 service worker.

    Matches worker URLs by exact `chrome-extension://<extension_id>/` prefix so
    a similarly-prefixed but distinct extension id can never match.

    `timeout_ms` bounds the *entire* operation — including the wake attempt —
    not just the final wait. A wake step that hangs (e.g. a `new_page()` call
    that never returns) cannot make this function block past `timeout_ms`.
    """
    target_prefix = f"chrome-extension://{extension_id}/"

    for worker in getattr(context, "service_workers", []):
        if _matches_extension(worker, target_prefix):
            return worker

    loop = asyncio.get_running_loop()
    found: "asyncio.Future[Any]" = loop.create_future()

    def _on_service_worker(worker: Any) -> None:
        if not found.done() and _matches_extension(worker, target_prefix):
            found.set_result(worker)

    listener_attached = False
    on_ = getattr(context, "on", None)
    if on_ is not None:
        on_("serviceworker", _on_service_worker)
        listener_attached = True

    async def _wake_and_wait() -> Any:
        wake_task = asyncio.ensure_future(_wake_extension(context, extension_id))
        try:
            return await found
        finally:
            if not wake_task.done():
                wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await wake_task

    try:
        return await asyncio.wait_for(_wake_and_wait(), timeout=timeout_ms / 1000)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ServiceWorkerNotFoundError(extension_id, timeout_ms) from exc
    finally:
        if listener_attached:
            remove_listener: Callable[[str, Any], None] | None = getattr(
                context, "remove_listener", None
            )
            if remove_listener is not None:
                with contextlib.suppress(Exception):
                    remove_listener("serviceworker", _on_service_worker)


async def probe_service_worker(worker: Any, extension_id: str, timeout_ms: int) -> None:
    """Confirm a discovered service worker actually responds to evaluation.

    A `Worker` handle can remain in Playwright's bookkeeping even after the
    underlying MV3 worker has been torn down; a bounded `evaluate()` call is
    the only reliable liveness signal, so doctor never reports a worker
    healthy purely because Playwright still has a reference to it.

    Raises `ServiceWorkerUnresponsiveError` (never `ServiceWorkerNotFoundError`)
    on any failure here: this function is only ever called on a worker that
    `find_service_worker` already discovered, so "not found" wording would be
    inaccurate — the worker exists, it just isn't answering.
    """
    evaluate = getattr(worker, "evaluate", None)
    if evaluate is None:
        raise ServiceWorkerUnresponsiveError(
            extension_id, timeout_ms, reason="worker handle has no evaluate() method"
        )
    try:
        await asyncio.wait_for(evaluate("1 + 1"), timeout=timeout_ms / 1000)
    except Exception as exc:
        raise ServiceWorkerUnresponsiveError(
            extension_id, timeout_ms, reason=f"evaluate() failed: {exc}"
        ) from exc
