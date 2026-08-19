"""Typed exceptions for browser, profile, rate, and model safety."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from app.agent.form_scanner import FormDiff, FormSnapshot, SettleResult
    from app.agent.jobright_trigger import TierAttempt
    from app.storage.models import Board


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

    Carries the last unsettled `SettleResult` (snapshot, diff against the
    caller's baseline, and whether any change was observed at all) so a
    caller can still act on what did happen — a page that autofilled and
    then kept animating has genuinely changed fields even though it never
    went quiet.
    """

    def __init__(self, quiet_ms: int, timeout_ms: int, result: "SettleResult") -> None:
        self.quiet_ms = quiet_ms
        self.timeout_ms = timeout_ms
        self.result = result
        super().__init__(
            f"Form never stayed unchanged for {quiet_ms}ms within {timeout_ms}ms; "
            f"last snapshot had {len(result.snapshot.fields)} field(s) with "
            f"{len(result.diff.changed)} change(s) versus the baseline"
        )

    @property
    def snapshot(self) -> "FormSnapshot":
        return self.result.snapshot

    @property
    def diff(self) -> "FormDiff":
        return self.result.diff


class SnapshotScannerMismatch(BrowserError):
    """Raised when snapshots from two different scanners are compared.

    Value digests are keyed per `FormScanner` instance, so comparing across
    instances would report every field as changed — a silent false positive
    that would make an autofill trigger look successful when nothing
    happened. Comparing is refused instead.
    """

    def __init__(self, left: str, right: str) -> None:
        self.left = left
        self.right = right
        super().__init__(
            f"Snapshots come from different scanners ({left} vs {right}); their "
            "value digests are keyed per scanner and cannot be compared. Take the "
            "baseline and the result with the same FormScanner instance (e.g. via "
            "JobrightTrigger.baseline)."
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


class PageUnavailable(BrowserError):
    """Raised when the page an application was staged on is no longer held.

    A staged application lives in a specific tab: the form is filled, the
    approval gate is what stands between it and the submit button. The
    checkpoint survives a restart but the tab does not, so a run that finds
    no page must stop rather than "submit" against nothing.

    What stopping means depends on when it happens. Mid-staging it is a
    skip: nothing was submitted and the listing can be staged again. After a
    decision it is a failure, because someone approved a submission that is
    now not going to happen and should be told so.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            f"No staged page is still held for thread {thread_id!r}. The "
            "application was staged in a browser tab that is gone (most "
            "likely the worker restarted), so there is nothing left to submit; "
            "queue the listing again to stage it afresh."
        )


class FieldWriteRefused(BrowserError):
    """Base class for "the answer was not typed, and this is why".

    Every subclass names a control by its stable key and never carries the
    value: these messages reach logs and tracebacks, and the value is
    somebody's address, salary, or cover letter.

    A refusal is not a malfunction. The graph records the gap as unfilled,
    which puts `unwritten_answer` in the blocking reasons and therefore
    keeps the approval gate in play even under `AUTO_SUBMIT` — so the
    outcome of every refusal below is that a human looks at the form.
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"Refusing to type an answer into {key!r}: {reason}")


class UnsupportedFieldControl(FieldWriteRefused):
    """Raised for a control the applicant has to operate themselves.

    A file input cannot be satisfied by text at all. A password field would
    take a credential out of a plaintext answers file and put it into a
    page. A checkbox, a radio, and a multi-select are consents and choices,
    and a writer that "just ticked the box" would be agreeing to something
    on somebody's behalf.
    """

    def __init__(self, key: str, tag: str, field_type: str) -> None:
        self.tag = tag
        self.field_type = field_type
        super().__init__(
            key,
            f"a <{tag}> of type {field_type!r} is not something this writer will "
            "type into; it is left for the applicant",
        )


class FieldNotUniquelyResolved(FieldWriteRefused):
    """Raised when the page holds no such control, or more than one.

    "Type this answer into the control this key names" is only a meaningful
    instruction when exactly one control answers to that description. Two
    matches means guessing which of someone's answers goes where; none means
    the form on the page is not the form that was scanned.
    """

    def __init__(self, key: str, matches: int, detail: str = "") -> None:
        self.matches = matches
        found = (
            "no visible, enabled control matches the metadata it was scanned with"
            if matches == 0
            else f"{matches} visible controls match the metadata it was scanned with"
        )
        super().__init__(key, f"{found}{f' ({detail})' if detail else ''}")


