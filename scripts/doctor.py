"""Preflight diagnostics for the persistent browser profile and extension.

Distinguishes actionable failure categories (missing profile, locked profile,
missing extension, dead service worker, missing xdotool, invalid toolbar
calibration) without requiring a real browser: `session_factory` defaults to
the real `BrowserSession` but can be swapped for a fake in tests.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

from app.agent.browser_session import BrowserSession, ProfileLock
from app.agent.errors import ExtensionNotFoundError, ProfileLockedError, ServiceWorkerNotFoundError
from app.agent.extension import find_installed_extension, find_service_worker
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

    lock_available = False
    lock = ProfileLock(profile_path)
    try:
        await lock.acquire()
    except ProfileLockedError as exc:
        status = CheckStatus.WARNING if exc.stale else CheckStatus.ERROR
        checks.append(DoctorCheck(DoctorCategory.PROFILE_LOCKED, status, str(exc)))
    else:
        checks.append(
            DoctorCheck(DoctorCategory.PROFILE_LOCKED, CheckStatus.OK, "Profile lock acquired")
        )
        await lock.release()
        lock_available = True

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


def _extension_check(settings: Settings) -> DoctorCheck:
    try:
        install = find_installed_extension(settings.chrome_profile_path, settings.jobright_extension_id)
    except ExtensionNotFoundError as exc:
        return DoctorCheck(DoctorCategory.EXTENSION_MISSING, CheckStatus.ERROR, str(exc))
    return DoctorCheck(
        DoctorCategory.EXTENSION_MISSING,
        CheckStatus.OK,
        f"Extension present: {install.name or install.extension_id} "
        f"({'enabled' if install.enabled else 'disabled'})",
    )


async def _service_worker_check(
    settings: Settings,
    factory: SessionFactory,
    timeout_ms: int,
) -> DoctorCheck:
    session = factory(settings)
    try:
        context = await session.start()
    except Exception as exc:  # pragma: no cover - requires a real browser
        return DoctorCheck(
            DoctorCategory.SERVICE_WORKER_DEAD,
            CheckStatus.ERROR,
            f"Could not launch browser to verify service worker: {exc}",
        )

    try:
        await find_service_worker(context, settings.jobright_extension_id, timeout_ms)
    except ServiceWorkerNotFoundError as exc:
        return DoctorCheck(DoctorCategory.SERVICE_WORKER_DEAD, CheckStatus.ERROR, str(exc))
    else:
        return DoctorCheck(
            DoctorCategory.SERVICE_WORKER_DEAD, CheckStatus.OK, "Service worker responsive"
        )
    finally:
        await session.close()


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
            f"toolbar coordinates are not calibrated: ({settings.toolbar_x}, {settings.toolbar_y})",
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
