"""Shared fakes for the application-graph tests.

Everything the graph touches that would open a browser, a socket, or a
model connection is faked here; the storage, rate, gap-filling,
ATS-detection, and approval layers stay real, so the graph tests exercise
shipped code rather than mocks.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import pytest

from app.agent.approval import ApprovalService
from app.agent.errors import SubmitNotAuthorized
from app.agent.form_scanner import FormField, FormSnapshot, SettleResult
from app.agent.gap_filler import AnswerBook, GapFiller
from app.agent.graph import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE_TTL,
    ApplicationRunner,
    GraphDependencies,
    SubmitAuthorization,
    SubmitOutcome,
    SubmitPermit,
    sqlite_checkpointer,
)
from app.agent.jobright_trigger import TierAttempt, TriggerResult, TriggerTier
from app.agent.model_router import ModelAnswer, ModelTier, TokenUsage
from app.agent.rate_limiter import RateLimiter
from app.boards.base import ApplyResult, ApplyStatus, ListingResult
from app.config import Settings
from app.storage.db import Database
from app.storage.logger import ApplicationLogger
from app.storage.models import Board

GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/1"
UNKNOWN_ATS_URL = "https://careers.example.com/apply/1"
LISTING_URL = "https://www.linkedin.com/jobs/view/1/"

GREENHOUSE_HTML = """
<html><head><meta name="generator" content="Greenhouse"></head>
<body><form id="application_form" action="https://boards.greenhouse.io/acme/apply">
</form></body></html>
"""

PLAIN_HTML = "<html><body><form><input name='q'></form></body></html>"


class Crash(BaseException):
    """Stands in for the process dying: never caught by node containment."""


class MovableClock:
    """A clock a test moves by hand, for anything that expires."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_field(
    key: str,
    *,
    label: str = "",
    name: str = "",
    field_type: str = "text",
    tag: str = "input",
    required: bool = False,
    filled: bool = False,
    free_text: bool = False,
    value: str | None = None,
) -> FormField:
    return FormField(
        key=key,
        frame_url=GREENHOUSE_URL,
        form="form#application_form",
        control_id=key,
        name=name or key,
        field_type=field_type,
        label=label,
        tag=tag,
        required=required,
        disabled=False,
        visible=True,
        filled=filled,
        free_text=free_text,
        value_digest=f"digest:{key}:{'filled' if filled else 'empty'}",
        value=value,
    )


def snapshot(*fields: FormField, skipped: Sequence[tuple[str, str]] = ()) -> FormSnapshot:
    from app.agent.form_scanner import FrameSkip

    return FormSnapshot(
        fields=tuple(fields),
        skipped_frames=tuple(FrameSkip(url, reason) for url, reason in skipped),
        scanner_id="fake-scanner",
    )


class FakePage:
    """A page double with only what the graph nodes actually read."""

    def __init__(self, url: str = GREENHOUSE_URL, html: str = GREENHOUSE_HTML) -> None:
        self.url = url
        self.html = html
        self.closed = False

    async def content(self) -> str:
        return self.html


class FakePageBroker:
    """Holds one live page per thread, the way a real worker holds a tab."""

    def __init__(self, page: FakePage | None = None) -> None:
        self.template = page or FakePage()
        self.pages: dict[str, FakePage] = {}
        self.released: list[str] = []

    async def open(self, thread_id: str) -> FakePage:
        self.pages.setdefault(thread_id, self.template)
        return self.pages[thread_id]

    async def get(self, thread_id: str) -> FakePage | None:
        return self.pages.get(thread_id)

    async def release(self, thread_id: str) -> None:
        self.released.append(thread_id)
        page = self.pages.pop(thread_id, None)
        if page is not None:
            page.closed = True


class FakeAdapter:
    def __init__(
        self,
        board: Board = Board.LINKEDIN,
        *,
        apply_result: ApplyResult | None = None,
        open_error: BaseException | None = None,
        start_error: BaseException | None = None,
    ) -> None:
        self.board = board
        self.apply_result = apply_result or ApplyResult(
            ApplyStatus.STARTED, "clicked the apply control", clicked="apply_button"
        )
        self.open_error = open_error
        self.start_error = start_error
        self.opened: list[str] = []
        self.started = 0

    async def open_listing(self, page: Any, url: str) -> ListingResult:
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(url)
        return ListingResult(board=self.board, url=url)

    async def start_application(self, page: Any) -> ApplyResult:
        if self.start_error is not None:
            raise self.start_error
        self.started += 1
        return self.apply_result


