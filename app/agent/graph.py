"""The durable per-application state machine.

One queue item becomes one LangGraph thread, checkpointed to SQLite, that
walks the sequence in the design document — open the listing, click Apply,
detect the ATS, snapshot the form, trigger Jobright Autofill, attribute what
changed, fill only the gaps that are safe to fill — and then *stops*. The
stop is a real `interrupt()` against a persisted checkpoint, not a blocking
prompt: the process can exit, and the same thread is still resumable
afterwards by the CLI gate, the API gate, or a different worker reading the
same file.

Four properties shape everything below.

**Nothing submits without a decision.** `AUTO_SUBMIT` is off by default, and
even when an operator turns it on the gate still runs whenever a protected
question is on the form, a gap is unanswered, an answer could not actually
be typed, the page never settled, or part of the page could not be scanned.
"Auto-submit" therefore means "submit a form this code fully filled and
fully saw", never "submit whatever is there".

**One bad listing ends one application.** An unknown ATS, a captcha, a login
wall, an unavailable extension, a failed trigger, and a reached rate cap are
typed failures that route to a terminal node, persist a reason (and a
screenshot where one can be taken), and return normally. `run_application`
does not raise for any of them, so a batch runner's next item is unaffected.
Only genuinely unexpected errors reach the `fail` node, and they are
contained the same way.

**Transient browser objects never enter the checkpoint.** State holds ints,
strings, and plain dicts only. The baseline snapshot and the trigger result
live in an in-memory cache scoped to one `build_graph` call and are only
needed *within* one uninterrupted invocation; the staged page itself is held
by the injected `PageBroker`. A resume that finds no staged page fails
loudly (`PageUnavailable`) rather than "submitting" against nothing.

**Every dependency is injected.** The browser, the adapter registry, the
trigger, the gap filler, the field writer, the submitter, the page guard,
and the screenshotter are all protocols, which is what lets a full
staged-then-approved run and a full staged-then-rejected run be proven with
no browser and no network at all.
"""

from __future__ import annotations

import asyncio
import operator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    TypedDict,
)

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.approval import (
    ApplicationThreadMismatch,
    ApprovalConflict,
    ApprovalError,
    ApprovalRequest,
    ApprovalService,
    InvalidApprovalRequest,
    parse_decision,
)
from app.agent.ats_detector import AtsKind, detect_ats as default_detect_ats
from app.agent.errors import (
    CaptchaEncountered,
    ExtensionNotFoundError,
    LoginWallEncountered,
    PageUnavailable,
    RateLimitExceeded,
    ServiceWorkerNotFoundError,
    ServiceWorkerUnresponsiveError,
    StagingArtefactsLost,
    TriggerFailed,
    UnknownAtsLayout,
)
from app.agent.form_scanner import FormField, FormSnapshot
from app.agent.gap_filler import GapFillPlan
from app.agent.jobright_trigger import TIER_ORDER, TriggerResult
from app.agent.rate_limiter import RateLimiter
from app.boards.base import (
    ApplyResult,
    ApplyStatus,
    BoardAdapter,
    UntrustedListingUrlError,
)
from app.boards.registry import adapter_for as registry_adapter_for
from app.config import Settings
from app.storage.db import Database
from app.storage.logger import ApplicationLogger
from app.storage.models import (
    TERMINAL_APPLICATION_STATUSES,
    TERMINAL_QUEUE_STATES,
    ApplicationRecord,
    ApplicationStatus,
    ApprovalDecision,
    ApprovalRecord,
    Board,
    FieldSource,
    QueueItem,
    QueueState,
)

#: The staging path, in the order the design document specifies. Exported so
#: a test can assert the order rather than infer it from the wiring, and so
#: the wiring below is built from the same tuple that is asserted.
STAGING_NODES: tuple[str, ...] = (
    "admit",
    "open_listing",
    "start_application",
    "detect_ats",
    "snapshot_fields",
    "trigger_autofill",
    "attribute_fields",
    "fill_gaps",
    "stage",
)

TERMINAL_NODES: tuple[str, ...] = ("submit", "reject", "skip", "fail")

#: Checkpoints are written before the next node starts, not concurrently
#: with it. The whole point of this graph is that a process can die at any
#: moment and the application is still recoverable; a checkpoint that was
#: merely scheduled is exactly the one a crash loses, and losing it here
#: means an application whose real state is a filled form in a browser and
#: whose recorded state is "still staging".
CHECKPOINT_DURABILITY = "sync"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def thread_id_for(queue_id: int) -> str:
    """The graph thread one queue item always uses.

    Deterministic on purpose: a worker restarted mid-application must land
    on the same thread and resume it, rather than opening a second one and
    clicking Apply on the same listing twice.
    """
    return f"application-{queue_id}"


class RunStatus(str, Enum):
    """Where one application ended up."""

    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    FAILED = "failed"


class SkipKind(str, Enum):
    """Typed reasons an application is abandoned without being failed.

    Each one is a condition where continuing would be unsafe or pointless
    rather than broken: the run ends, the reason is persisted on the queue
    row, and the next queue item is unaffected.
    """

    UNKNOWN_ATS = "unknown_ats"
    CAPTCHA = "captcha_required"
    LOGIN_WALL = "login_required"
    EXTENSION_UNAVAILABLE = "extension_unavailable"
    TRIGGER_FAILED = "trigger_failed"
    RATE_CAP_REACHED = "rate_cap_reached"
    UNTRUSTED_LISTING_URL = "untrusted_listing_url"
    BOARD_FLOW_UNSUPPORTED = "board_flow_unsupported"
    #: The tab an application was being staged in is gone.
    STAGED_PAGE_LOST = "staged_page_lost"
    #: The thread was picked up by a worker that does not hold the in-memory
    #: artefacts the previous attempt staged.
    STAGING_LOST = "staging_artefacts_lost"


class BlockingReason(str, Enum):
    """Why the approval gate must run even when `AUTO_SUBMIT` is enabled."""

    PROTECTED_QUESTION = "protected_question"
    UNANSWERED_GAP = "unanswered_gap"
    HUMAN_REQUIRED = "human_required"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    UNWRITTEN_ANSWER = "unwritten_answer"
    PAGE_NEVER_SETTLED = "page_never_settled"


