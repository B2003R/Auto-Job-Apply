"""The human approval gate: one typed decision, two front ends.

Nothing in this project submits a real application without a decision that
passed through here. That makes the gate an audit boundary as much as a
control-flow one, so the rules it enforces are deliberately stricter than
"the caller said approve":

* **A decision names an application, never a thread.** `ApprovalRequest`
  has no thread field at all, and every gate derives the graph thread from
  the stored `applications` row. An attacker holding one valid application
  id therefore cannot aim a decision at somebody else's staged
  application: there is no parameter to aim it with, and
  `ApprovalService.decide` re-checks the pairing anyway when a caller
  (`resume_application`) already knows which thread it is resuming.
* **Replaying the identical decision is a no-op; anything else is a
  conflict.** A double-clicked CLI prompt or a retried HTTP request must
  not fail, and must not rewrite the record either — so an exact repeat
  returns the *first* record, with its original timestamp, while a
  different decision, a different actor, or a different note is refused
  with `ApprovalConflict`. Storage enforces this too
  (`Database.try_record_approval` is insert-only), so a future caller that
  skipped this service still could not erase who approved a submission.
* **Validation happens before anything is written.** An unknown
  application, an application that is not waiting for a decision, and a
  thread mismatch all raise with nothing persisted, so a refused decision
  leaves no trace suggesting one was made.
* **The graph resumes with what was stored, not with what was asked for.**
  `ApprovalOutcome.resume_payload()` is built from the persisted
  `ApprovalRecord`, so the decision the graph acts on and the decision in
  the audit table cannot diverge.

`CliApprovalGate` and `ApiApprovalGate` differ only in how they obtain a
`ApprovalRequest` — a console prompt versus a request body. Both then take
exactly the same path, so a rule proven for one holds for the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from app.storage.db import Database
from app.storage.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApprovalDecision,
    ApprovalRecord,
    QueueItem,
)

#: Bounds on the free-text fields a decision carries. Both are audit data
#: written by a human, not a document store: something far past these
#: lengths is a mistake or an attempt to bloat the table, and truncating it
#: silently would falsify the record.
MAX_ACTOR_CHARS = 320
MAX_NOTE_CHARS = 2_000

#: Words each front end accepts for a decision. Deliberately narrow: "y",
#: "ok", and "1" are the kinds of input a tired operator types at the wrong
#: prompt, and reading any of them as "submit this application" is exactly
#: the mistake this gate exists to prevent.
_DECISION_WORDS: Mapping[str, ApprovalDecision] = {
    "approve": ApprovalDecision.APPROVED,
    "approved": ApprovalDecision.APPROVED,
    "reject": ApprovalDecision.REJECTED,
    "rejected": ApprovalDecision.REJECTED,
}

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalError(Exception):
    """Base class for every refusal to accept or apply a decision."""


class InvalidApprovalRequest(ApprovalError):
    """Raised when a decision is malformed before it is even looked up.

    Covers an unparseable decision word, a missing actor, an out-of-range
    identifier, and an over-long actor or note. Nothing is read from
    storage and nothing is written.
    """


class UnknownApplicationError(ApprovalError):
    """Raised when no application has the given id."""

    def __init__(self, application_id: int) -> None:
        self.application_id = application_id
        super().__init__(f"No application with id {application_id}")


class UnknownThreadError(ApprovalError):
    """Raised when no application owns the given graph thread."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"No application owns graph thread {thread_id!r}")


class ApplicationThreadMismatch(ApprovalError):
    """Raised when a decision's application belongs to a different thread.

    The only way to reach this is for a caller to name both an application
    and a thread that do not go together — which is what a forged
    application id looks like from the resume side. Nothing is recorded and
    nothing is resumed.
    """

    def __init__(self, application_id: int, expected: str, actual: str) -> None:
        self.application_id = application_id
        self.expected_thread_id = expected
        self.actual_thread_id = actual
        super().__init__(
            f"Application {application_id} belongs to thread {actual!r}, not "
            f"{expected!r}; refusing to apply its decision to another thread"
        )


class ApplicationNotAwaitingApproval(ApprovalError):
    """Raised when an application is not at the approval gate.

    A decision for an application that is still staging has nothing to
    release, and one for an application already submitted, rejected, or
    failed would be recorded against an outcome that has already happened.
    """

    def __init__(self, application_id: int, status: ApplicationStatus) -> None:
        self.application_id = application_id
        self.status = status
        super().__init__(
            f"Application {application_id} is {status.value}, not "
            f"{ApplicationStatus.AWAITING_APPROVAL.value}; there is no pending "
            "approval to decide"
        )


