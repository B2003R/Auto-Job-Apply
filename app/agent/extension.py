"""Chrome profile extension verification and service worker discovery.

Playwright itself is never imported here; `find_service_worker` operates on a
duck-typed `context` (matching Playwright's `BrowserContext`: `.service_workers`,
`.on`/`.remove_listener`, `.new_page()`), so tests can supply lightweight fakes
without Playwright or a browser binary being installed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.agent.errors import ExtensionNotFoundError, ServiceWorkerNotFoundError

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
    """
    profile_dir = Path(profile) / _PROFILE_DIRECTORY
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

    reason = "; ".join(diagnostics) if diagnostics else "no preference files found"
    raise ExtensionNotFoundError(Path(profile), extension_id, reason)


def _matches_extension(worker: Any, target_prefix: str) -> bool:
    return str(getattr(worker, "url", "")).startswith(target_prefix)


async def _wake_extension(context: Any, extension_id: str) -> None:
    """Best-effort attempt to wake a dormant MV3 service worker.

    Navigating a page to the extension's own manifest URL is enough to make
    Chrome spin the extension's service worker back up if it had gone idle.
    Failures here are non-fatal: the caller still waits for the event.
    """
    page = await context.new_page()
    try:
        await page.goto(f"chrome-extension://{extension_id}/manifest.json")
    except Exception:
        pass
    finally:
        await page.close()


async def find_service_worker(context: Any, extension_id: str, timeout_ms: int) -> Any:
    """Find (or wake and wait for) the extension's MV3 service worker.

    Matches worker URLs by exact `chrome-extension://<extension_id>/` prefix so
    a similarly-prefixed but distinct extension id can never match.
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

    context.on("serviceworker", _on_service_worker)
    try:
        await _wake_extension(context, extension_id)
        return await asyncio.wait_for(found, timeout=timeout_ms / 1000)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ServiceWorkerNotFoundError(extension_id, timeout_ms) from exc
    finally:
        remove_listener: Callable[[str, Any], None] | None = getattr(
            context, "remove_listener", None
        )
        if remove_listener is not None:
            remove_listener("serviceworker", _on_service_worker)