#: Typed failures that end one application without failing it, in the order
#: they are matched. Order matters: several of these share a base class, and
#: the first match wins.
_SKIP_KINDS: tuple[tuple[type[BaseException], SkipKind], ...] = (
    (RateLimitExceeded, SkipKind.RATE_CAP_REACHED),
    (UnknownAtsLayout, SkipKind.UNKNOWN_ATS),
    (CaptchaEncountered, SkipKind.CAPTCHA),
    (LoginWallEncountered, SkipKind.LOGIN_WALL),
    (ExtensionNotFoundError, SkipKind.EXTENSION_UNAVAILABLE),
    (ServiceWorkerNotFoundError, SkipKind.EXTENSION_UNAVAILABLE),
    (ServiceWorkerUnresponsiveError, SkipKind.EXTENSION_UNAVAILABLE),
    (TriggerFailed, SkipKind.TRIGGER_FAILED),
    (UntrustedListingUrlError, SkipKind.UNTRUSTED_LISTING_URL),
    # Losing the tab or the in-memory staging artefacts part-way through is
    # not a malfunction: nothing has been submitted, and the listing can be
    # staged again from the beginning. Failing here instead would fill the
    # operator's failure log with entries whose only remedy is "run it
    # again", and bury the ones that mean something is actually broken.
    (StagingArtefactsLost, SkipKind.STAGING_LOST),
    (PageUnavailable, SkipKind.STAGED_PAGE_LOST),
)


class UnknownQueueItem(Exception):
    """Raised when `run_application` is given an id no queue row has."""

    def __init__(self, queue_id: int) -> None:
        self.queue_id = queue_id
        super().__init__(f"No queue item with id {queue_id}")


class ThreadNotAwaitingApproval(ApprovalError):
    """Raised when a thread has no pending interrupt to resume.

    Subclasses `ApprovalError` so an API layer can map every refusal from
    the gate through one branch. Distinct from
    `ApplicationNotAwaitingApproval`, which is about the *database* row:
    this one is about the checkpoint, and it is the authoritative one, since
    the checkpoint is what a resume would actually act on.
    """

    def __init__(self, thread_id: str, application_id: int | None = None) -> None:
        self.thread_id = thread_id
        self.application_id = application_id
        super().__init__(
            f"Thread {thread_id!r} is not paused at the approval gate; there "
            "is nothing to resume. The application either has not reached the "
            "gate yet or has already been decided."
        )


class ResumeInProgress(ApprovalError):
    """Raised when another worker already holds this application's decision.

    Only reachable across processes: within one runner the resumes for a
    thread are serialised, so the second caller waits and then reads the
    finished outcome. Two workers on one database cannot wait for each
    other, so the loser is told to come back rather than being allowed to
    submit the same form a second time.
    """

    def __init__(self, thread_id: str, application_id: int) -> None:
        self.thread_id = thread_id
        self.application_id = application_id
        super().__init__(
            f"Application {application_id} on thread {thread_id!r} is already "
            "being resumed by another worker. Its decision is recorded and "
            "will be acted on there; retry this call afterwards to read the "
            "outcome."
        )


class ApplicationState(TypedDict, total=False):
    """Everything checkpointed about one application.

    Deliberately plain data: ints, strings, bools, and lists/dicts of them.
    A `FormSnapshot`, a page handle, or a `Decimal` in here would either
    fail to round-trip through the checkpointer or quietly pin the on-disk
    format to this project's class shapes. The model cost is carried as its
    exact decimal *string* for that reason.
    """

    queue_id: int
    thread_id: str
    application_id: int
    listing_url: str
    board: str
    #: Nodes that actually ran, in order. Appended to, never replaced, so
    #: the finished value is the real path through the graph.
    visited: Annotated[list[str], operator.add]
    ats: str
    trigger_tier: int
    trigger_tier_name: str
    settled: bool
    attributed_fields: list[str]
    gap_summary: list[dict[str, Any]]
    blocking_reasons: list[str]
    model_cost: str
    gate: str
    #: The decision only. Who made it and what they wrote about it are
    #: deliberately absent: the approvals table is the audit record, and a
    #: checkpoint file travels with a working directory in a way that an
    #: access-controlled table does not.
    decision: str
    decided_at: str
    outcome: str
    #: The stable, machine-readable outcome — a `SkipKind` or a short
    #: phrase — that a caller may branch on.
    reason: str
    #: The sentence underneath it, naming the particular page, thread, or
    #: control involved. Separate from `reason` so that stability and
    #: informativeness do not have to be traded against each other.
    detail: str
    screenshot_path: str
    #: Set by any node that could not continue. Its `terminal` key names the
    #: node the router sends the run to.
    failure: dict[str, str]


@dataclass(frozen=True)
class SubmitOutcome:
    """What a submitter did. `submitted` is never inferred from the absence
    of an exception: a submit control that never appeared is a failure, not
    a quiet success."""

    submitted: bool
    reason: str = ""
    screenshot_path: str | None = None


@dataclass(frozen=True)
class RunResult:
    """The outcome of one `run_application` or `resume_application` call."""

    thread_id: str
    queue_id: int
    application_id: int | None
    status: RunStatus
    #: Stable enough to branch on: a `SkipKind` value, or a short phrase.
    reason: str = ""
    #: The specifics behind `reason`, when there are any.
    detail: str | None = None
    interrupt: dict[str, Any] | None = None
    ats: str | None = None
    trigger_tier: int | None = None
    model_cost: Decimal = Decimal("0")
    blocking_reasons: tuple[str, ...] = ()
    visited: tuple[str, ...] = ()
    gaps: tuple[dict[str, Any], ...] = ()
    decision: str | None = None
    actor: str | None = None
    note: str | None = None
    decided_at: str | None = None

    @property
    def awaiting_approval(self) -> bool:
        return self.status is RunStatus.AWAITING_APPROVAL

    @property
    def submitted(self) -> bool:
        return self.status is RunStatus.SUBMITTED