class FieldProvenanceMismatch(FieldWriteRefused):
    """Raised when the resolved control is not the one the key was made from.

    Every audit row says "this answer went into this stable key". Resolving
    a control from metadata is a different question from "is this the
    control that key was derived from", so the key is re-derived from what
    was actually found and a mismatch stops the write.
    """

    def __init__(self, key: str, resolved_key: str) -> None:
        self.resolved_key = resolved_key
        super().__init__(
            key,
            "the control found on the page derives the stable key "
            f"{resolved_key!r}, so typing into it would record an answer "
            "against a control it did not go into",
        )


class FieldWriteNotVerified(FieldWriteRefused):
    """Raised when the control did not hold the value afterwards.

    The comparison happens in the page and only a boolean comes back, so
    neither the intended answer nor whatever the control actually holds
    crosses the wire a second time. Nothing is retried: a second attempt
    would be a second set of input events on a control that already refused
    one.
    """

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(
            key,
            "the value was dispatched but the control did not hold it afterwards"
            + (f" ({detail})" if detail else "")
            + "; nothing is retried",
        )


class SubmitRefused(BrowserError):
    """Base class for "no submission was attempted, and this is why".

    Distinct from a `SubmitOutcome` reporting `submitted=False`, which means
    a final-submit control *was* clicked and no success signal followed.
    That distinction is the whole point of the type: one says nothing
    happened, the other says something happened and cannot be confirmed.
    """


class SubmitNotAuthorized(SubmitRefused):
    """Raised when a submission was asked for without a decision behind it.

    The graph only routes to its submit node after an approval, or under
    `AUTO_SUBMIT` for a form with nothing blocking it. This is the same
    condition restated as a precondition the submitter enforces itself, so
    that a retry path, a debugging script, or a new graph edge cannot submit
    an application nobody released.
    """

    def __init__(self, application_id: int, detail: str) -> None:
        self.application_id = application_id
        super().__init__(
            f"Refusing to submit application {application_id}: {detail}. Nothing "
            "was clicked."
        )


class SubmitAlreadyAttempted(SubmitRefused):
    """Raised when this application's final control has already been pressed.

    The case is a worker killed between the press and the outcome. Its
    thread is left looking exactly like one whose press never happened: an
    approval on file, no outcome recorded, an interrupt still in the
    checkpoint. Replaying the node is the right recovery for every other
    node in the graph and the wrong one here, because the page has already
    had the application.

    So the outcome is a failure rather than a second attempt. That is a
    person checking one ATS by hand — the honest cost of not knowing —
    instead of an applicant explaining a duplicate they did not send.
    """

    def __init__(self, application_id: int, owner: str, attempted_at: datetime) -> None:
        self.application_id = application_id
        self.owner = owner
        self.attempted_at = attempted_at
        super().__init__(
            f"The final submit control for application {application_id} was already "
            f"pressed by {owner!r} at {attempted_at.isoformat()}, and no outcome was "
            "recorded — most likely that worker was killed mid-submit. It is not "
            "pressed again: check this application in the ATS by hand."
        )


class FinalSubmitControlNotFound(SubmitRefused):
    """Raised when nothing on the page is recognisably the last click.

    Carries what was rejected and why, because the usual cause is a form
    whose final control is worded in a way the accessible-name rules do not
    accept — and an operator can only tell that from the list.
    """

    def __init__(self, rejected: Sequence[tuple[str, str]]) -> None:
        self.rejected = tuple(rejected)
        listed = "; ".join(f"{name!r}: {reason}" for name, reason in self.rejected)
        super().__init__(
            "No visible control on this page is recognisably a final submit. "
            + (f"Rejected: {listed}. " if listed else "")
            + "Nothing was clicked; the form is left filled for the applicant."
        )


class FinalSubmitControlAmbiguous(SubmitRefused):
    """Raised when several controls could each be the last click.

    Clicking one of several would be a guess about which form is being
    submitted, on a page that is about to send somebody's application
    somewhere.
    """

    def __init__(self, accepted: Sequence[str]) -> None:
        self.accepted = tuple(accepted)
        listed = ", ".join(repr(name) for name in self.accepted)
        super().__init__(
            f"{len(self.accepted)} visible controls each look like a final submit "
            f"({listed}), so which one submits this application is a guess. "
            "Nothing was clicked."
        )


class SafetyError(Exception):
    """Base class for refusals that protect the account or the applicant.

    Deliberately *not* a `BrowserError`: these are policy outcomes, not
    browser malfunctions, and a caller that swallows browser errors must not
    accidentally swallow a rate cap or a protected-question refusal too.
    """


