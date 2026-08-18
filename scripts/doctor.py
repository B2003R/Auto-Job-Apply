"""Preflight diagnostics for the persistent browser profile and extension.

Distinguishes actionable failure categories (missing profile, locked profile,
missing extension, dead service worker, missing xdotool, invalid toolbar
calibration) without requiring a real browser: `session_factory` defaults to
the real `BrowserSession` but can be swapped for a fake in tests.

Every check is wrapped so that filesystem errors and unexpected
browser/worker errors always become a categorized `ERROR` check — this
script never lets an unhandled exception surface as a raw traceback.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

from app.agent.browser_session import BrowserSession, ProfileLock, describe_chrome_singleton_locks
from app.agent.errors import (
    ExtensionNotFoundError,
    ProfileLockedError,
    ServiceWorkerNotFoundError,
    ServiceWorkerUnresponsiveError,
)
from app.agent.extension import find_installed_extension, find_service_worker, probe_service_worker
from app.config import Settings

DEFAULT_SERVICE_WORKER_TIMEOUT_MS = 5000


class DoctorCategory(str, Enum):
    PROFILE_MISSING = "profile_missing"
    PROFILE_LOCKED = "profile_locked"
    EXTENSION_MISSING = "extension_missing"
    SERVICE_WORKER_DEAD = "service_worker_dead"
    XDOTOOL_MISSING = "xdotool_missing"
    CALIBRATION_INVALID = "calibration_invalid"


class CheckStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class DoctorCheck:
    category: DoctorCategory
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]

    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.ERROR for check in self.checks)


SessionFactory = Callable[[Settings], Any]


async def run_doctor(
    settings: Settings,
    *,
    session_factory: SessionFactory | None = None,
    service_worker_timeout_ms: int = DEFAULT_SERVICE_WORKER_TIMEOUT_MS,
) -> DoctorReport:
    checks: list[DoctorCheck] = []
    profile_path = settings.chrome_profile_path
    factory = session_factory or BrowserSession

    if not profile_path.exists():
        checks.append(
            DoctorCheck(
                DoctorCategory.PROFILE_MISSING,
                CheckStatus.ERROR,
                f"Profile directory missing: {profile_path}",
            )
        )
        checks.append(_xdotool_check())
        checks.append(_calibration_check(settings))
        return DoctorReport(checks)

    checks.append(
        DoctorCheck(
            DoctorCategory.PROFILE_MISSING,
            CheckStatus.OK,
            f"Profile directory present: {profile_path}",
        )
    )

    checks.append(_extension_check(settings))

    lock_check, lock_available = await _lock_check(profile_path)
    checks.append(lock_check)

    if lock_available:
        checks.append(
            await _service_worker_check(settings, factory, service_worker_timeout_ms)
        )
    else:
        checks.append(
            DoctorCheck(
                DoctorCategory.SERVICE_WORKER_DEAD,
                CheckStatus.WARNING,
                "Skipped service worker check: profile lock unavailable",
            )
        )

    checks.append(_xdotool_check())
    checks.append(_calibration_check(settings))
    return DoctorReport(checks)


async def _lock_check(profile_path: Path) -> tuple[DoctorCheck, bool]:
    """Verify the profile is not locked by this agent or by Chrome itself.

    Both a stale and an active `.job-apply-lock.json` are reported as
    `ERROR` (the lock is never auto-removed either way; a stale lock just
    means it is *probably* safe for an operator to remove it manually).
    Chrome's own `SingletonLock`/`SingletonSocket`/`SingletonCookie` markers
    are checked too and are also never deleted, regardless of whether their
    parsed active/stale/unknown status looks recoverable. Any filesystem
    error while checking either mechanism — including a failure to release
    *this function's own* throwaway probe lock — becomes a categorized
    `ERROR`, never a traceback.
    """
    lock = ProfileLock(profile_path)
    try:
        await lock.acquire()
    except ProfileLockedError as exc:
        return DoctorCheck(DoctorCategory.PROFILE_LOCKED, CheckStatus.ERROR, str(exc)), False
    except OSError as exc:
        return (
            DoctorCheck(
                DoctorCategory.PROFILE_LOCKED,
                CheckStatus.ERROR,
                f"Could not verify the profile lock due to a filesystem error: {exc}",
            ),
            False,
        )

    try:
        singleton_locks = describe_chrome_singleton_locks(profile_path)
        singleton_error: Exception | None = None
    except OSError as exc:
        singleton_locks = []
        singleton_error = exc

    try:
        await lock.release()
    except OSError as exc:
        return (
            DoctorCheck(
                DoctorCategory.PROFILE_LOCKED,
                CheckStatus.ERROR,
                "Doctor's own preflight probe lock could not be released "
                f"({lock.lock_path}): {exc}. This lock file must be removed "
                "manually (after confirming no other process is using this "
                "profile) before a real browser session can start.",
            ),
            False,
        )

    if singleton_error is not None:
        return (
            DoctorCheck(
                DoctorCategory.PROFILE_LOCKED,
                CheckStatus.ERROR,
                "Could not check Chrome's own singleton lock files due to a "
                f"filesystem error: {singleton_error}",
            ),
            False,
        )

    if singleton_locks:
        descriptions = ", ".join(lock_info.describe() for lock_info in singleton_locks)
        return (
            DoctorCheck(
                DoctorCategory.PROFILE_LOCKED,
                CheckStatus.ERROR,
                f"Chrome profile-in-use markers present: {descriptions}. A "
                "Chrome process may already have this profile open (or "
                "crashed without cleaning up); these files are never "
                "removed automatically.",
            ),
            False,
        )

    return DoctorCheck(DoctorCategory.PROFILE_LOCKED, CheckStatus.OK, "Profile lock acquired"), True


def _extension_check(settings: Settings) -> DoctorCheck:
    try:
        install = find_installed_extension(settings.chrome_profile_path, settings.jobright_extension_id)
    except ExtensionNotFoundError as exc:
        return DoctorCheck(DoctorCategory.EXTENSION_MISSING, CheckStatus.ERROR, str(exc))
    except OSError as exc:
        return DoctorCheck(
            DoctorCategory.EXTENSION_MISSING,
            CheckStatus.ERROR,
            f"Could not read profile preferences due to a filesystem error: {exc}",
        )

    if not install.enabled:
        return DoctorCheck(
            DoctorCategory.EXTENSION_MISSING,
            CheckStatus.ERROR,
            f"Extension {install.name or install.extension_id} is installed but disabled "
            f"in {install.source_file}",
        )

    return DoctorCheck(
        DoctorCategory.EXTENSION_MISSING,
        CheckStatus.OK,
        f"Extension present: {install.name or install.extension_id} (enabled)",
    )


async def _service_worker_check(
    settings: Settings,
    factory: SessionFactory,
    timeout_ms: int,
) -> DoctorCheck:
    try:
        session = factory(settings)
    except Exception as exc:
        return DoctorCheck(
            DoctorCategory.SERVICE_WORKER_DEAD,
            CheckStatus.ERROR,
            f"Could not construct a browser session: {exc}",
        )

    try:
        try:
            context = await session.start()
        except Exception as exc:
            return DoctorCheck(
                DoctorCategory.SERVICE_WORKER_DEAD,
                CheckStatus.ERROR,
                f"Could not launch browser to verify service worker: {exc}",
            )

        try:
            worker = await find_service_worker(context, settings.jobright_extension_id, timeout_ms)
            await probe_service_worker(worker, settings.jobright_extension_id, timeout_ms)
        except (ServiceWorkerNotFoundError, ServiceWorkerUnresponsiveError) as exc:
            return DoctorCheck(DoctorCategory.SERVICE_WORKER_DEAD, CheckStatus.ERROR, str(exc))
        except Exception as exc:
            return DoctorCheck(
                DoctorCategory.SERVICE_WORKER_DEAD,
                CheckStatus.ERROR,
                f"Unexpected error verifying the service worker: {exc}",
            )
        else:
            return DoctorCheck(
                DoctorCategory.SERVICE_WORKER_DEAD, CheckStatus.OK, "Service worker responsive"
            )
    finally:
        # Attempted unconditionally (even after a failed start()) so a
        # session that only partially initialized never leaks resources;
        # teardown issues here must never mask the check result above.
        try:
            await session.close()
        except Exception:
            pass


def _xdotool_check() -> DoctorCheck:
    if shutil.which("xdotool") is None:
        return DoctorCheck(
            DoctorCategory.XDOTOOL_MISSING,
            CheckStatus.WARNING,
            "xdotool not found on PATH; toolbar-click trigger tier unavailable",
        )
    return DoctorCheck(DoctorCategory.XDOTOOL_MISSING, CheckStatus.OK, "xdotool available")


def _calibration_check(settings: Settings) -> DoctorCheck:
    if settings.toolbar_x <= 0 or settings.toolbar_y <= 0:
        return DoctorCheck(
            DoctorCategory.CALIBRATION_INVALID,
            CheckStatus.WARNING,
            "toolbar coordinates are the uncalibrated sentinel "
            f"({settings.toolbar_x}, {settings.toolbar_y}); the native toolbar-click "
            "trigger tier will refuse to run — set TOOLBAR_X/TOOLBAR_Y with "
            "scripts/calibrate_toolbar.py to enable it",
        )
    return DoctorCheck(
        DoctorCategory.CALIBRATION_INVALID,
        CheckStatus.OK,
        f"toolbar coordinates configured: ({settings.toolbar_x}, {settings.toolbar_y})",
    )


def _print_report(report: DoctorReport) -> None:
    for check in report.checks:
        print(f"[{check.status.value.upper():7}] {check.category.value}: {check.message}")


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run browser/profile preflight diagnostics")
    parser.parse_args(argv)
    resolved_settings = settings if settings is not None else Settings()
    report = asyncio.run(
        run_doctor(resolved_settings, session_factory=session_factory)
    )
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
