"""Persistent headed browser session with an exclusive profile lock.

Playwright is imported lazily (inside function bodies) so that constructing
launch options, acquiring/releasing the profile lock, and running most of the
doctor's preflight checks never require Playwright or a browser binary to be
installed. Only an actual `BrowserSession.start()` call touches Playwright.
"""

from __future__ import annotations

import asyncio
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
    SingletonLockInfo,
    TeardownError,
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


def describe_chrome_singleton_locks(profile_path: Path) -> list[SingletonLockInfo]:
    """Return rich diagnostics for every Chrome singleton marker present at
    the profile root: which files exist, and (for `SingletonLock`, which
    Chrome creates as a symlink encoding `"<hostname>-<pid>"`) whether the
    owning pid looks active, stale, or could not be determined.

    `SingletonLock` is typically a symlink (sometimes dangling), so both
    `exists()` and `is_symlink()` are checked for presence. This is purely
    diagnostic: presence alone (regardless of status) is always treated as a
    profile-in-use condition by callers, and no marker is ever deleted here.
    """
    infos: list[SingletonLockInfo] = []
    for name in SINGLETON_LOCK_FILENAMES:
        candidate = profile_path / name
        if candidate.exists() or candidate.is_symlink():
            infos.append(_describe_singleton_marker(candidate, name))
    return infos


def _describe_singleton_marker(candidate: Path, name: str) -> SingletonLockInfo:
    if not candidate.is_symlink():
        return SingletonLockInfo(filename=name, status="unknown")
    try:
        target = os.readlink(candidate)
    except OSError:
        return SingletonLockInfo(filename=name, status="unknown")

    hostname, sep, pid_text = target.rpartition("-")
    if not sep or not hostname or "/" in hostname or not pid_text.isdigit():
        return SingletonLockInfo(filename=name, status="unknown")

    pid = int(pid_text)
    status = "active" if _pid_alive(pid) else "stale"
    return SingletonLockInfo(filename=name, status=status, hostname=hostname, pid=pid)


def detect_chrome_singleton_locks(profile_path: Path) -> list[str]:
    """Backward-compatible presence check: just the filenames of whichever
    singleton markers exist, with no status parsing. See
    `describe_chrome_singleton_locks` for hostname/pid/active-stale-unknown
    diagnostics.
    """
    return [info.filename for info in describe_chrome_singleton_locks(profile_path)]


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

    `start()` and `close()` share one internal `asyncio.Lock` so concurrent
    calls on the same instance are serialized rather than racing: concurrent
    `start()` calls become idempotent (only the first actually launches;
    later ones simply wait and return the same context) instead of the
    second colliding with the first's own profile lock, and a `close()`
    issued while a `start()` is still in flight waits for that `start()` to
    finish (success or failure) before tearing down whatever resulted.
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
        self._guard = asyncio.Lock()

    @property
    def context(self) -> "BrowserContext":
        if self._context is None:
            raise RuntimeError("BrowserSession has not been started")
        return self._context

    async def start(self) -> "BrowserContext":
        async with self._guard:
            return await self._start_locked()

    async def _start_locked(self) -> "BrowserContext":
        if self._context is not None:
            return self._context

        profile_path = self._settings.chrome_profile_path
        if not profile_path.exists():
            raise ProfileMissingError(profile_path)

        lock = self._lock_factory(profile_path)
        await lock.acquire()
        self._lock = lock

        try:
            singleton_locks = describe_chrome_singleton_locks(profile_path)
            if singleton_locks:
                raise ProfileInUseError(profile_path, singleton_locks)

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
        async with self._guard:
            await self._teardown(primary_exc=None)

    async def _teardown(self, *, primary_exc: BaseException | None) -> None:
        """Best-effort teardown: always attempts context close, Playwright
        stop, and lock release, independently of one another, so a failure in
        one step never skips or masks another.

        Every failure is collected (not just the first). When `primary_exc`
        is set (a `start()` failure), each teardown failure is attached to it
        as a note (via `BaseException.add_note`) rather than raised, so the
        original launch failure is still what propagates — but the details
        are not lost. When called from `close()` directly (`primary_exc is
        None`): zero failures raise nothing, exactly one failure raises that
        failure's own exception directly (so narrow `except SomeError`
        callers keep working), and two-or-more failures raise a
        `TeardownError` aggregate that calls out a lock-release failure
        first, since a leaked lock is the most safety-critical outcome.

        `context` and `playwright` are always cleared before their teardown
        action is attempted, whether or not it succeeds, since retrying
        either after a failure is not generally safe. The profile lock is
        different: it is only cleared on a *successful* `release()`, so a
        failed release leaves the lock object retained on `self._lock` and a
        later `close()` call will retry releasing the very same lock.
        """
        failures: list[tuple[str, BaseException]] = []

        if self._context is not None:
            context = self._context
            self._context = None
            try:
                await context.close()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                failures.append(("context_close", exc))

        if self._playwright is not None:
            playwright = self._playwright
            self._playwright = None
            stop = getattr(playwright, "stop", None)
            if stop is not None:
                try:
                    await stop()
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    failures.append(("playwright_stop", exc))

        if self._lock is not None:
            try:
                await self._lock.release()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                failures.append(("lock_release", exc))
                # Retained (not cleared) so a later close() can retry.
            else:
                self._lock = None

        if not failures:
            return

        if primary_exc is not None:
            for step, failure in failures:
                primary_exc.add_note(
                    f"Cleanup issue during teardown after this failure ({step}): {failure}"
                )
            return

        if len(failures) == 1:
            raise failures[0][1]

        raise TeardownError(failures)