class RateLimitExceeded(SafetyError):
    """Raised when a board's per-UTC-day cap has already been reached.

    There is no bypass: the only ways forward are to wait for
    `next_reset_at` or to raise the configured cap in the environment, both
    of which are deliberate operator actions.
    """

    def __init__(
        self,
        board: "Board",
        cap: int,
        count: int,
        next_reset_at: datetime,
    ) -> None:
        self.board = board
        self.cap = cap
        self.count = count
        self.next_reset_at = next_reset_at
        super().__init__(
            f"Daily cap reached for {board.value}: {count} of {cap} action(s) already "
            f"recorded for this UTC day. The count resets at "
            f"{next_reset_at.isoformat()}; there is no runtime override, so either "
            "wait for the reset or raise the configured cap."
        )


class StagingArtefactsLost(BrowserError):
    """Raised when a continuation cannot see what the crashed attempt staged.

    The baseline snapshot and the trigger result are held in memory for the
    length of one invocation, deliberately: they describe a live page and
    would be meaningless in a checkpoint. A worker that picks up a thread
    another process was part-way through therefore has the checkpoint but
    none of the artefacts, and the attribution and gap-filling steps have
    nothing to work from.

    Distinct from `PageUnavailable`, which is about the tab. Neither is a
    malfunction: nothing was submitted, so the listing can simply be staged
    again from the start.
    """

    def __init__(self, thread_id: str, needed: str) -> None:
        self.thread_id = thread_id
        self.needed = needed
        super().__init__(
            f"Thread {thread_id!r} resumed without the {needed} its previous "
            "attempt held in memory, so staging cannot continue where it left "
            "off. Nothing was submitted; queue the listing again to stage it "
            "afresh."
        )


class UnknownAtsLayout(SafetyError):
    """Raised when the page an Apply click landed on is not a known ATS.

    Every filling, attribution, and submission decision this project makes
    assumes a recognised applicant tracking system. On an unrecognised page
    the same code would be typing into and clicking controls it has never
    been calibrated against, in someone's name, so the application is
    abandoned instead.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(
            f"No supported applicant tracking system was recognised at {url}. "
            "Refusing to fill or submit a form whose layout has never been "
            "verified."
        )


class CaptchaEncountered(SafetyError):
    """Raised when a page presents a human-verification challenge.

    Solving one automatically is exactly the behaviour the challenge exists
    to stop, so the application is abandoned and left for the applicant.
    """

    def __init__(self, marker: str) -> None:
        self.marker = marker
        super().__init__(
            f"A human-verification challenge is present ({marker}). This "
            "application is abandoned rather than answered automatically."
        )


class LoginWallEncountered(SafetyError):
    """Raised when a page asks for credentials before showing the form.

    Nothing here types a password into a page: credentials belong to the
    applicant and to the browser profile they already signed in with.
    """

    def __init__(self, marker: str) -> None:
        self.marker = marker
        super().__init__(
            f"The page is asking for a sign-in before the application form "
            f"({marker}). Sign in yourself in the dedicated profile and queue "
            "the listing again; no credentials are ever entered from here."
        )


class ProtectedQuestionError(SafetyError):
    """Raised when a protected question was about to be sent to a model.

    Visa/sponsorship, EEO/demographic, compensation, and employment-date
    answers are the applicant's to give. A model asked for one would produce
    a plausible sentence with no basis in fact, which is exactly the failure
    this project exists to prevent, so the request is refused at the boundary
    that would otherwise perform it.
    """

    def __init__(self, category: str, question: str) -> None:
        self.category = category
        self.question = question
        super().__init__(
            f"Refusing to ask a model a {category} question: {question!r}. "
            "Answers in this category must come from the applicant, either "
            "through the canonical answers file or through the approval gate."
        )


class ModelError(Exception):
    """Base class for model-routing failures."""


class ModelUnavailable(ModelError):
    """Raised when a completion cannot even be attempted.

    A missing API key, a missing `httpx`, or an unusable endpoint all mean no
    request was made; the caller escalates the question to a human rather
    than treating the gap as answered.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Model completion unavailable: {reason}")


class ModelResponseError(ModelError):
    """Raised when the provider replied with something unusable.

    Carries a redacted excerpt only: the request's credentials are never
    echoed back into a message, a log, or a traceback.
    """

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Model request failed with status {status}: {detail}")


class AnswerBookError(Exception):
    """Raised when the canonical answers file cannot be trusted.

    A malformed answers file is never partially applied: a typo that silently
    dropped one entry would send a question a human already answered to a
    model instead.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Canonical answers file {path} is unusable: {reason}")