class PageBroker(Protocol):
    """Owns the tabs. One live page per thread, for as long as it lives."""

    async def open(self, thread_id: str) -> Any: ...

    async def get(self, thread_id: str) -> Any | None: ...

    async def release(self, thread_id: str) -> None: ...


class AutofillTrigger(Protocol):
    """The slice of `JobrightTrigger` the graph uses."""

    async def baseline(self, page: Any) -> FormSnapshot: ...

    async def trigger(self, page: Any, before: FormSnapshot) -> TriggerResult: ...


class GapFillerLike(Protocol):
    """The slice of `GapFiller` the graph uses."""

    async def fill(
        self, fields: Any, *, skipped_frames: Sequence[str] = ()
    ) -> GapFillPlan: ...

    def log_payload(self, plan: GapFillPlan) -> tuple[dict[str, Any], ...]: ...


class FieldWriter(Protocol):
    """Types one decided answer into one control.

    Returns whether the value actually landed. A `False` here keeps the
    approval gate in play even under `AUTO_SUBMIT`, because a form with an
    answer that was decided but never typed is not a filled form.
    """

    async def write(self, page: Any, key: str, value: str) -> bool: ...


class Submitter(Protocol):
    async def submit(self, page: Any) -> SubmitOutcome: ...


class PageGuard(Protocol):
    """Refuses to continue on a challenged or gated page.

    Implementations raise `CaptchaEncountered` or `LoginWallEncountered`;
    returning normally means the page is safe to keep working on.
    """

    async def inspect(self, page: Any) -> None: ...


class Screenshotter(Protocol):
    async def capture(self, page: Any, name: str) -> str | None: ...


@dataclass(frozen=True)
class GraphDependencies:
    """Everything the nodes touch, injected rather than constructed.

    The callables use `default_factory` rather than a plain default because
    a bare function stored as a dataclass *class* attribute is a descriptor
    and would bind as a method on attribute access, silently passing the
    dependencies object as the first argument.
    """

    db: Database
    settings: Settings
    logger: ApplicationLogger
    rate_limiter: RateLimiter
    pages: PageBroker
    trigger: AutofillTrigger
    gap_filler: GapFillerLike
    writer: FieldWriter
    submitter: Submitter
    guard: PageGuard | None = None
    screenshots: Screenshotter | None = None
    adapter_for: Callable[[Board], BoardAdapter] = dataclass_field(
        default_factory=lambda: registry_adapter_for
    )
    detect_ats: Callable[[str, str | None], AtsKind] = dataclass_field(
        default_factory=lambda: default_detect_ats
    )
    clock: Callable[[], datetime] = dataclass_field(default_factory=lambda: _utc_now)


@dataclass
class _Staging:
    """Browser artefacts for one in-flight invocation. Never checkpointed."""

    baseline: FormSnapshot | None = None
    result: TriggerResult | None = None


def _failure(kind: str, reason: str, terminal: str) -> dict[str, str]:
    return {"kind": kind, "reason": reason, "terminal": terminal}


def _classify(error: BaseException) -> dict[str, str]:
    """Turn an exception into a terminal route.

    Anything in `_SKIP_KINDS` ends the application without failing it;
    everything else is unexpected and fails the queue item so an operator
    sees it. Either way the exception stops here rather than propagating out
    of `run_application` and taking the rest of the batch with it.
    """
    for error_type, kind in _SKIP_KINDS:
        if isinstance(error, error_type):
            return _failure(kind.value, str(error), "skip")
    return _failure(type(error).__name__, str(error), "fail")


def _page_url(page: Any, fallback: str) -> str:
    """A page's URL, whether it exposes one as a property or not."""
    url = getattr(page, "url", None)
    if callable(url):  # pragma: no cover - Playwright's is a property
        url = url()
    return str(url) if url else fallback


async def _page_html(page: Any) -> str | None:
    content = getattr(page, "content", None)
    if content is None:
        return None
    return str(await content())


