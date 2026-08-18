"""Typed exceptions for browser/profile safety."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LockMetadata:
    """Diagnostic metadata read from an existing profile lock file."""

    pid: int
    hostname: str
    acquired_at: str


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
    """Raised when no matching extension service worker was discovered."""

    def __init__(self, extension_id: str, timeout_ms: int) -> None:
        self.extension_id = extension_id
        self.timeout_ms = timeout_ms
        super().__init__(
            f"No service worker for extension {extension_id} discovered within "
            f"{timeout_ms}ms"
        )
