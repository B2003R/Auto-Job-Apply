"""The FastAPI control plane and the single worker that owns the browser.

One process, one browser, one worker. Everything in this module follows
from that:

* **The lifespan owns the worker.** `create_app` builds nothing at import
  time and nothing per request; the worker is constructed, started, and
  stopped exactly once, by the lifespan. A second `start()` is refused
  rather than tolerated, because two Playwright contexts on one Chrome
  profile is precisely the collision the profile lock exists to prevent,
  and discovering it through a lock error is worse than discovering it
  here.
* **The actor is the authenticated caller, never the request body.** An
  approval is the record of a human authorising a submission made in their
  name. A body-supplied actor is a claim the server cannot check, so the
  models forbid the field outright: a request that carries one is refused
  rather than quietly attributed to somebody else.
* **Reachable implies authenticated.** With no token configured the API
  serves loopback callers only — checked per request, not merely assumed
  from the bind address — and `insecure_binding_reason` refuses a wider
  bind at startup. A misconfigured `API_HOST` is otherwise an anonymous
  submit button on the network.
* **Every refusal has a status that says what to do next.** Validation is
  422, an unknown record is 404, a decision that contradicts a recorded one
  is 409, and a thread another worker is running is 423 with `Retry-After`
  — distinct from 409 because the caller's request was not wrong, it was
  early.

Nothing here submits anything by itself. The graph stops at the approval
gate, and `POST /applications/{id}/approve` is the only route that can
release it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol, Sequence

from fastapi import Depends, FastAPI, Path as PathParam, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent.approval import (
    ApiApprovalGate,
    ApplicationNotAwaitingApproval,
    ApplicationThreadMismatch,
    ApprovalConflict,
    ApprovalError,
    ApprovalRequest,
    InvalidApprovalRequest,
    UnknownApplicationError,
    UnknownThreadError,
)
from app.agent.browser_actions import (
    PlaywrightFieldWriter,
    PlaywrightPageGuard,
    PlaywrightSubmitter,
)
from app.agent.gap_filler import GapFiller
from app.agent.graph import (
    ApplicationRunner,
    ExecutionInProgress,
    GraphDependencies,
    RunResult,
    RunStatus,
    ThreadNotAwaitingApproval,
    UnknownQueueItem,
    sqlite_checkpointer,
    thread_id_for,
)
from app.agent.jobright_trigger import JobrightTrigger
from app.agent.model_router import ModelRouter
from app.agent.rate_limiter import RateLimiter
from app.boards.base import UntrustedListingUrlError, require_trusted_host
from app.config import Settings
from app.storage.db import Database
from app.storage.logger import ApplicationLogger
from app.storage.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApprovalDecision,
    ApprovalRecord,
    Board,
    QueueItem,
    QueueState,
)

logger = logging.getLogger(__name__)

#: The longest listing URL the queue accepts. Generous for a real posting
#: and small enough that the queue cannot be filled with megabyte strings.
MAX_LISTING_URL_CHARS = 2_048

#: What a client is told to wait when a thread is busy and its holder's
#: lease expiry cannot be read.
DEFAULT_RETRY_AFTER_S = 5

#: Said in place of an unhandled exception. Deliberately incurious: the
#: detail belongs in the log, where it is not attacker-readable.
INTERNAL_ERROR_MESSAGE = (
    "the control plane could not complete this request. The reason has been "
    "logged."
)


class WorkerNotReady(RuntimeError):
    """Raised when a route needs the worker before it has finished starting.

    Mapped to 503 rather than 500: nothing is wrong, the browser is simply
    not up yet, and the caller should retry.
    """


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiIdentity:
    """Who the server believes is calling, and how it decided.

    `actor` is what reaches the approvals table. It is derived here and
    nowhere else, so no request body can influence it.
    """

    actor: str
    scheme: str


def token_fingerprint(token: str) -> str:
    """A stable name for a credential that is not the credential.

    Written into the approvals table when no `API_ACTOR` is configured, so
    two different tokens are distinguishable in the audit record while
    neither is recoverable from it.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _wire_bytes(value: str) -> bytes:
    """The bytes a header value arrived as.

    Starlette decodes header bytes with latin-1, which is a total mapping,
    so encoding back with latin-1 recovers exactly what was sent — including
    byte sequences that are not valid UTF-8 and are not valid anything else.
    The fallback is for a caller passing a genuine Python string that never
    came off a socket, which latin-1 cannot represent.
    """
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return value.encode("utf-8")