def build_graph(
    dependencies: GraphDependencies, checkpointer: BaseCheckpointSaver[Any]
) -> Any:
    """Compile the application graph against a checkpointer.

    The returned graph is stateless between threads; per-thread browser
    artefacts live in the `staging` cache closed over here, which is scoped
    to this graph and holds nothing that needs to survive an interrupt.
    """
    deps = dependencies
    staging: dict[str, _Staging] = {}

    def staged(thread_id: str) -> _Staging:
        return staging.setdefault(thread_id, _Staging())

    async def require_page(state: ApplicationState) -> Any:
        thread_id = state["thread_id"]
        page = await deps.pages.get(thread_id)
        if page is None:
            raise PageUnavailable(thread_id)
        return page

    async def guard(page: Any) -> None:
        if deps.guard is not None:
            await deps.guard.inspect(page)

    async def capture(state: ApplicationState, name: str) -> str | None:
        """Best-effort screenshot. A failed screenshot never masks a failure."""
        if deps.screenshots is None:
            return None
        page = await deps.pages.get(state["thread_id"])
        if page is None:
            return None
        try:
            return await deps.screenshots.capture(page, f"{state['thread_id']}-{name}")
        except Exception:  # noqa: BLE001 - diagnostics must not replace the cause
            return None

    def contained(
        name: str, body: Callable[[ApplicationState], Awaitable[dict[str, Any]]]
    ) -> Any:
        """Run one node, recording that it ran and containing its failures."""

        async def node(state: ApplicationState) -> dict[str, Any]:
            try:
                update = await body(state)
            except GraphBubbleUp:
                # LangGraph's own control flow — an interrupt, a parent
                # command — travels as an exception. Classifying one as a
                # node failure would turn a request for a human decision
                # into a permanently failed application.
                raise
            except Exception as exc:  # noqa: BLE001 - classified, never swallowed
                return {"visited": [name], "failure": _classify(exc)}
            return {"visited": [name], **update}

        node.__name__ = name
        return node

    async def admit(state: ApplicationState) -> dict[str, Any]:
        queue_id = state["queue_id"]
        thread_id = state["thread_id"]
        item = deps.db.get_queue_item(queue_id)
        if item is None:  # pragma: no cover - the runner checks this first
            raise UnknownQueueItem(queue_id)

        existing = deps.db.get_application_by_thread(thread_id)
        if existing is None:
            application_id = deps.db.create_application(
                queue_id=queue_id,
                thread_id=thread_id,
                status=ApplicationStatus.STAGING,
            )
        else:
            application_id = existing.id
            deps.db.update_application(
                application_id, status=ApplicationStatus.STAGING
            )
        deps.db.update_queue_state(queue_id, QueueState.RUNNING)

        update: dict[str, Any] = {
            "application_id": application_id,
            "listing_url": item.listing_url,
            "board": item.board.value,
        }
        try:
            deps.rate_limiter.check_and_record(item.board, "apply")
        except RateLimitExceeded as exc:
            # Returned rather than raised so the application id above still
            # reaches the terminal node; without it the skip would be
            # recorded against the queue row only.
            update["failure"] = _failure(
                SkipKind.RATE_CAP_REACHED.value, str(exc), "skip"
            )
        return update

    async def open_listing(state: ApplicationState) -> dict[str, Any]:
        page = await deps.pages.open(state["thread_id"])
        adapter = deps.adapter_for(Board(state["board"]))
        await adapter.open_listing(page, state["listing_url"])
        await guard(page)
        return {}

    async def start_application(state: ApplicationState) -> dict[str, Any]:
        page = await require_page(state)
        adapter = deps.adapter_for(Board(state["board"]))
        result: ApplyResult = await adapter.start_application(page)
        if result.status is ApplyStatus.SKIPPED:
            return {
                "failure": _failure(
                    result.reason or SkipKind.BOARD_FLOW_UNSUPPORTED.value,
                    result.reason,
                    "skip",
                )
            }
        if result.status is not ApplyStatus.STARTED:
            return {"failure": _failure("apply_failed", result.reason, "fail")}
        # An Apply click is the most common place to land on a login wall.
        await guard(page)
        return {}

    async def detect_ats(state: ApplicationState) -> dict[str, Any]:
        page = await require_page(state)
        url = _page_url(page, state["listing_url"])
        kind = deps.detect_ats(url, await _page_html(page))
        if kind is AtsKind.UNKNOWN:
            raise UnknownAtsLayout(url)
        deps.db.update_application(state["application_id"], ats=kind.value)
        return {"ats": kind.value}

    async def snapshot_fields(state: ApplicationState) -> dict[str, Any]:
        page = await require_page(state)
        staged(state["thread_id"]).baseline = await deps.trigger.baseline(page)
        return {}

    async def trigger_autofill(state: ApplicationState) -> dict[str, Any]:
        page = await require_page(state)
        cache = staged(state["thread_id"])
        baseline = cache.baseline
        if baseline is None:  # pragma: no cover - snapshot_fields always sets it
            baseline = await deps.trigger.baseline(page)
        result = await deps.trigger.trigger(page, baseline)
        cache.result = result
        tier = TIER_ORDER.index(result.tier) + 1
        deps.db.update_application(state["application_id"], trigger_tier=tier)
        return {
            "trigger_tier": tier,
            "trigger_tier_name": result.tier.value,
            "settled": result.settled,
        }

    async def attribute_fields(state: ApplicationState) -> dict[str, Any]:
        result = staged(state["thread_id"]).result
        if result is None:
            raise StagingArtefactsLost(state["thread_id"], "trigger result")
        attributed: list[str] = []
        for change in result.diff.changed:
            after = change.after
            deps.logger.log_field(
                application_id=state["application_id"],
                stable_key=after.key,
                source=FieldSource.JOBRIGHT,
                required=after.required,
                filled=after.filled,
                value=after.value,
                metadata={
                    "label": after.label,
                    "name": after.name,
                    "field_type": after.field_type,
                    "frame_url": after.frame_url,
                    "trigger_tier": result.tier.value,
                    "became_filled": change.became_filled,
                },
            )
            attributed.append(after.key)
        return {"attributed_fields": attributed}

    async def fill_gaps(state: ApplicationState) -> dict[str, Any]:
        page = await require_page(state)
        result = staged(state["thread_id"]).result
        if result is None:
            raise StagingArtefactsLost(state["thread_id"], "trigger result")

        plan = await deps.gap_filler.fill(
            _open_gaps(result),
            skipped_frames=[skip.frame_url for skip in result.diff.skipped_frames],
        )
        # Charged before a single answer is typed. The provider billed for
        # those completions the moment it produced them, and the writing
        # below is the fallible part: a crash in the middle of it re-runs
        # this whole node, model calls included. Recording the spend last
        # would lose every crashed attempt's bill, and recording it as an
        # assignment rather than an addition would lose all but the last.
        total = deps.db.add_model_cost(state["application_id"], plan.model_cost)
        unwritten = await _apply(plan, page, state["application_id"])

        return {
            "model_cost": str(total),
            "gap_summary": [dict(entry) for entry in deps.gap_filler.log_payload(plan)],
            # No default for `settled`: a state that never recorded whether
            # the page stopped changing is a state that cannot say the form
            # was stable when it was read.
            "blocking_reasons": _blocking(plan, unwritten, state.get("settled", False)),
        }

    async def _apply(
        plan: GapFillPlan, page: Any, application_id: int
    ) -> list[str]:
        """Type each decided answer, then record what every gap ended up as."""
        unwritten: list[str] = []
        for item in plan.items:
            applied = False
            if item.answer is not None:
                applied = bool(await deps.writer.write(page, item.key, item.answer))
                if not applied:
                    unwritten.append(item.key)
            deps.logger.log_field(
                application_id=application_id,
                stable_key=item.key,
                # An unanswered gap is recorded against the applicant: they
                # are who still has to supply it.
                source=item.source or FieldSource.USER,
                required=item.required,
                filled=applied,
                value=item.answer,
                metadata={
                    "label": item.label,
                    "name": item.name,
                    "field_type": item.field_type,
                    "resolution": item.resolution.value,
                    "category": item.category.value if item.category else None,
                    "reason": item.reason,
                    "cost": str(item.cost),
                },
            )
        return unwritten

    async def stage(state: ApplicationState) -> dict[str, Any]:
        # Nothing after this point reads the baseline or the trigger result,
        # so they are dropped here rather than at the terminal node. That
        # makes "no browser artefact crosses the interrupt" structural — an
        # application left pending for a week holds no snapshot in memory —
        # instead of merely true of the current node order.
        staging.pop(state["thread_id"], None)
        if _auto_submittable(state, deps.settings.auto_submit):
            return {"gate": "auto_submit"}
        deps.db.update_application(
            state["application_id"], status=ApplicationStatus.AWAITING_APPROVAL
        )
        return {"gate": "approval"}

    async def approval_gate(state: ApplicationState) -> dict[str, Any]:
        """Pause durably until a decision arrives.

        Deliberately side-effect free before `interrupt()`: LangGraph
        re-executes this node from the top when the thread resumes, so
        anything written here would be written twice. Persisting
        `awaiting_approval` is `stage`'s job for exactly that reason.
        """
        resumed = interrupt(_gate_payload(state))
        return {"visited": ["approval_gate"], **_decision_from(resumed, state)}

    async def submit(state: ApplicationState) -> dict[str, Any]:
        try:
            page = await require_page(state)
            outcome = await deps.submitter.submit(page)
        except GraphBubbleUp:
            raise
        except Exception as exc:  # noqa: BLE001 - contained like any other node
            # A submission that could not even be attempted is a failure
            # rather than a skip: the application was *approved*, so someone
            # is waiting on it and needs to be told it did not happen. The
            # classified kind goes on the queue row, because an operator
            # reading `error_reason` should not find it empty.
            classified = _classify(exc)
            return await _terminal(
                state,
                "submit",
                RunStatus.FAILED,
                classified["kind"],
                ApplicationStatus.FAILED,
                QueueState.FAILED,
                queue_reason=classified["kind"],
                detail=classified["reason"] or str(exc),
            )
        if not outcome.submitted:
            reason = outcome.reason or "the submission did not complete"
            return await _terminal(
                state,
                "submit",
                RunStatus.FAILED,
                reason,
                ApplicationStatus.FAILED,
                QueueState.FAILED,
                queue_reason=reason,
                screenshot=outcome.screenshot_path,
            )
        return await _terminal(
            state,
            "submit",
            RunStatus.SUBMITTED,
            outcome.reason or "submitted",
            ApplicationStatus.SUBMITTED,
            QueueState.COMPLETED,
            screenshot=outcome.screenshot_path,
        )

    async def reject(state: ApplicationState) -> dict[str, Any]:
        return await _terminal(
            state,
            "reject",
            RunStatus.REJECTED,
            # A fixed reason, not the reviewer's note: `reason` is the
            # machine-readable outcome (and reaches logs), while the note is
            # their own words and already lives in the approvals table.
            "rejected at the approval gate",
            ApplicationStatus.REJECTED,
            QueueState.COMPLETED,
        )

    async def skip(state: ApplicationState) -> dict[str, Any]:
        failure = state.get("failure", {})
        kind = failure.get("kind", SkipKind.BOARD_FLOW_UNSUPPORTED.value)
        return await _terminal(
            state,
            "skip",
            RunStatus.SKIPPED,
            kind,
            ApplicationStatus.SKIPPED,
            QueueState.SKIPPED,
            queue_reason=kind,
            # Which page, which thread, which control. Every skip of a kind
            # reads the same without it.
            detail=failure.get("reason", ""),
            screenshot=await capture(state, kind),
        )

    async def fail(state: ApplicationState) -> dict[str, Any]:
        failure = state.get("failure", {})
        kind = failure.get("kind", "unknown_error")
        detail = failure.get("reason", "") or kind
        return await _terminal(
            state,
            "fail",
            RunStatus.FAILED,
            detail,
            ApplicationStatus.FAILED,
            QueueState.FAILED,
            queue_reason=kind,
            detail=detail,
            screenshot=await capture(state, kind),
        )

    async def _terminal(
        state: ApplicationState,
        name: str,
        outcome: RunStatus,
        reason: str,
        application_status: ApplicationStatus,
        queue_state: QueueState,
        *,
        queue_reason: str | None = None,
        detail: str = "",
        screenshot: str | None = None,
    ) -> dict[str, Any]:
        """Persist one final outcome and let go of the page.

        Releasing the page here rather than in the runner means it happens
        on every path out of the graph, including the contained-failure
        ones, so a skipped application never leaves a tab behind.

        Both writes are refused by storage if the record has already
        finished, so a node re-executed after a crash restates the outcome
        rather than replacing one that was reached in the meantime.
        """
        application_id = state.get("application_id")
        if application_id is not None:
            deps.db.update_application(
                application_id,
                status=application_status,
                screenshot_path=screenshot,
            )
        deps.db.update_queue_state(state["queue_id"], queue_state, queue_reason)
        try:
            await deps.pages.release(state["thread_id"])
        except Exception:  # noqa: BLE001 - a tab that will not close is not an outcome
            # The outcome above is already persisted. Letting a failed tab
            # close raise here would turn a recorded submission into an
            # exception out of `run_application` and stop the batch.
            pass
        staging.pop(state["thread_id"], None)
        update: dict[str, Any] = {
            "visited": [name],
            "outcome": outcome.value,
            "reason": reason,
            "detail": detail,
        }
        if screenshot is not None:
            update["screenshot_path"] = screenshot
        return update

    graph: StateGraph[ApplicationState, None, ApplicationState, ApplicationState] = (
        StateGraph(ApplicationState)
    )
    bodies = {
        "admit": admit,
        "open_listing": open_listing,
        "start_application": start_application,
        "detect_ats": detect_ats,
        "snapshot_fields": snapshot_fields,
        "trigger_autofill": trigger_autofill,
        "attribute_fields": attribute_fields,
        "fill_gaps": fill_gaps,
        "stage": stage,
    }
    for name in STAGING_NODES:
        graph.add_node(name, contained(name, bodies[name]))
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("submit", submit)
    graph.add_node("reject", reject)
    graph.add_node("skip", skip)
    graph.add_node("fail", fail)

    graph.add_edge(START, STAGING_NODES[0])
    for name, following in zip(STAGING_NODES, STAGING_NODES[1:]):
        graph.add_conditional_edges(
            name, _continue_to(following), [following, "skip", "fail"]
        )
    graph.add_conditional_edges(
        "stage", _after_stage, ["approval_gate", "submit", "skip", "fail"]
    )
    graph.add_conditional_edges(
        "approval_gate", _after_gate, ["submit", "reject", "fail"]
    )
    for name in TERMINAL_NODES:
        graph.add_edge(name, END)

    return graph.compile(checkpointer=checkpointer)


