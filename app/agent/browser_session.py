"""Persistent headed browser session with an exclusive profile lock.

Playwright is imported lazily (inside function bodies) so that constructing
launch options, acquiring/releasing the profile lock, and running most of the
doctor's preflight checks never require Playwright or a browser binary to be
installed. Only an actual `BrowserSession.start()` call touches Playwright.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.agent.errors import (
    LockMetadata,
    ProfileInUseError,
    ProfileLockedError,
    ProfileMissingError,
)
from app.config import Settings

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright

LOCK_FILENAME = ".job-apply-lock.json"

#: Chrome's own singleton markers, written directly under the user-data-dir
#: root (never under `Default/`). Distinct from this project's own
#: `.job-apply-lock.json`; their presence means a real Chrome process already
#: has this profile open (or crashed without cleaning up). Never deleted here.
SINGLETON_LOCK_FILENAMES: tuple[str, ...] = (
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
)


def detect_chrome_singleton_locks(profile_path: Path) -> list[str]:
    """Return the names of Chrome singleton markers present at the profile root.

    `SingletonLock` is typically a symlink (sometimes dangling), so both
    `exists()` and `is_symlink()` are checked.
    """
    found: list[str] = []
    for name in SINGLETON_LOCK_FILENAMES:
        candidate = profile_path / name
        if candidate.exists() or candidate.is_symlink():
            found.append(name)
    return found


#: Playwright's Chromium defaults disable extensions and component extensions
#: with background pages; both must be removed for the persistent context to
#: keep the installed Jobright Autofill extension active.
IGNORED_DEFAULT_ARGS: tuple[str, ...] = (
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
)


def build_launch_options(settings: Settings) -> dict[str, object]:
    """Build headed persistent-context launch kwargs that keep extensions enabled."""
    return {
        "user_data_dir": str(settings.chrome_profile_path),
        "headless": False,
        "executable_path": str(settings.chrome_executable),
        "ignore_default_args": list(IGNORED_DEFAULT_ARGS),
        "args": [],
    }


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID recorded in a lock file."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user; treat as alive.
        return True
    except OSError:
        return False
    else:
        return True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProfileLock:
    """Atomic, exclusive lock over a Chrome profile directory.

    Uses `os.O_CREAT | os.O_EXCL` for atomic create-if-absent semantics so two
    processes racing to acquire the same profile cannot both succeed. Never
    automatically removes an existing lock file, even when it is detected as
    stale (owning process no longer alive): callers get actionable diagnostics
    via `ProfileLockedError` and must remove a stale lock explicitly.
    """

    def __init__(self, path: Path) -> None:
        self._profile_path = Path(path)
        self._lock_path = self._profile_path / LOCK_FILENAME
        self._held = False
        self._own_pid: int | None = None
        self._own_token: str | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    async def acquire(self) -> None:
        self._profile_path.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        token = uuid.uuid4().hex
        payload = {
            "pid": pid,
            "hostname": socket.gethostname(),
            "acquired_at": _utc_now_iso(),
            "token": token,
        }
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            metadata = self._read_metadata()
            stale = metadata is not None and not _pid_alive(metadata.pid)
            raise ProfileLockedError(self._lock_path, metadata, stale) from exc

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self._held = True
        self._own_pid = pid
        self._own_token = token

    async def release(self) -> None:
        if not self._held:
            return
        try:
            raw = self._lock_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            owned_by_us = (
                data.get("pid") == self._own_pid and data.get("token") == self._own_token
            )
        except (OSError, ValueError):
            owned_by_us = False

        if owned_by_us:
            try:
                self._lock_path.unlink()
            except FileNotFoundError:
                pass

        self._held = False
        self._own_pid = None
        self._own_token = None

    def _read_metadata(self) -> LockMetadata | None:
        try:
            raw = self._lock_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return LockMetadata(
                pid=int(data["pid"]),
                hostname=str(data["hostname"]),
                acquired_at=str(data["acquired_at"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def __aenter__(self) -> "ProfileLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


async def _start_real_playwright() -> Any:
    from playwright.async_api import async_playwright  # lazy import

    return await async_playwright().start()


PlaywrightFactory = Callable[[], Awaitable[Any]]
LockFactory = Callable[[Path], Any]


class BrowserSession:
    """Owns the single long-lived Playwright persistent browser context.

    `playwright_factory` defaults to a lazy real Playwright import so unit
    tests can inject a fake factory and never require Playwright or a browser
    binary to be installed. `lock_factory` defaults to the real `ProfileLock`
    and exists mainly so tests can inject a lock whose `release()` fails,
    proving teardown still completes every other step.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        playwright_factory: PlaywrightFactory | None = None,
        lock_factory: LockFactory | None = None,
    ) -> None:
        self._settings = settings
        self._playwright_factory = playwright_factory or _start_real_playwright
        self._lock_factory = lock_factory or ProfileLock
        self._lock: Any | None = None
        self._playwright: Any | None = None
        self._context: "BrowserContext | None" = None

    @property
    def context(self) -> "BrowserContext":
        if self._context is None:
            raise RuntimeError("BrowserSession has not been started")
        return self._context

    async def start(self) -> "BrowserContext":
        if self._context is not None:
            return self._context

        profile_path = self._settings.chrome_profile_path
        if not profile_path.exists():
            raise ProfileMissingError(profile_path)

        lock = self._lock_factory(profile_path)
        await lock.acquire()
        self._lock = lock

        try:
            singleton_files = detect_chrome_singleton_locks(profile_path)
            if singleton_files:
                raise ProfileInUseError(profile_path, singleton_files)

            playwright = await self._playwright_factory()
            self._playwright = playwright
            options = build_launch_options(self._settings)
            user_data_dir = options.pop("user_data_dir")
            self._context = await playwright.chromium.launch_persistent_context(
                user_data_dir, **options
            )
        except BaseException as start_exc:
            await self._teardown(primary_exc=start_exc)
            raise

        return self._context

    async def close(self) -> None:
        await self._teardown(primary_exc=None)

    async def _teardown(self, *, primary_exc: BaseException | None) -> None:
        """Best-effort teardown: always attempts context close, Playwright
        stop, and lock release, independently of one another, so a failure in
        one step never skips or masks another.

        When `primary_exc` is set (a `start()` failure), teardown errors are
        suppressed so the original failure is what propagates. When called
        from `close()` directly (`primary_exc is None`), the first teardown
        error encountered is re-raised after every step has been attempted.
        """
        captured: BaseException | None = None

        if self._context is not None:
            context = self._context
            self._context = None
            try:
                await context.close()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                captured = captured or exc

        if self._playwright is not None:
            playwright = self._playwright
            self._playwright = None
            stop = getattr(playwright, "stop", None)
            if stop is not None:
                try:
                    await stop()
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    captured = captured or exc

        if self._lock is not None:
            lock = self._lock
            self._lock = None
            try:
                await lock.release()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                captured = captured or exc

        if primary_exc is None and captured is not None:
            raise captured