class ApprovalConflict(ApprovalError):
    """Raised when a decision differs from the one already recorded.

    Never carries the stored note: a conflict message travels into logs and
    HTTP responses, and the note is the reviewer's own words about someone's
    job application.
    """

    def __init__(self, application_id: int, stored: ApprovalRecord, field: str) -> None:
        self.application_id = application_id
        self.stored = stored
        self.field = field
        super().__init__(
            f"Application {application_id} was already {stored.decision.value} by "
            f"{stored.actor} at {stored.timestamp.isoformat()}; this request "
            f"differs by {field} and was refused rather than overwriting the "
            "recorded decision"
        )


def parse_decision(value: Any) -> ApprovalDecision:
    """Turn one front end's raw input into the shared typed decision.

    Both the CLI prompt and the API body go through this, so neither can
    develop its own idea of what counts as approval.
    """
    if isinstance(value, ApprovalDecision):
        return value
    if not isinstance(value, str) or isinstance(value, bool):
        raise InvalidApprovalRequest(
            f"a decision must be one of {sorted(_DECISION_WORDS)}, not "
            f"{type(value).__name__}"
        )
    word = value.strip().casefold()
    try:
        return _DECISION_WORDS[word]
    except KeyError:
        raise InvalidApprovalRequest(
            f"{value!r} is not a decision; say one of {sorted(_DECISION_WORDS)}"
        ) from None


def _require_actor(actor: Any) -> str:
    if not isinstance(actor, str):
        raise InvalidApprovalRequest(
            f"an actor must be a string, not {type(actor).__name__}"
        )
    trimmed = actor.strip()
    if not trimmed:
        raise InvalidApprovalRequest(
            "an actor is required: an approval records who authorised a "
            "submission made in their name"
        )
    if len(trimmed) > MAX_ACTOR_CHARS:
        raise InvalidApprovalRequest(
            f"an actor may be at most {MAX_ACTOR_CHARS} characters; got {len(trimmed)}"
        )
    return trimmed


def _clean_note(note: Any) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise InvalidApprovalRequest(
            f"a note must be a string, not {type(note).__name__}"
        )
    trimmed = note.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_NOTE_CHARS:
        raise InvalidApprovalRequest(
            f"a note may be at most {MAX_NOTE_CHARS} characters; got {len(trimmed)}. "
            "It is refused rather than truncated, since a shortened note is a "
            "falsified record of what the reviewer said."
        )
    return trimmed


@dataclass(frozen=True)
class ApprovalRequest:
    """One human decision, validated at construction.

    There is deliberately no thread field. A gate resolves the thread from
    the application record, so a request cannot carry a thread that
    disagrees with the application it names.
    """

    application_id: int
    decision: ApprovalDecision
    actor: str
    note: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.application_id, bool) or not isinstance(
            self.application_id, int
        ):
            raise InvalidApprovalRequest(
                f"an application id must be an int, not "
                f"{type(self.application_id).__name__}"
            )
        if self.application_id <= 0:
            raise InvalidApprovalRequest(
                f"an application id must be positive; got {self.application_id}"
            )
        if not isinstance(self.decision, ApprovalDecision):
            raise InvalidApprovalRequest(
                "a decision must be an ApprovalDecision; parse raw input with "
                "parse_decision() so the CLI and the API agree on what the "
                "words mean"
            )
        object.__setattr__(self, "actor", _require_actor(self.actor))
        object.__setattr__(self, "note", _clean_note(self.note))

    @classmethod
    def from_payload(
        cls, application_id: int, payload: Mapping[str, Any]
    ) -> "ApprovalRequest":
        """Build a request from an untrusted mapping (an HTTP body, say).

        `application_id` comes from the route, not the body, and any
        `thread_id` in the payload is ignored: a body that could name a
        thread would let one valid application id release a different
        application.
        """
        if not isinstance(payload, Mapping):
            raise InvalidApprovalRequest(
                f"a decision payload must be a mapping, not {type(payload).__name__}"
            )
        if "decision" not in payload:
            raise InvalidApprovalRequest("a decision payload must carry 'decision'")
        if "actor" not in payload:
            raise InvalidApprovalRequest("a decision payload must carry 'actor'")
        return cls(
            application_id=application_id,
            decision=parse_decision(payload["decision"]),
            actor=payload["actor"],
            note=payload.get("note"),
        )

    def __repr__(self) -> str:
        """Never renders the note; a repr reaches logs and tracebacks."""
        has_note = "<note>" if self.note is not None else None
        return (
            f"ApprovalRequest(application_id={self.application_id!r}, "
            f"decision={self.decision.value!r}, actor={self.actor!r}, "
            f"note={has_note!r})"
        )

    __str__ = __repr__

    def matches(self, record: ApprovalRecord) -> bool:
        """Whether `record` is this exact decision, already stored.

        Actor and note are part of the comparison, not just the verdict: a
        second person approving, or the same person rewriting their
        reasoning, is a new statement about the application and must not be
        silently absorbed as a replay of the first one.
        """
        return (
            record.application_id == self.application_id
            and record.decision is self.decision
            and record.actor == self.actor
            and record.note == self.note
        )

    def differing_field(self, record: ApprovalRecord) -> str:
        """Which part of `record` this request disagrees with."""
        if record.decision is not self.decision:
            return "decision"
        if record.actor != self.actor:
            return "actor"
        if record.note != self.note:
            return "note"
        return "application"