def _open_gaps(result: TriggerResult) -> list[FormField]:
    """Every control still needing an answer, each listed once."""
    gaps: dict[str, FormField] = {}
    for field in (*result.diff.still_empty_required, *result.diff.unanswered_free_text):
        gaps.setdefault(field.key, field)
    return list(gaps.values())


def _blocking(plan: GapFillPlan, unwritten: Sequence[str], settled: bool) -> list[str]:
    """Every reason the gate must run, even under `AUTO_SUBMIT`.

    Returned as a list rather than a boolean so the interrupt payload can
    tell a reviewer *why* they are being asked.
    """
    reasons: list[str] = []
    if plan.protected:
        reasons.append(BlockingReason.PROTECTED_QUESTION.value)
    if plan.unanswered:
        reasons.append(BlockingReason.UNANSWERED_GAP.value)
    if plan.requires_human:
        reasons.append(BlockingReason.HUMAN_REQUIRED.value)
    if not plan.coverage_complete:
        reasons.append(BlockingReason.COVERAGE_INCOMPLETE.value)
    if unwritten:
        reasons.append(BlockingReason.UNWRITTEN_ANSWER.value)
    if not settled:
        reasons.append(BlockingReason.PAGE_NEVER_SETTLED.value)
    return reasons


def _auto_submittable(state: ApplicationState, auto_submit: bool) -> bool:
    """Whether this state may bypass the approval gate.

    An absent `blocking_reasons` key is not an empty one. A state that never
    reached `fill_gaps` — because an older version wrote it, or because the
    key was lost in a schema change — has never been checked for protected
    questions, unanswered gaps, or unwritten answers, and reading its
    silence as consent is exactly the failure this gate exists to prevent.
    """
    if not auto_submit:
        return False
    if "blocking_reasons" not in state:
        return False
    return not state["blocking_reasons"]


