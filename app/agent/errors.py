"""Typed exceptions for browser/profile safety."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from app.agent.form_scanner import FormDiff, FormSnapshot
    from app.agent.jobright_trigger import TierAttempt


@dataclass(frozen=True)
class LockMetadata:
    """Diagnostic metadata read from an existing profile lock file."""

    pid: int
    hostname: str
    acquired_at: str


@dataclass(frozen=True)
class SingletonLockInfo:
    """Diagnostic info about one Chrome-internal singleton marker file.

    `SingletonLock` is a symlink whose target Chrome encodes as
    `"<hostname>-<pid>"`; when that can be parsed, `status` reports whether
    the owning pid still appears alive ("active"), looks gone ("stale"), or
    could not be determined ("unknown" — e.g. `SingletonSocket` and
    `SingletonCookie` are plain files with no such encoding, or the symlink
    target didn't match the expected shape). `status` is purely diagnostic:
    every marker is always treated as a profile-in-use condition regardless
    of status, and none are ever deleted automatically.
    """

    filename: str
    status: str  # "active" | "stale" | "unknown"
    hostname: str | None = None
    pid: int | None = None

    def describe(self) -> str:
        if self.status in ("active", "stale") and self.pid is not None:
            liveness = "no longer running" if self.status == "stale" else "running"
            return (
                f"{self.filename} ({self.status}: pid {self.pid} on host "
                f"{self.hostname}, {liveness})"
            )
        return f"{self.filename} (status unknown)"


class BrowserError(Exception):
    """Base class for browser/profile safety errors."""


class ProfileMissingError(BrowserError):
    """Raised when the configured Chrome profile directory does not exist."""

    def __init__(self, profile_path: Path) -> None:
        self.profile_path = profile_path
        super().__init__(f"Chrome profile directory does not exist: {profile_path}")


class ProfileLockedError(BrowserError):
    """Raised when a profile lock is already held.

    Carries actionable diagnostics (owning pid/host/time and whether the owning
    process still appears to be alive) but never triggers automatic removal of
    the existing lock file: an operator must confirm the owner is dead and
    remove it explicitly.
    """

    def __init__(self, lock_path: Path, metadata: LockMetadata | None, stale: bool) -> None:
        self.lock_path = lock_path
        self.metadata = metadata
        self.stale = stale
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        if self.metadata is None:
            return (
                f"Profile lock file exists at {self.lock_path} but its metadata "
                "could not be read. Refusing to remove it automatically; "
                "verify manually that no other process is using this profile "
                "before deleting the lock file."
            )
        status = "stale (owning process appears to be gone)" if self.stale else "active"
        action = (
            "It appears safe to remove manually, but this tool will not delete "
            "it automatically."
            if self.stale
            else "Wait for that process to exit, or stop it, before retrying."
        )
        return (
            f"Profile lock at {self.lock_path} is held by pid {self.metadata.pid} "
            f"on host {self.metadata.hostname} (acquired at {self.metadata.acquired_at}); "
            f"status: {status}. {action}"
        )


class ProfileInUseError(BrowserError):
    """Raised when Chrome's own singleton markers indicate a Chrome process
    already has this profile open, independent of this project's own
    `.job-apply-lock.json`.

    These files (`SingletonLock`, `SingletonSocket`, `SingletonCookie`) are
    Chrome-internal and are never deleted by this project; an operator must
    close the other Chrome process (or confirm it is gone and remove the
    files manually) before retrying.
    """

    def __init__(self, profile_path: Path, locks: list[SingletonLockInfo]) -> None:
        self.profile_path = profile_path
        self.locks = list(locks)
        self.singleton_files = [lock.filename for lock in self.locks]
        descriptions = ", ".join(lock.describe() for lock in self.locks)
        super().__init__(
            f"Chrome profile-in-use markers found in {profile_path}: {descriptions}. "
            "A Chrome process may already have this profile open (or crashed "
            "without cleaning up). Close that Chrome process first; these "
            "files are never removed automatically."
        )


class ExtensionNotFoundError(BrowserError):
    """Raised when the extension cannot be verified as installed via Chrome
    profile preference files."""

    def __init__(self, profile_path: Path, extension_id: str, reason: str) -> None:
        self.profile_path = profile_path
        self.extension_id = extension_id
        self.reason = reason
        super().__init__(
            f"Extension {extension_id} not found in profile {profile_path}: {reason}"
        )


class ServiceWorkerNotFoundError(BrowserError):
    """Raised when no matching extension service worker was ever discovered
    within the timeout (including any wake attempt). Distinct from
    `ServiceWorkerUnresponsiveError`, which means a worker WAS discovered but
    failed a liveness probe."""

    def __init__(self, extension_id: str, timeout_ms: int) -> None:
        self.extension_id = extension_id
        self.timeout_ms = timeout_ms
        super().__init__(
            f"No service worker for extension {extension_id} discovered within "
            f"{timeout_ms}ms"
        )


class ServiceWorkerUnresponsiveError(BrowserError):
    """Raised when a service worker WAS discovered but failed to respond to
    a liveness probe (e.g. `evaluate()` raised, timed out, or the worker
    handle had no usable `evaluate` at all).

    Kept distinct from `ServiceWorkerNotFoundError` so a discovered-but-dead
    worker is never described with "not found"/"not discovered" wording,
    which would misleadingly suggest discovery itself failed.
    """

    def __init__(self, extension_id: str, timeout_ms: int, reason: str) -> None:
        self.extension_id = extension_id
        self.timeout_ms = timeout_ms
        self.reason = reason
        super().__init__(
            f"Service worker for extension {extension_id} was discovered but "
            f"did not respond within {timeout_ms}ms ({reason})"
        )


class FormSettleTimeout(BrowserError):
    """Raised when a page never reaches mutation/value quiescence in time.

    Carries the last snapshot and the last diff against the caller's
    baseline so a caller (or an operator reading logs) can still see how far
    the page got, instead of only learning that it never stopped changing.
    """

    def __init__(
        self,
        quiet_ms: int,
        timeout_ms: int,
        snapshot: "FormSnapshot",
        diff: "FormDiff",
    ) -> None:
        self.quiet_ms = quiet_ms
        self.timeout_ms = timeout_ms
        self.snapshot = snapshot
        self.diff = diff
        super().__init__(
            f"Form never stayed unchanged for {quiet_ms}ms within {timeout_ms}ms; "
            f"last snapshot had {len(snapshot.fields)} field(s) with "
            f"{len(diff.changed)} change(s) versus the baseline"
        )


class NativeClickError(BrowserError):
    """Base class for native (OS-level) toolbar click failures."""


class NativeClickUnavailable(NativeClickError):
    """Raised when a native toolbar click cannot even be attempted.

    Distinct from `NativeClickFailed`: this means a precondition is missing
    (no `xdotool`, no X display, uncalibrated coordinates), so nothing was
    executed and no pointer was moved.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Native toolbar click unavailable: {reason}")


class NativeClickFailed(NativeClickError):
    """Raised when a native toolbar command ran but did not succeed."""

    def __init__(self, command: Sequence[str], reason: str) -> None:
        self.command = tuple(command)
        self.reason = reason
        super().__init__(f"Native toolbar click failed ({' '.join(self.command)}): {reason}")


class TriggerFailed(BrowserError):
    """Raised when every Jobright Autofill trigger tier failed.

    Retains one diagnostic per attempted tier, in attempt order, so an
    operator can see which tier got how far rather than only that the last
    one failed.
    """

    def __init__(self, attempts: Sequence["TierAttempt"]) -> None:
        self.attempts = tuple(attempts)
        details = "; ".join(f"{attempt.tier.value}: {attempt.detail}" for attempt in self.attempts)
        super().__init__(
            f"All {len(self.attempts)} Jobright Autofill trigger tier(s) failed. {details}"
            if self.attempts
            else "No Jobright Autofill trigger tiers were configured"
        )


class TeardownError(BrowserError):
    """Raised by `BrowserSession.close()` when *more than one* teardown step
    (context close, Playwright stop, profile lock release) fails.

    A single-step failure is raised directly as its own exception type so
    narrow `except SomeSpecificError` callers keep working; this aggregate is
    only used once there is genuinely more than one failure to report, since
    picking just one would otherwise silently mask the others. The profile
    lock failure (if any) is always called out first/most prominently since a
    leaked lock is the most safety-critical outcome — it blocks every future
    `start()` until an operator intervenes.
    """

    def __init__(self, failures: list[tuple[str, BaseException]]) -> None:
        self.failures = list(failures)
        lock_failures = [(step, exc) for step, exc in self.failures if step == "lock_release"]
        other_failures = [(step, exc) for step, exc in self.failures if step != "lock_release"]

        parts: list[str] = []
        if lock_failures:
            _, lock_exc = lock_failures[0]
            parts.append(
                "profile lock release failed and the lock is retained on this "
                f"session for a retry on the next close() call: {lock_exc}"
            )
        for step, exc in other_failures:
            parts.append(f"{step} failed: {exc}")

        super().__init__(
            "Browser session teardown encountered multiple errors: " + "; ".join(parts)
        )