@dataclass(frozen=True)
class ApprovalOutcome:
    """A decision that is now persisted, and the application it belongs to.

    `replayed` distinguishes "this call recorded it" from "it was already
    recorded identically", which is what lets a retried request succeed
    without the graph being resumed a second time.
    """

    application: ApplicationRecord
    record: ApprovalRecord
    replayed: bool

    @property
    def application_id(self) -> int:
        return self.application.id

    @property
    def thread_id(self) -> str:
        return self.application.thread_id

    @property
    def decision(self) -> ApprovalDecision:
        return self.record.decision

    @property
    def approved(self) -> bool:
        return self.record.decision is ApprovalDecision.APPROVED

    @property
    def actor(self) -> str:
        return self.record.actor

    @property
    def note(self) -> str | None:
        return self.record.note

    @property
    def timestamp(self) -> datetime:
        return self.record.timestamp

    def resume_payload(self) -> dict[str, Any]:
        """What the graph is resumed with: the *stored* decision, in plain types.

        Built from the persisted record rather than the request, so the
        decision the graph acts on is by construction the decision in the
        audit table. Only strings, ints, and `None` appear, so any
        checkpointer can round-trip it.

        Carries the decision and its timestamp, not the reviewer. A resume
        payload is written into the checkpoint file, and a checkpoint file
        is a working artefact that gets copied around with a working
        directory; the approvals table is the access-controlled record of
        who authorised a submission made in their name, and it stays the
        only place that answers that question.
        """
        return {
            "application_id": self.record.application_id,
            "decision": self.record.decision.value,
            "decided_at": self.record.timestamp.isoformat(),
        }


class ApprovalService:
    """Validates and persists decisions. Knows nothing about the graph."""

    def __init__(self, db: Database, *, clock: Clock = _utc_now) -> None:
        self._db = db
        self._clock = clock

    def application_for(self, application_id: int) -> ApplicationRecord:
        application = self._db.get_application(application_id)
        if application is None:
            raise UnknownApplicationError(application_id)
        return application

    def application_for_thread(self, thread_id: str) -> ApplicationRecord:
        application = self._db.get_application_by_thread(thread_id)
        if application is None:
            raise UnknownThreadError(thread_id)
        return application

    def recorded(self, application_id: int) -> ApprovalRecord | None:
        return self._db.get_approval(application_id)

    def queue_item_for(self, application: ApplicationRecord) -> QueueItem | None:
        """The queue row an application came from, for rendering a prompt."""
        return self._db.get_queue_item(application.queue_id)

    def decide(
        self, request: ApprovalRequest, *, thread_id: str | None = None
    ) -> ApprovalOutcome:
        """Validate, then persist, one decision.

        `thread_id` is the thread a caller believes it is resuming. When
        given, it must be the one the application actually owns; the
        mismatch is refused before any lookup of an existing decision, so a
        forged pairing never even reads another application's audit record.
        """
        application = self.application_for(request.application_id)
        if thread_id is not None and application.thread_id != thread_id:
            raise ApplicationThreadMismatch(
                request.application_id, thread_id, application.thread_id
            )

        existing = self._db.get_approval(request.application_id)
        if existing is not None:
            return self._replay_or_conflict(application, request, existing)

        if application.status is not ApplicationStatus.AWAITING_APPROVAL:
            raise ApplicationNotAwaitingApproval(application.id, application.status)

        recorded, stored = self._db.try_record_approval(
            application_id=request.application_id,
            decision=request.decision,
            actor=request.actor,
            note=request.note,
            timestamp=self._clock(),
        )
        if not recorded:
            # Another gate won the race between the read above and this
            # insert. Its record is the real one, so this request is judged
            # against it exactly as a late duplicate would be.
            return self._replay_or_conflict(application, request, stored)
        return ApprovalOutcome(application=application, record=stored, replayed=False)

    def _replay_or_conflict(
        self,
        application: ApplicationRecord,
        request: ApprovalRequest,
        existing: ApprovalRecord,
    ) -> ApprovalOutcome:
        """An identical repeat is a replay; anything else is a conflict.

        The application's *current* status is deliberately not rechecked
        here. By the time a client retries, the graph has usually already
        resumed and moved the application on, and reporting a failure for a
        decision that was in fact recorded and acted upon would be wrong.
        """
        if request.matches(existing):
            return ApprovalOutcome(
                application=application, record=existing, replayed=True
            )
        raise ApprovalConflict(
            application.id, existing, request.differing_field(existing)
        )