def _gate_payload(state: ApplicationState) -> dict[str, Any]:
    """What a reviewer is shown, and what the checkpoint stores.

    `gap_summary` comes from `GapFiller.log_payload`, which honours
    `LOG_FIELD_VALUES`, so answer text does not reach the checkpoint file
    unless field-value logging was explicitly enabled.
    """
    return {
        "application_id": state["application_id"],
        "thread_id": state["thread_id"],
        "queue_id": state["queue_id"],
        "listing_url": state.get("listing_url", ""),
        "board": state.get("board", ""),
        "ats": state.get("ats", AtsKind.UNKNOWN.value),
        "trigger_tier": state.get("trigger_tier"),
        "model_cost": state.get("model_cost", "0"),
        "blocking_reasons": list(state.get("blocking_reasons", ())),
        "gaps": [dict(entry) for entry in state.get("gap_summary", ())],
    }


def _decision_from(resumed: Any, state: ApplicationState) -> dict[str, Any]:
    """Read a resume payload, refusing one that is not this application's.

    The runner already validates the decision against storage; this is the
    graph's own last check, so a `Command(resume=...)` sent straight at a
    compiled graph still cannot approve an application it does not name.
    """
    if not isinstance(resumed, Mapping):
        return {
            "failure": _failure(
                "malformed_resume",
                f"a resume payload must be a mapping, not {type(resumed).__name__}",
                "fail",
            )
        }
    if resumed.get("application_id") != state["application_id"]:
        return {
            "failure": _failure(
                "forged_resume",
                f"the resume payload names application "
                f"{resumed.get('application_id')!r}, but this thread is "
                f"application {state['application_id']}",
                "fail",
            )
        }
    try:
        decision = parse_decision(resumed.get("decision"))
    except InvalidApprovalRequest as exc:
        return {"failure": _failure("malformed_resume", str(exc), "fail")}
    # The decision and when it was taken, and nothing else. Who took it and
    # what they wrote stay in the approvals table, which is the audit
    # record; the checkpoint is a resumption artefact and does not need
    # them to route the run.
    return {
        "decision": decision.value,
        "decided_at": str(resumed.get("decided_at", "")),
    }


def _continue_to(following: str) -> Callable[[ApplicationState], str]:
    def route(state: ApplicationState) -> str:
        failure = state.get("failure")
        if failure:
            return failure.get("terminal", "fail")
        return following

    return route


def _after_stage(state: ApplicationState) -> str:
    failure = state.get("failure")
    if failure:
        return failure.get("terminal", "fail")
    return "submit" if state.get("gate") == "auto_submit" else "approval_gate"


def _after_gate(state: ApplicationState) -> str:
    failure = state.get("failure")
    if failure:
        return failure.get("terminal", "fail")
    if state.get("decision") == ApprovalDecision.APPROVED.value:
        return "submit"
    return "reject"


#: How a finished record maps back to a run outcome, for the paths that read
#: storage instead of a checkpoint.
_RECORDED_STATUSES: Mapping[ApplicationStatus, RunStatus] = {
    ApplicationStatus.SUBMITTED: RunStatus.SUBMITTED,
    ApplicationStatus.REJECTED: RunStatus.REJECTED,
    ApplicationStatus.SKIPPED: RunStatus.SKIPPED,
    ApplicationStatus.FAILED: RunStatus.FAILED,
}

_RECORDED_QUEUE_STATES: Mapping[QueueState, RunStatus] = {
    QueueState.SKIPPED: RunStatus.SKIPPED,
    QueueState.FAILED: RunStatus.FAILED,
}