def _tokens_match(presented: str, configured: str) -> bool:
    """Constant-time comparison that cannot be crashed by its input.

    `hmac.compare_digest` raises `TypeError` when handed a `str` holding
    anything outside ASCII, and the presented half comes straight off the
    wire. That made one accented byte an unhandled exception any anonymous
    caller could trigger at will; compared as bytes it is what it actually
    is, which is the wrong token.

    The configured side is encoded as UTF-8 because that is how a token
    written into `.env` reaches this process and how every client sends
    one, so an operator whose token is not pure ASCII gets a token that
    works rather than one that never matches.
    """
    return hmac.compare_digest(_wire_bytes(presented), configured.encode("utf-8"))


def is_loopback(host: str) -> bool:
    """Whether `host` is a loopback *address*.

    Deliberately not a name comparison. A hostname can resolve anywhere, and
    the whole point of this check is that it cannot be talked out of.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def insecure_binding_reason(settings: Settings) -> str | None:
    """Why this configuration must not be served, or `None` if it may be.

    Consulted by `serve()` before a socket is opened. Binding beyond
    loopback without a token would put an unauthenticated submit button on
    the network; the per-request loopback check would still refuse those
    callers, but a configuration that depends on a second line of defence
    is one that will eventually be served without it.
    """
    if settings.api_token.get_secret_value():
        return None
    if is_loopback(settings.api_host):
        return None
    return (
        f"API_HOST={settings.api_host!r} is not a loopback address and no "
        "API_TOKEN is configured. This API can submit job applications in "
        "your name; set API_TOKEN before binding it anywhere reachable, or "
        "leave API_HOST at 127.0.0.1."
    )


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ApiError(Exception):
    """One refused request, with the status and machine-readable kind."""

    def __init__(
        self,
        status_code: int,
        kind: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.kind = kind
        self.message = message
        self.headers = headers or {}
        super().__init__(message)


#: Domain exception to (status, kind), most specific first. `ResumeInProgress`
#: subclasses both `ExecutionInProgress` and `ApprovalError`, so order is what
#: decides whether a busy thread reads as a conflict (it is not one) or as a
#: request that arrived early (it is).
_ERROR_MAP: tuple[tuple[type[BaseException], int, str], ...] = (
    (ExecutionInProgress, 423, "execution_in_progress"),
    (UnknownApplicationError, 404, "unknown_application"),
    (UnknownThreadError, 404, "unknown_thread"),
    (UnknownQueueItem, 404, "unknown_queue_item"),
    (ApprovalConflict, 409, "approval_conflict"),
    (ApplicationNotAwaitingApproval, 409, "not_awaiting_approval"),
    (ThreadNotAwaitingApproval, 409, "not_awaiting_approval"),
    (ApplicationThreadMismatch, 409, "thread_mismatch"),
    (InvalidApprovalRequest, 422, "invalid_request"),
    (UntrustedListingUrlError, 422, "untrusted_listing_url"),
    (WorkerNotReady, 503, "worker_unavailable"),
    # Anything else from the approval layer is still a refusal, not a bug.
    (ApprovalError, 409, "approval_refused"),
)


def _mapped(error: BaseException) -> tuple[int, str]:
    for error_type, status, kind in _ERROR_MAP:
        if isinstance(error, error_type):
            return status, kind
    return 500, "internal_error"  # pragma: no cover - handlers are registered per type


def _retry_after(error: BaseException) -> dict[str, str]:
    """How long to wait for a busy thread, from its holder's lease.

    A number the client can act on beats a bare 423: the lease says when
    the current holder's claim lapses, which is the earliest moment a retry
    can succeed.
    """
    lease = getattr(error, "lease", None)
    seconds = DEFAULT_RETRY_AFTER_S
    expires = getattr(lease, "expires_at", None)
    if isinstance(expires, datetime):
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        seconds = max(1, ceil(remaining))
    return {"Retry-After": str(seconds)}


def _error_response(
    status_code: int, kind: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"kind": kind, "message": message}},
        headers=headers,
    )


# --------------------------------------------------------------------------
# Request and response models
# --------------------------------------------------------------------------


class QueueRequest(BaseModel):
    """A listing to apply to.

    `extra="forbid"` throughout: a field this server does not understand is
    a client that believes it is controlling something it is not.
    """

    model_config = ConfigDict(extra="forbid")

    listing_url: str = Field(min_length=1, max_length=MAX_LISTING_URL_CHARS)
    board: Board


class QueueAccepted(BaseModel):
    queue_id: int
    thread_id: str
    listing_url: str
    board: Board
    state: QueueState


class DecisionRequest(BaseModel):
    """The reviewer's optional note, and nothing else.

    There is deliberately no `actor` and no `decision` field. The decision
    is the route, and the actor is the authenticated identity; accepting
    either from the body would let a caller attribute a submission to
    somebody else, or approve through the reject route.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = None