class ThreadResumer(Protocol):
    """The graph side of a gate: resume one thread with one decision."""

    async def resume_application(
        self, thread_id: str, decision: ApprovalRequest
    ) -> Any: ...


class BaseApprovalGate:
    """Shared plumbing: resolve the thread from the record, then resume.

    Subclasses only differ in how they obtain the `ApprovalRequest`. The
    thread is looked up here from the application row, so no front end has
    the opportunity to supply one.
    """

    def __init__(self, service: ApprovalService, resumer: ThreadResumer) -> None:
        self._service = service
        self._resumer = resumer

    @property
    def service(self) -> ApprovalService:
        return self._service

    async def decide(self, request: ApprovalRequest) -> Any:
        application = self._service.application_for(request.application_id)
        return await self._resumer.resume_application(application.thread_id, request)


class ApiApprovalGate(BaseApprovalGate):
    """The gate behind `POST /applications/{id}/approve` and `/reject`."""

    async def decide_payload(
        self, application_id: int, payload: Mapping[str, Any]
    ) -> Any:
        """Decide from an untrusted request body.

        The application id comes from the route; the body supplies only the
        decision, the actor, and an optional note.
        """
        return await self.decide(ApprovalRequest.from_payload(application_id, payload))


class CliApprovalGate(BaseApprovalGate):
    """The gate behind the console prompt.

    `reader` and `writer` are injected so the prompt is testable without a
    terminal, and so a batch runner can drive it from something other than
    stdin.
    """

    def __init__(
        self,
        service: ApprovalService,
        resumer: ThreadResumer,
        *,
        reader: Callable[[], str] = input,
        writer: Callable[[str], Any] = print,
        max_attempts: int = 5,
    ) -> None:
        super().__init__(service, resumer)
        self._reader = reader
        self._writer = writer
        self._max_attempts = max(1, max_attempts)

    async def prompt(self, application_id: int, *, actor: str) -> Any:
        """Show what is pending, read a decision, and resume the thread.

        An unrecognised answer is asked again rather than interpreted; after
        `max_attempts` the prompt gives up with `InvalidApprovalRequest`
        instead of looping forever against a non-interactive reader.
        """
        application = self._service.application_for(application_id)
        for line in self._render(application):
            self._writer(line)

        decision = self._read_decision()
        self._writer("Note (optional, press enter to skip):")
        note = self._reader()
        return await self.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=decision,
                actor=actor,
                note=note,
            )
        )

    def _render(self, application: ApplicationRecord) -> list[str]:
        queue_item = self._service.queue_item_for(application)
        listing = queue_item.listing_url if queue_item is not None else "<unknown>"
        board = queue_item.board.value if queue_item is not None else "<unknown>"
        return [
            f"Application {application.id} is awaiting approval.",
            f"  board:   {board}",
            f"  listing: {listing}",
            f"  ats:     {application.ats or 'unknown'}",
            f"  status:  {application.status.value}",
            "Approve or reject? [approve/reject]",
        ]

    def _read_decision(self) -> ApprovalDecision:
        last: InvalidApprovalRequest | None = None
        for _attempt in range(self._max_attempts):
            try:
                return parse_decision(self._reader())
            except InvalidApprovalRequest as exc:
                last = exc
                self._writer(f"{exc} Please answer 'approve' or 'reject'.")
        raise InvalidApprovalRequest(
            f"no decision after {self._max_attempts} attempts; the last answer "
            f"was refused because: {last}"
        )