def _recorded_result(
    thread_id: str,
    item: QueueItem | None,
    application: ApplicationRecord | None,
    approval: ApprovalRecord | None,
) -> RunResult | None:
    """The outcome as storage remembers it, or `None` if it remembers none.

    Built from the queue and application rows rather than the checkpoint, so
    it is still available to a worker whose checkpoint file is gone — which
    is exactly the situation in which reporting "no outcome" would be
    dangerous, because the caller's next move is to stage the listing again.
    """
    status: RunStatus | None = None
    if application is not None:
        status = _RECORDED_STATUSES.get(application.status)
    if status is None and item is not None:
        status = _RECORDED_QUEUE_STATES.get(item.state)
    if status is None:
        return None

    return RunResult(
        thread_id=thread_id,
        queue_id=item.id if item is not None else 0,
        application_id=application.id if application is not None else None,
        status=status,
        reason=(item.error_reason if item is not None else None) or status.value,
        detail="recovered from the recorded outcome, not from a checkpoint",
        ats=application.ats if application is not None else None,
        trigger_tier=application.trigger_tier if application is not None else None,
        model_cost=Decimal(
            str(application.model_cost or "0") if application is not None else "0"
        ),
        decision=approval.decision.value if approval is not None else None,
        actor=approval.actor if approval is not None else None,
        note=approval.note if approval is not None else None,
        decided_at=approval.timestamp.isoformat() if approval is not None else None,
    )


def _finished_result(
    thread_id: str,
    item: QueueItem,
    application: ApplicationRecord | None,
    service: ApprovalService,
) -> RunResult | None:
    """The recorded outcome of a queue item that is already over.

    A completed queue item whose application is still mid-flight is not
    over — that combination means a terminal node wrote the queue row and
    then the process died — so the application row has the final say
    wherever there is one.
    """
    if application is not None:
        if application.status not in TERMINAL_APPLICATION_STATUSES:
            return None
    elif item.state not in TERMINAL_QUEUE_STATES:
        return None

    approval = service.recorded(application.id) if application is not None else None
    return _recorded_result(thread_id, item, application, approval)