class ApplicationView(BaseModel):
    id: int
    thread_id: str
    status: ApplicationStatus
    ats: str | None
    trigger_tier: int | None
    model_cost: float | None
    screenshot_path: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, record: ApplicationRecord) -> "ApplicationView":
        return cls(
            id=record.id,
            thread_id=record.thread_id,
            status=record.status,
            ats=record.ats,
            trigger_tier=record.trigger_tier,
            model_cost=record.model_cost,
            screenshot_path=record.screenshot_path,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class DecisionRecordView(BaseModel):
    decision: ApprovalDecision
    actor: str
    note: str | None
    decided_at: datetime

    @classmethod
    def of(cls, record: ApprovalRecord) -> "DecisionRecordView":
        return cls(
            decision=record.decision,
            actor=record.actor,
            note=record.note,
            decided_at=record.timestamp,
        )


class RunView(BaseModel):
    queue_id: int
    thread_id: str
    listing_url: str
    board: Board
    state: QueueState
    error_reason: str | None
    application: ApplicationView | None
    decision: DecisionRecordView | None
    #: True only when a decision would actually do something right now: a
    #: thread another worker is running is not offered to a reviewer.
    awaiting_decision: bool
    executing: bool
    lease_expires_at: datetime | None
    #: The gate payload, already redacted by the graph unless
    #: `LOG_FIELD_VALUES` is enabled.
    interrupt: dict[str, Any] | None


class DecisionView(BaseModel):
    #: Optional because `RunResult`'s is. Reporting `0` for an absent id
    #: would be a value a client could read as an application.
    application_id: int | None
    thread_id: str
    queue_id: int
    decision: str | None
    actor: str | None
    note: str | None
    decided_at: str | None
    status: RunStatus
    reason: str
    detail: str | None
    submitted: bool

    @classmethod
    def of(cls, result: RunResult) -> "DecisionView":
        return cls(
            application_id=result.application_id,
            thread_id=result.thread_id,
            queue_id=result.queue_id,
            decision=result.decision,
            actor=result.actor,
            note=result.note,
            decided_at=result.decided_at,
            status=result.status,
            reason=result.reason,
            detail=result.detail,
            submitted=result.submitted,
        )


class HealthView(BaseModel):
    status: str
    worker: str
    auth: str


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------


class BrowserSessionLike(Protocol):
    async def start(self) -> Any: ...

    async def close(self) -> None: ...


SessionFactory = Callable[[Settings], BrowserSessionLike]
DependenciesFactory = Callable[[Settings, Database, Any], GraphDependencies]


class ContextPageBroker:
    """One tab per thread, opened from the worker's one browser context."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._pages: dict[str, Any] = {}

    async def open(self, thread_id: str) -> Any:
        page = self._pages.get(thread_id)
        if page is None:
            page = await self._context.new_page()
            self._pages[thread_id] = page
        return page

    async def get(self, thread_id: str) -> Any | None:
        return self._pages.get(thread_id)

    async def release(self, thread_id: str) -> None:
        page = self._pages.pop(thread_id, None)
        if page is None:
            return
        await page.close()


#: Everything a caller's name is allowed to contribute to a filename. The
#: names are built from thread ids, which come out of the database, and a
#: name is never allowed to decide *where* an artifact is written.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class ArtifactScreenshotter:
    """Writes diagnostic screenshots under the configured artifacts path.

    Every filename is unique. A screenshot is the only evidence an operator
    has for a submission nobody could confirm, and while the caller's name
    said which application and which outcome, two applications reaching the
    same outcome — or one application photographed on two attempts — wrote
    to the same path, so the queue row pointed at a picture of a different
    page than the one it was about.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def capture(self, page: Any, name: str) -> str | None:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._unique_path(name)
        await page.screenshot(path=str(target))
        return str(target)

    def _unique_path(self, name: str) -> Path:
        safe = _UNSAFE_IN_FILENAME.sub("-", name).strip("-.") or "screenshot"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        candidate = self._directory / f"{safe}-{stamp}.png"
        # Two captures inside one microsecond is not a thing that happens,
        # and overwriting evidence because it did is not a thing to allow.
        attempt = 1
        while candidate.exists():
            candidate = self._directory / f"{safe}-{stamp}-{attempt}.png"
            attempt += 1
        return candidate


def build_dependencies(
    settings: Settings, db: Database, context: Any
) -> GraphDependencies:
    """Wire the production graph dependencies around one browser context.

    The trigger and the writer share one `FormScanner` deliberately. A
    scanner holds a random per-instance key that its stable keys are derived
    with, so a writer given a scanner of its own would re-derive a different
    key for the same control and refuse every write. Sharing this one is
    what turns the writer's provenance check from a guaranteed refusal into
    the guarantee it is meant to be: the control typed into is the control
    the answer is recorded against.
    """
    screenshots = ArtifactScreenshotter(settings.artifacts_path)
    trigger = JobrightTrigger.from_settings(settings)
    return GraphDependencies(
        db=db,
        settings=settings,
        logger=ApplicationLogger(db, settings),
        rate_limiter=RateLimiter(db, settings),
        pages=ContextPageBroker(context),
        trigger=trigger,
        gap_filler=GapFiller.from_settings(settings, router=ModelRouter(settings)),
        writer=PlaywrightFieldWriter(scanner=trigger.scanner),
        guard=PlaywrightPageGuard(),
        submitter=PlaywrightSubmitter(screenshots=screenshots),
        screenshots=screenshots,
    )


class ApplicationWorker:
    """Owns the browser, the checkpointer, and the queue-draining loop.

    Exactly one of these exists per process. `start()` refuses a second
    call on the same instance, and the lifespan only ever builds one, so
    there is no path on which two Playwright contexts open the same Chrome
    profile.

    Every browser-touching piece is injected. The tests replace the session
    and the dependencies with fakes and keep the real database, approval
    service, rate limiter, gap filler, and graph, so what they prove about
    claiming and containment is true of this class rather than of a mock.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        session_factory: SessionFactory | None = None,
        dependencies_factory: DependenciesFactory | None = None,
        checkpointer_path: Path | None = None,
        poll_interval_s: float | None = None,
        run_loop: bool = True,
    ) -> None:
        self._settings = settings
        self._db = db if db is not None else Database(settings)
        self._session_factory = session_factory or _default_session_factory
        self._dependencies_factory = dependencies_factory or build_dependencies
        self._checkpointer_path = checkpointer_path or (
            settings.sqlite_path.parent / "checkpoints.sqlite"
        )
        self._poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else settings.worker_poll_interval_s
        )
        self._run_loop = run_loop
        self._session: BrowserSessionLike | None = None
        self._checkpointer_cm: Any | None = None
        self._runner: ApplicationRunner | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._started = False
        self._stopped = False
        self.starts = 0
        self.stops = 0

    @property
    def db(self) -> Database:
        return self._db

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def runner(self) -> ApplicationRunner:
        if self._runner is None:
            raise WorkerNotReady(
                "the worker has not finished starting, so no application can "
                "be run or resumed yet"
            )
        return self._runner

    async def start(self) -> None:
        """Bring up storage, the checkpointer, the browser, and the loop.

        Refuses a second call rather than returning the existing context:
        an idempotent `start()` would hide a caller that believes it owns a
        worker it does not, and the failure that eventually surfaces is a
        profile lock error a long way from the mistake.
        """
        if self._started:
            raise RuntimeError(
                "this worker has already been started; one process owns one "
                "browser, so build a second worker only for a second profile"
            )
        self._started = True
        self.starts += 1

        self._db.initialize()
        self._recover_abandoned()
        self._checkpointer_cm = sqlite_checkpointer(self._checkpointer_path)
        checkpointer = await self._checkpointer_cm.__aenter__()
        self._session = self._session_factory(self._settings)
        context = await self._session.start()
        dependencies = self._dependencies_factory(self._settings, self._db, context)
        self._runner = ApplicationRunner(dependencies, checkpointer)
        if self._run_loop:
            self._task = asyncio.create_task(self._loop(), name="job-apply-worker")

    async def stop(self) -> None:
        """Stop the loop, close the browser, and close the checkpointer.

        Safe to call twice — the lifespan calls it on the way out and again
        after a failed start — and every step is attempted even if an
        earlier one failed, so a browser is never left running because a
        task refused to cancel.
        """
        if self._stopped or not self._started:
            return
        self._stopped = True
        self.stops += 1
        failures: list[BaseException] = []

        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - collected, never swallowed
                failures.append(exc)

        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001 - collected below
                failures.append(exc)

        checkpointer_cm, self._checkpointer_cm = self._checkpointer_cm, None
        if checkpointer_cm is not None:
            try:
                await checkpointer_cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - collected below
                failures.append(exc)

        self._runner = None
        if failures:
            raise failures[0]

    def _recover_abandoned(self) -> None:
        """Return listings a dead worker was holding to the pending queue.

        A claim moves a row to `running`, and `claim_next_pending` only
        ever claims `pending`, so a process killed mid-application leaves a
        listing no worker will ever look at again — with no error, no
        failure, and nothing for an operator to notice. The application row
        and the execution lease both recover on their own; this is the
        piece that does not.

        A live lease is the one thing that means "somebody is still working
        on this", so a leased row is left alone. The window between a claim
        and its lease is the case this can get wrong, and it is safe to
        get wrong: the loser of that race finds the thread leased and
        returns `IN_PROGRESS` without touching the browser, so the worst
        outcome is a wasted claim rather than a second application.

        An absent lease is not on its own proof of abandonment, though. The
        approval gate releases its lease *on purpose* the moment an
        application is durably parked — that is precisely what lets a
        decision arrive from a different process later — so a `running`
        row with no lease and an application sitting at
        `AWAITING_APPROVAL` is healthy, not orphaned. Stamping
        `worker_abandoned` on it would be a false alarm on every single
        restart while anything is waiting for a human, and would mislead an
        operator reading `error_reason` into thinking a decision they never
        made was lost.
        """
        now = datetime.now(timezone.utc)
        for item in self._db.list_queue_items(states=[QueueState.RUNNING]):
            thread_id = thread_id_for(item.id)
            lease = self._db.get_lease(thread_id)
            if lease is not None and lease.expires_at > now:
                continue
            if lease is None:
                application = self._db.get_application_by_thread(thread_id)
                if (
                    application is not None
                    and application.status is ApplicationStatus.AWAITING_APPROVAL
                ):
                    continue
            if self._db.requeue_running(item.id, "worker_abandoned"):
                logger.warning(
                    "queue item %s was left running by a worker that is gone; "
                    "it has been returned to the queue",
                    item.id,
                )

    def wake(self) -> None:
        """Tell the loop there is new work, instead of waiting out the poll."""
        self._wake.set()

    async def drain(self) -> list[RunResult]:
        """Claim and run every pending queue item, one at a time.

        Sequential on purpose. The worker owns one browser and the rate
        caps are per board per day; running two applications at once would
        buy nothing and would put two staged forms in front of a reviewer
        who can only look at one.
        """
        results: list[RunResult] = []
        while True:
            item = self._db.claim_next_pending()
            if item is None:
                return results
            results.append(await self._run_one(item))

    async def _run_one(self, item: QueueItem) -> RunResult:
        """Run one claimed item, containing anything the runner does not.

        The graph already turns a captcha, a login wall, an unknown ATS, a
        reached cap, and an unexpected node error into a recorded outcome.
        Reaching the handler below therefore means the runner itself fell
        over, which is exactly the case where continuing to the next
        listing matters most: one broken record must not end the batch.
        """
        try:
            return await self.runner.run_application(item.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad item ends one item
            logger.exception("queue item %s could not be run", item.id)
            self._db.update_queue_state(item.id, QueueState.FAILED, "worker_error")
            return RunResult(
                thread_id=thread_id_for(item.id),
                queue_id=item.id,
                application_id=None,
                status=RunStatus.FAILED,
                reason="worker_error",
                detail=str(exc),
            )

    async def _loop(self) -> None:
        while True:
            # Cleared before draining, not after: a wake that arrives while
            # the worker is busy is about work it may not have seen, and
            # clearing afterwards would drop it.
            self._wake.clear()
            try:
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop outlives its items
                logger.exception("the worker loop could not drain the queue")
            try:
                await asyncio.wait_for(self._wake.wait(), self._poll_interval_s)
            except (asyncio.TimeoutError, TimeoutError):
                pass


def _default_session_factory(settings: Settings) -> BrowserSessionLike:
    """The real browser session, imported only when one is actually needed."""
    from app.agent.browser_session import BrowserSession

    return BrowserSession(settings)


WorkerLike = ApplicationWorker
WorkerFactory = Callable[[Settings], ApplicationWorker]


def _default_worker_factory(settings: Settings) -> ApplicationWorker:
    return ApplicationWorker(settings)


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------


def create_app(
    settings: Settings, worker_factory: WorkerFactory | None = None
) -> FastAPI:
    """Build the control plane around one lifespan-owned worker."""
    factory = worker_factory or _default_worker_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker = factory(settings)
        app.state.worker = worker
        try:
            await worker.start()
        except BaseException:
            # A half-started worker may already hold a profile lock and a
            # browser. Tearing down before re-raising is what keeps a
            # failed startup from blocking the next one.
            await worker.stop()
            raise
        try:
            yield
        finally:
            await worker.stop()

    app = FastAPI(
        title="Job Apply Agent control plane",
        version="0.1.0",
        lifespan=lifespan,
        # FastAPI's own documentation routes are plain Starlette routes, so
        # no dependency of ours runs for them and they would describe every
        # route and body shape to anyone who could reach the port. They are
        # re-registered below, authenticated, or not at all.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.worker = None
    _register_error_handlers(app)
    _register_docs(app, settings)
    _register_routes(app, settings)
    return app


def _register_docs(app: FastAPI, settings: Settings) -> None:
    """Serve the schema and its two viewers, behind the loopback check.

    Only when no token is configured. Swagger UI and ReDoc fetch the schema
    from the browser, which has no way to present a bearer token, so on a
    token-protected control plane an authenticated docs page could not load
    the very thing it exists to render. Serving the schema unauthenticated
    to make that work is the trade this refuses: the alternative to a
    broken docs page is no docs page, and `README.md` says so.
    """
    if settings.api_token.get_secret_value():
        return

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema(identity: ApiIdentity = Identity) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui(identity: ApiIdentity = Identity) -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title)

    @app.get("/redoc", include_in_schema=False)
    async def redoc(identity: ApiIdentity = Identity) -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title=app.title)


def _register_error_handlers(app: FastAPI) -> None:
    async def domain_error(request: Request, exc: Exception) -> JSONResponse:
        status_code, kind = _mapped(exc)
        headers = _retry_after(exc) if status_code == 423 else None
        return _error_response(status_code, kind, str(exc), headers)

    for error_type in (
        ApprovalError,
        ExecutionInProgress,
        UnknownQueueItem,
        UntrustedListingUrlError,
        WorkerNotReady,
    ):
        app.add_exception_handler(error_type, domain_error)

    async def api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return _error_response(exc.status_code, exc.kind, exc.message, exc.headers)

    app.add_exception_handler(ApiError, api_error)

    async def validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return _error_response(
            422,
            "validation_error",
            "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            or "the request body could not be validated",
        )

    app.add_exception_handler(RequestValidationError, validation_error)

    async def http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return _error_response(
            exc.status_code,
            f"http_{exc.status_code}",
            str(exc.detail),
            dict(exc.headers or {}),
        )

    app.add_exception_handler(StarletteHTTPException, http_error)

    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """The last resort: an answer that says nothing about itself.

        Everything reachable is mapped above, so arriving here means a bug.
        The caller gets the same envelope as every other refusal and not one
        word about the cause — exception messages carry connection strings,
        file paths, and whatever a third-party library felt like including.
        The operator gets the whole thing, with a traceback, in the log.
        """
        logger.exception(
            "unhandled error serving %s %s", request.method, request.url.path
        )
        return _error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE)

    app.add_exception_handler(Exception, unexpected_error)


def _worker_of(request: Request) -> ApplicationWorker:
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        raise WorkerNotReady("the control plane has no worker")
    return worker


def _identity_of(request: Request) -> ApiIdentity:
    """Authenticate one request, and name who made it.

    Two schemes, chosen by configuration rather than by the caller, so a
    client cannot pick the weaker one:

    * A configured `API_TOKEN` means every request must present it as a
      bearer token, from anywhere.
    * No token means loopback callers only. A token presented to a server
      that has none is refused rather than ignored, because a client that
      believes it authenticated and did not is worse off than one that is
      told.
    """
    settings: Settings = request.app.state.settings
    configured = settings.api_token.get_secret_value()
    header = request.headers.get("authorization", "").strip()

    if configured:
        scheme, _, presented = header.partition(" ")
        if not header:
            raise ApiError(
                401,
                "missing_credentials",
                "this control plane requires a bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scheme.lower() != "bearer" or not _tokens_match(presented.strip(), configured):
            raise ApiError(
                401,
                "invalid_credentials",
                "the presented credentials were not accepted",
                headers={"WWW-Authenticate": "Bearer"},
            )
        actor = settings.api_actor.strip() or f"api-token:{token_fingerprint(configured)}"
        return ApiIdentity(actor=actor, scheme="token")

    if header:
        raise ApiError(
            401,
            "no_token_configured",
            "credentials were presented but this control plane has no "
            "API_TOKEN configured, so nothing could have been verified",
            headers={"WWW-Authenticate": "Bearer"},
        )

    host = request.client.host if request.client is not None else None
    if host is None or not is_loopback(host):
        raise ApiError(
            403,
            "not_loopback",
            "this control plane has no API_TOKEN configured and therefore "
            "serves loopback callers only",
        )
    return ApiIdentity(actor=f"loopback:{host}", scheme="loopback")


Identity = Depends(_identity_of)
Worker = Depends(_worker_of)


def _register_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/health", response_model=HealthView)
    async def health(
        identity: ApiIdentity = Identity, worker: ApplicationWorker = Worker
    ) -> HealthView:
        try:
            worker.runner
            state = "running"
        except WorkerNotReady:
            state = "starting"
        return HealthView(status="ok", worker=state, auth=identity.scheme)

    @app.post("/queue", response_model=QueueAccepted, status_code=201)
    async def queue(
        body: QueueRequest,
        identity: ApiIdentity = Identity,
        worker: ApplicationWorker = Worker,
    ) -> QueueAccepted:
        # Validated before anything is written: an untrusted URL that
        # reached the queue would be refused later by the adapter, but only
        # after occupying a queue row and a worker's attention.
        require_trusted_host(body.listing_url, body.board)
        queue_id = worker.db.enqueue_job(body.listing_url, body.board)
        worker.wake()
        return QueueAccepted(
            queue_id=queue_id,
            thread_id=thread_id_for(queue_id),
            listing_url=body.listing_url,
            board=body.board,
            state=QueueState.PENDING,
        )

    @app.get("/runs/{queue_id}", response_model=RunView)
    async def run_status(
        queue_id: int = PathParam(ge=1),
        identity: ApiIdentity = Identity,
        worker: ApplicationWorker = Worker,
    ) -> RunView:
        item = worker.db.get_queue_item(queue_id)
        if item is None:
            raise UnknownQueueItem(queue_id)
        return await _run_view(worker, item)

    @app.get("/applications", response_model=list[RunView])
    async def applications(
        status: ApplicationStatus | None = None,
        identity: ApiIdentity = Identity,
        worker: ApplicationWorker = Worker,
    ) -> list[RunView]:
        records = worker.db.list_applications(
            statuses=[status] if status is not None else None
        )
        views: list[RunView] = []
        for record in records:
            item = worker.db.get_queue_item(record.queue_id)
            if item is None:  # pragma: no cover - a foreign key guarantees one
                continue
            views.append(await _run_view(worker, item, record))
        return views

    @app.post("/applications/{application_id}/approve", response_model=DecisionView)
    async def approve(
        body: DecisionRequest,
        application_id: int = PathParam(ge=1),
        identity: ApiIdentity = Identity,
        worker: ApplicationWorker = Worker,
    ) -> DecisionView:
        return await _decide(
            worker, identity, application_id, ApprovalDecision.APPROVED, body.note
        )

    @app.post("/applications/{application_id}/reject", response_model=DecisionView)
    async def reject(
        body: DecisionRequest,
        application_id: int = PathParam(ge=1),
        identity: ApiIdentity = Identity,
        worker: ApplicationWorker = Worker,
    ) -> DecisionView:
        return await _decide(
            worker, identity, application_id, ApprovalDecision.REJECTED, body.note
        )


async def _decide(
    worker: ApplicationWorker,
    identity: ApiIdentity,
    application_id: int,
    decision: ApprovalDecision,
    note: str | None,
) -> DecisionView:
    """Record one decision and resume its thread.

    The request is built here, from the route and the authenticated
    identity, so the only thing the body contributes is the note. The gate
    resolves the graph thread from the stored application row, which is why
    no request can aim a decision at another application's thread.
    """
    gate = ApiApprovalGate(worker.runner.approvals, worker.runner)
    request = ApprovalRequest(
        application_id=application_id,
        decision=decision,
        actor=identity.actor,
        note=note,
    )
    result: RunResult = await gate.decide(request)
    return DecisionView.of(result)


async def _run_view(
    worker: ApplicationWorker,
    item: QueueItem,
    application: ApplicationRecord | None = None,
) -> RunView:
    thread_id = thread_id_for(item.id)
    record = (
        application
        if application is not None
        else worker.db.get_application_by_thread(thread_id)
    )
    status = await worker.runner.thread_status(thread_id)
    approval = worker.db.get_approval(record.id) if record is not None else None
    return RunView(
        queue_id=item.id,
        thread_id=thread_id,
        listing_url=item.listing_url,
        board=item.board,
        state=item.state,
        error_reason=item.error_reason,
        application=ApplicationView.of(record) if record is not None else None,
        decision=DecisionRecordView.of(approval) if approval is not None else None,
        awaiting_decision=status.awaiting_decision,
        executing=status.executing,
        lease_expires_at=status.lease.expires_at if status.lease else None,
        interrupt=status.interrupt if status.awaiting_decision else None,
    )


def serve(settings: Settings | None = None, argv: Sequence[str] | None = None) -> int:
    """Run the control plane with uvicorn, refusing an unsafe binding."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the job application control plane"
    )
    parser.add_argument("--host", default=None, help="override API_HOST")
    parser.add_argument("--port", type=int, default=None, help="override API_PORT")
    args = parser.parse_args(argv)

    resolved = settings if settings is not None else Settings()
    if args.host is not None:
        resolved = resolved.model_copy(update={"api_host": args.host})
    if args.port is not None:
        resolved = resolved.model_copy(update={"api_port": args.port})

    reason = insecure_binding_reason(resolved)
    if reason is not None:
        print(f"refusing to start: {reason}")
        return 2

    import uvicorn

    uvicorn.run(
        create_app(resolved), host=resolved.api_host, port=resolved.api_port
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(serve())