class FakeTrigger:
    def __init__(
        self,
        before: FormSnapshot,
        after: FormSnapshot,
        *,
        tier: TriggerTier = TriggerTier.IN_PAGE,
        error: BaseException | None = None,
        settled: bool = True,
    ) -> None:
        self.before = before
        self.after = after
        self.tier = tier
        self.error = error
        self.settled = settled
        self.calls = 0

    async def baseline(self, page: Any) -> FormSnapshot:
        return self.before

    async def trigger(self, page: Any, before: FormSnapshot) -> TriggerResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        diff = before.diff(self.after)
        return TriggerResult(
            tier=self.tier,
            diff=diff,
            after=self.after,
            settle=SettleResult(
                snapshot=self.after,
                diff=diff,
                waited_ms=120.0,
                polls=3,
                mutations=4,
                settled=self.settled,
                observed_change=bool(diff.changed),
            ),
            attempts=(TierAttempt(self.tier, True, "clicked"),),
        )


class FakeWriter:
    """Types an answer into a control, or refuses to."""

    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.written: list[tuple[str, str]] = []
        self.fields: list[FormField] = []

    async def write(self, page: Any, field: FormField, value: str) -> bool:
        if not self.succeeds:
            return False
        self.fields.append(field)
        self.written.append((field.key, value))
        return True


class FakeSubmitter:
    def __init__(self, outcome: SubmitOutcome | None = None) -> None:
        self.outcome = outcome or SubmitOutcome(
            submitted=True, reason="fake submission"
        )
        self.calls = 0
        self.authorizations: list[SubmitAuthorization] = []
        self.permits: list[SubmitPermit] = []

    async def submit(
        self, page: Any, authorization: SubmitAuthorization, permit: SubmitPermit
    ) -> SubmitOutcome:
        self.calls += 1
        self.authorizations.append(authorization)
        self.permits.append(permit)
        if not authorization.approved:
            # Refused before anything was clicked, so the one press this
            # application gets stays unspent — the order the real submitter
            # checks in too.
            raise SubmitNotAuthorized(
                authorization.application_id, authorization.refusal()
            )
        permit.claim()
        return self.outcome


class FakeGuard:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def inspect(self, page: Any) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class FakeScreenshotter:
    def __init__(self) -> None:
        self.captured: list[str] = []

    async def capture(self, page: Any, name: str) -> str | None:
        self.captured.append(name)
        return f"/artifacts/{name}.png"


class FakeRouter:
    """A model that always answers, with a fixed, exact cost."""

    def __init__(self, text: str = "Because the work matters.") -> None:
        self.text = text
        self.questions: list[str] = []

    async def complete(self, question: str, complexity: Any) -> ModelAnswer:
        self.questions.append(question)
        return ModelAnswer(
            text=self.text,
            tier=ModelTier.ESCALATION,
            model="gpt-4o",
            usage=TokenUsage(input_tokens=100, output_tokens=50),
            cost=Decimal("0.25"),
        )