@asynccontextmanager
async def sqlite_checkpointer(
    path: Path | str,
) -> AsyncIterator[AsyncSqliteSaver]:
    """Open the SQLite checkpointer the durable interrupt depends on.

    The schema is created on entry rather than lazily, so a caller that only
    inspects a thread (a status endpoint, say) works against a brand-new
    file just as well as one that runs an application through it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(target)) as saver:
        await saver.setup()
        yield saver


def checkpointer_for(settings: Settings) -> Any:
    """The checkpointer path this project uses, alongside the queue database."""
    return sqlite_checkpointer(settings.sqlite_path.parent / "checkpoints.sqlite")


class ApplicationRunner:
    """Runs and resumes application threads.

    Satisfies `app.agent.approval.ThreadResumer`, so both approval gates
    drive the same object and therefore the same validation.
    """

    def __init__(
        self,
        dependencies: GraphDependencies,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> None:
        self._deps = dependencies
        self._graph = build_graph(dependencies, checkpointer)
        self._service = ApprovalService(dependencies.db, clock=dependencies.clock)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        """One lock per thread, so two decisions queue instead of racing.

        This is the in-process half of the guarantee. Two coroutines that
        both read "interrupted, decision matches" and then both resume would
        submit the same form twice, and no amount of validation before the
        read prevents it. The database claim in `resume_application` is the
        other half, for workers that cannot share a lock.
        """
        return self._locks.setdefault(thread_id, asyncio.Lock())

    @property
    def graph(self) -> Any:
        return self._graph

    @property
    def approvals(self) -> ApprovalService:
        return self._service

    async def run_application(self, queue_id: int) -> RunResult:
        """Stage one queue item, stopping at the approval gate.

        Safe to call again for the same queue item: a thread already paused
        at the gate is reported as-is rather than restaged, and a finished
        one reports its outcome. Re-staging would mean a second Apply click
        on the same listing.
        """
        item = self._deps.db.get_queue_item(queue_id)
        if item is None:
            raise UnknownQueueItem(queue_id)

        thread_id = thread_id_for(queue_id)
        application = self._deps.db.get_application_by_thread(thread_id)
        # Storage is consulted before the checkpoint, because it is the
        # record that survives losing the checkpoint file. Without this the
        # queue runner rediscovers a submitted application as an unstarted
        # one, opens the listing, and clicks Apply for a second time in
        # someone's name.
        finished = _finished_result(thread_id, item, application, self._service)
        if finished is not None:
            return finished

        config = _config(thread_id)
        state = await self._graph.aget_state(config)
        if state.created_at is not None:
            if state.interrupts or not state.next:
                return self._from_state(thread_id, queue_id, state)
            # A checkpoint that is neither finished nor paused belongs to a
            # run that died mid-flight; continue it rather than restarting.
            values = await self._graph.ainvoke(
                None, config, durability=CHECKPOINT_DURABILITY
            )
            return self._from_values(thread_id, queue_id, values)

        values = await self._graph.ainvoke(
            ApplicationState(
                queue_id=queue_id,
                thread_id=thread_id,
                listing_url=item.listing_url,
                board=item.board.value,
                visited=[],
            ),
            config,
            durability=CHECKPOINT_DURABILITY,
        )
        return self._from_values(thread_id, queue_id, values)

    async def pending_approval(self, thread_id: str) -> dict[str, Any] | None:
        """The interrupt payload a thread is paused on, if it is paused."""
        state = await self._graph.aget_state(_config(thread_id))
        if not state.interrupts:
            return None
        return dict(state.interrupts[0].value)

    async def resume_application(
        self, thread_id: str, decision: ApprovalRequest
    ) -> RunResult:
        """Apply one decision to one thread.

        Held under this thread's lock from the first read to the last write,
        so two decisions arriving together are applied one after the other
        rather than both against the same "still interrupted" snapshot. Every
        read that matters — the application row, the recorded approval, the
        checkpoint — happens inside it, because a value read before the lock
        is a value the other caller may already have changed.
        """
        async with self._lock_for(thread_id):
            return await self._resume_locked(thread_id, decision)

    async def _resume_locked(
        self, thread_id: str, decision: ApprovalRequest
    ) -> RunResult:
        """Apply one decision, with this thread's lock already held.

        Validation order is deliberate. The application the decision names
        is checked against the thread being resumed *before* any approval
        record is read or written, so a forged application id never reads
        another application's audit row, let alone releases it. Then storage
        is checked for a finished application, then the checkpoint for a
        pending interrupt, and only then is the decision recorded and the
        resume claimed.
        """
        application = self._service.application_for_thread(thread_id)
        named = self._service.application_for(decision.application_id)
        if named.thread_id != thread_id:
            raise ApplicationThreadMismatch(
                decision.application_id, thread_id, named.thread_id
            )

        config = _config(thread_id)
        if application.status in TERMINAL_APPLICATION_STATUSES:
            # The application is over, whatever the checkpoint says — and it
            # may say nothing at all, if the file was lost. An identical
            # decision reads the outcome it already caused; a different one
            # is still a conflict, because arriving late does not make it
            # agree with what was decided.
            existing = self._require_matching_approval(thread_id, application, decision)
            recorded = _recorded_result(
                thread_id,
                self._deps.db.get_queue_item(application.queue_id),
                application,
                existing,
            )
            if recorded is not None:
                return recorded
            state = await self._graph.aget_state(config)
            return self._from_state(thread_id, application.queue_id, state)

        state = await self._graph.aget_state(config)
        if not state.interrupts:
            self._require_matching_approval(thread_id, application, decision)
            return self._from_state(thread_id, application.queue_id, state)

        outcome = self._service.decide(decision, thread_id=thread_id)
        claimed, current = self._deps.db.claim_resume(application.id)
        if not claimed:
            return self._refuse_or_report(thread_id, application.id, current, decision)

        try:
            values = await self._graph.ainvoke(
                Command(resume=outcome.resume_payload()),
                config,
                durability=CHECKPOINT_DURABILITY,
            )
        except BaseException:
            # Hand the gate back. The claim is only meaningful while someone
            # is acting on it, and an application stranded in `resuming`
            # would be invisible to every later attempt to decide it.
            self._deps.db.release_resume_claim(application.id)
            raise
        return self._from_values(thread_id, application.queue_id, values)

    def _require_matching_approval(
        self,
        thread_id: str,
        application: ApplicationRecord,
        decision: ApprovalRequest,
    ) -> ApprovalRecord:
        """The recorded decision, if this request is the same one.

        A thread with nothing left to resume and no recorded decision was
        never at the gate. One with a *different* recorded decision is a
        conflict rather than a replay: a second reviewer's answer does not
        become the first one's by arriving after it.
        """
        existing = self._service.recorded(application.id)
        if existing is None:
            raise ThreadNotAwaitingApproval(thread_id, application.id)
        if not decision.matches(existing):
            raise ApprovalConflict(
                application.id, existing, decision.differing_field(existing)
            )
        return existing

    def _refuse_or_report(
        self,
        thread_id: str,
        application_id: int,
        current: ApplicationRecord | None,
        decision: ApprovalRequest,
    ) -> RunResult:
        """What a caller that lost the claim is told.

        If the winner has already finished, the loser reads that outcome:
        this is the ordinary shape of a retried request, and the honest
        answer is the submission it caused. If the winner is still in
        flight, there is no outcome to report yet and waiting is not
        possible across processes, so the loser is refused rather than
        allowed anywhere near the submitter.
        """
        if current is not None and current.status in TERMINAL_APPLICATION_STATUSES:
            existing = self._require_matching_approval(thread_id, current, decision)
            recorded = _recorded_result(
                thread_id,
                self._deps.db.get_queue_item(current.queue_id),
                current,
                existing,
            )
            if recorded is not None:
                return recorded
        raise ResumeInProgress(thread_id, application_id)

    def _from_state(self, thread_id: str, queue_id: int, state: Any) -> RunResult:
        payload = dict(state.interrupts[0].value) if state.interrupts else None
        return self._result(thread_id, queue_id, dict(state.values), payload)

    def _from_values(
        self, thread_id: str, queue_id: int, values: Mapping[str, Any]
    ) -> RunResult:
        interrupts = values.get("__interrupt__") or ()
        payload = dict(interrupts[0].value) if interrupts else None
        return self._result(thread_id, queue_id, values, payload)

    def _result(
        self,
        thread_id: str,
        queue_id: int,
        values: Mapping[str, Any],
        interrupt_payload: dict[str, Any] | None,
    ) -> RunResult:
        if interrupt_payload is not None:
            status = RunStatus.AWAITING_APPROVAL
            reason = "awaiting a human decision"
        else:
            raw = values.get("outcome")
            status = RunStatus(raw) if raw else RunStatus.FAILED
            reason = str(
                values.get("reason", "" if raw else "the run produced no outcome")
            )
        # Who decided, and what they said about it, are read back from the
        # approvals table rather than from the graph's state, because that
        # is where they are kept: the checkpoint carries only the decision.
        approval = self._approval_for(values.get("application_id"))
        return RunResult(
            thread_id=thread_id,
            queue_id=queue_id,
            application_id=values.get("application_id"),
            status=status,
            reason=reason,
            detail=values.get("detail") or None,
            interrupt=interrupt_payload,
            ats=values.get("ats"),
            trigger_tier=values.get("trigger_tier"),
            model_cost=Decimal(str(values.get("model_cost", "0"))),
            blocking_reasons=tuple(values.get("blocking_reasons", ())),
            visited=tuple(values.get("visited", ())),
            gaps=tuple(dict(entry) for entry in values.get("gap_summary", ())),
            decision=values.get("decision"),
            actor=approval.actor if approval is not None else None,
            note=approval.note if approval is not None else None,
            decided_at=values.get("decided_at")
            or (approval.timestamp.isoformat() if approval is not None else None),
        )

    def _approval_for(self, application_id: Any) -> ApprovalRecord | None:
        if not isinstance(application_id, int):
            return None
        return self._service.recorded(application_id)


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}