@dataclass
class World:
    """A wired graph with only the browser parts faked."""

    tmp_path: Path
    settings: Settings
    db: Database
    broker: FakePageBroker
    adapter: FakeAdapter
    trigger: FakeTrigger
    writer: FakeWriter
    submitter: FakeSubmitter
    guard: FakeGuard
    screenshots: FakeScreenshotter
    router: FakeRouter | None
    deps: GraphDependencies
    checkpoint_path: Path
    built: list[Any] = dataclass_field(default_factory=list)

    def enqueue(
        self, url: str = LISTING_URL, board: Board = Board.LINKEDIN
    ) -> int:
        return self.db.enqueue_job(listing_url=url, board=board)

    @asynccontextmanager
    async def runner(
        self,
        checkpoint_path: Path | None = None,
        *,
        owner: str | None = None,
        lease_ttl: timedelta | None = None,
        heartbeat: timedelta | None = None,
    ) -> AsyncIterator[ApplicationRunner]:
        """A runner over this world's database and checkpoint file.

        `checkpoint_path` stands in for a second worker that cannot see the
        first one's checkpoints — a lost file, or a process pointed at the
        wrong directory — while still sharing the queue database. Two
        runners always have distinct lease owners, which is what makes a
        second one behave like a second process rather than a second
        coroutine.
        """
        async with sqlite_checkpointer(
            checkpoint_path or self.checkpoint_path
        ) as checkpointer:
            runner = ApplicationRunner(
                self.deps,
                checkpointer,
                owner=owner,
                lease_ttl=lease_ttl or DEFAULT_LEASE_TTL,
                heartbeat=heartbeat or DEFAULT_HEARTBEAT_INTERVAL,
            )
            self.built.append(runner)
            yield runner

    def service(self) -> ApprovalService:
        return ApprovalService(self.db)


def build_world(
    tmp_path: Path,
    *,
    auto_submit: bool = False,
    log_field_values: bool = False,
    before: FormSnapshot | None = None,
    after: FormSnapshot | None = None,
    answers: dict[str, Any] | None = None,
    with_router: bool = False,
    adapter: FakeAdapter | None = None,
    trigger_error: BaseException | None = None,
    trigger_settled: bool = True,
    guard_error: BaseException | None = None,
    broker: FakePageBroker | None = None,
    submit_outcome: SubmitOutcome | None = None,
    write_succeeds: bool = True,
    caps: dict[str, int] | None = None,
    adapter_lookup: Any = None,
    clock: Any = None,
) -> World:
    settings = Settings(
        _env_file=None,
        sqlite_path=tmp_path / "jobs.db",
        artifacts_path=tmp_path / "artifacts",
        auto_submit=auto_submit,
        log_field_values=log_field_values,
        **(caps or {}),
    )
    db = Database(settings)
    db.initialize()

    baseline = before if before is not None else snapshot(
        make_field("name", label="Full name", required=True),
        make_field("email", label="Email", required=True),
    )
    filled = after if after is not None else snapshot(
        make_field("name", label="Full name", required=True, filled=True, value="Ada"),
        make_field(
            "email", label="Email", required=True, filled=True, value="ada@example.com"
        ),
    )

    router = FakeRouter() if with_router else None
    gap_filler = GapFiller(
        AnswerBook.from_mapping(answers or {"answers": []}),
        router=router,
        log_field_values=log_field_values,
    )
    world_adapter = adapter or FakeAdapter()
    world_broker = broker or FakePageBroker()
    world_trigger = FakeTrigger(
        baseline, filled, error=trigger_error, settled=trigger_settled
    )
    world_writer = FakeWriter(succeeds=write_succeeds)
    world_submitter = FakeSubmitter(submit_outcome)
    world_guard = FakeGuard(guard_error)
    world_shots = FakeScreenshotter()

    deps = GraphDependencies(
        db=db,
        settings=settings,
        logger=ApplicationLogger(db, settings),
        rate_limiter=RateLimiter(db, settings),
        pages=world_broker,
        adapter_for=adapter_lookup or (lambda board: world_adapter),
        trigger=world_trigger,
        gap_filler=gap_filler,
        writer=world_writer,
        submitter=world_submitter,
        guard=world_guard,
        screenshots=world_shots,
        **({"clock": clock} if clock is not None else {}),
    )
    return World(
        tmp_path=tmp_path,
        settings=settings,
        db=db,
        broker=world_broker,
        adapter=world_adapter,
        trigger=world_trigger,
        writer=world_writer,
        submitter=world_submitter,
        guard=world_guard,
        screenshots=world_shots,
        router=router,
        deps=deps,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
    )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path)


def cover_letter_gap() -> FormField:
    return make_field(
        "cover_letter",
        label="Why do you want to work here?",
        field_type="textarea",
        tag="textarea",
        required=True,
        free_text=True,
    )


def sponsorship_gap() -> FormField:
    return make_field(
        "sponsorship",
        label="Will you now or in the future require visa sponsorship?",
        required=True,
    )
