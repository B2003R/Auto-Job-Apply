"""The one suite that drives a real browser, against a loopback fixture.

Every other test in this repository fakes the browser, which is what makes
them fast and safe — and also means the JavaScript in `form_scanner`,
`jobright_trigger`, and `browser_actions` is never executed by a browser in
CI. That is a large blind spot in a project whose whole job is to operate a
page: a selector that matches nothing, a shadow-root path built one way in
the scanner and read another way in the writer, or an event a framework
ignores would all pass the unit tests.

So this suite runs the real thing:

* a headed Chromium persistent context with the unpacked MV3 stub extension
  from `tests/fixtures/fake_extension/` — and `--disable-extensions-except`,
  so a pass cannot be crediting somebody's real Jobright installation;
* the ATS fixtures served over 127.0.0.1 by the fixture server, with
  `loopback_only` asserting that is where the browser is pointed;
* the real `FormScanner`, `JobrightTrigger`, `PlaywrightFieldWriter`,
  `PlaywrightPageGuard`, and `PlaywrightSubmitter`, wired into the real
  LangGraph graph with the real database, gap filler, and approval service.

Only two things are faked, both because the alternative is applying for a
job: the board adapter (which would otherwise navigate LinkedIn) and the
listing URL (which points at the fixture). **No test here can reach a real
application form**: the fixture is loopback, the submit is answered by a
local handler, and nothing in the graph is given a real board.

Prerequisites are checked rather than assumed. A machine with no browser or
no display gets a skip naming the thing to install; see
`tests/integration/browser.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio

from app.agent.browser_actions import (
    PlaywrightFieldWriter,
    PlaywrightPageGuard,
    PlaywrightSubmitter,
)
from app.agent.errors import CaptchaEncountered, LoginWallEncountered
from app.agent.form_scanner import FormScanner
from app.agent.gap_filler import AnswerBook, GapFiller
from app.agent.graph import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE_TTL,
    ApplicationRunner,
    GraphDependencies,
    RunStatus,
    sqlite_checkpointer,
)
from app.agent.jobright_trigger import (
    DeepAutofillClicker,
    JobrightTrigger,
    TriggerTier,
)
from app.agent.rate_limiter import RateLimiter
from app.boards.base import ApplyResult, ApplyStatus, ListingResult
from app.config import Settings
from app.storage.db import Database
from app.storage.logger import ApplicationLogger
from app.storage.models import ApprovalDecision, Board, FieldSource
from tests.agent.support import FakeAdapter
from tests.integration.browser import loopback_only

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "stub_gap_contract.json"
    ).read_text(encoding="utf-8")
)["greenhouse"]

#: The two gaps the stub leaves on purpose. Read from the shared contract so
#: this suite cannot disagree with the offline one about what a gap is.
REQUIRED_GAP = CONTRACT["required_input_left_empty"]
TEXTAREA_GAP = CONTRACT["textarea_left_empty"]

#: The answer the applicant has already given, so the required gap can be
#: filled by the writer rather than by a model.
LAST_NAME = "Lovelace"

APPROVER = "integration@example.com"


# --------------------------------------------------------------------------
# The graph, wired the way production wires it
# --------------------------------------------------------------------------


class _OnePageBroker:
    """Hands every thread the one tab this test opened.

    The production broker opens a tab per thread from the browser context.
    Here the tab is already open and pointed at the fixture, and closing it
    between the staging run and the resumed one would throw away the form
    the whole test is about.
    """

    def __init__(self, page: Any) -> None:
        self._page = page
        self.released: list[str] = []

    async def open(self, thread_id: str) -> Any:
        return self._page

    async def get(self, thread_id: str) -> Any:
        return self._page

    async def release(self, thread_id: str) -> None:
        self.released.append(thread_id)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        sqlite_path=tmp_path / "jobs.db",
        artifacts_path=tmp_path / "artifacts",
        auto_submit=False,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings)
    db.initialize()
    return db


@pytest.fixture
def trigger() -> JobrightTrigger:
    """The real trigger, restricted to the tier a fixture page can serve.

    Tiers two through four talk to a real extension's popup, service worker,
    or toolbar pixel. The stub has none of those, and a test that let the
    trigger fall through to them would be measuring the fallbacks rather
    than the in-page click.
    """
    from app.agent.jobright_trigger import InPageAutofillTier

    scanner = FormScanner()
    return JobrightTrigger(
        "stub-extension-id",
        scanner=scanner,
        tiers=(InPageAutofillTier(clicker=DeepAutofillClicker()),),
    )


@pytest.fixture
def dependencies(
    settings: Settings, database: Database, trigger: JobrightTrigger, page: Any
) -> GraphDependencies:
    """Production wiring, with the board and the tab supplied by this test."""
    answers = AnswerBook.from_mapping(
        {
            "answers": [
                {"question": "Last name", "name": REQUIRED_GAP, "value": LAST_NAME},
            ]
        }
    )
    return GraphDependencies(
        db=database,
        settings=settings,
        logger=ApplicationLogger(database, settings),
        rate_limiter=RateLimiter(database, settings),
        pages=_OnePageBroker(page),
        trigger=trigger,
        gap_filler=GapFiller(answers, router=None),
        writer=PlaywrightFieldWriter(scanner=trigger.scanner),
        guard=PlaywrightPageGuard(),
        submitter=PlaywrightSubmitter(confirm_timeout_ms=8_000),
        adapter_for=lambda board: FakeAdapter(
            Board.LINKEDIN,
            apply_result=ApplyResult(
                ApplyStatus.STARTED, "the fixture form is already open"
            ),
        ),
    )


@pytest_asyncio.fixture(loop_scope="function")
async def runner(
    dependencies: GraphDependencies, tmp_path: Path
) -> AsyncIterator[ApplicationRunner]:
    async with sqlite_checkpointer(tmp_path / "checkpoints.sqlite") as checkpointer:
        yield ApplicationRunner(
            dependencies,
            checkpointer,
            lease_ttl=DEFAULT_LEASE_TTL,
            heartbeat=DEFAULT_HEARTBEAT_INTERVAL,
        )


def _queue(database: Database, fixture_server: str) -> int:
    return database.enqueue_job(
        listing_url=loopback_only(f"{fixture_server}/ats/greenhouse.html"),
        board=Board.LINKEDIN,
    )


# --------------------------------------------------------------------------
# The extension, in a browser
# --------------------------------------------------------------------------


class TestTheStubExtensionInARealBrowser:
    """What the offline suite asserts about the stub, actually observed."""

    async def test_the_in_page_tier_clicks_a_button_in_an_open_shadow_root(
        self, page: Any, trigger: JobrightTrigger
    ) -> None:
        """Tier one, against a control no `querySelector` would reach.

        The stub's Autofill button lives in an open shadow root attached to
        `documentElement`, which is where a real extension puts its sidebar.
        A deep query is the only thing that finds it.
        """
        before = await trigger.baseline(page)
        result = await trigger.trigger(page, before)

        assert result.tier is TriggerTier.IN_PAGE
        assert result.attempts[0].succeeded
        assert "autofill" in result.attempts[0].detail.lower()

    async def test_the_page_is_waited_out_rather_than_slept_through(
        self, page: Any, trigger: JobrightTrigger
    ) -> None:
        """The stub fills fields 25ms apart; quiescence is observed."""
        before = await trigger.baseline(page)
        result = await trigger.trigger(page, before)

        assert result.settle.settled is True
        assert result.settle.observed_change is True
        assert result.settle.mutations > 0
        assert result.settle.waited_ms > 0

    async def test_every_filled_field_is_attributed_to_the_extension(
        self, page: Any, trigger: JobrightTrigger
    ) -> None:
        before = await trigger.baseline(page)
        result = await trigger.trigger(page, before)

        filled = {
            change.after.name for change in result.diff.changed if change.became_filled
        }
        assert filled == set(CONTRACT["partial_values"])

    async def test_exactly_one_required_input_and_one_textarea_are_left_open(
        self, page: Any, trigger: JobrightTrigger
    ) -> None:
        """The gap contract, observed in a browser rather than simulated."""
        before = await trigger.baseline(page)
        result = await trigger.trigger(page, before)

        required = [field.name for field in result.diff.still_empty_required]
        # Every empty text control is an unanswered free-text gap, which is
        # what routes an optional phone number to the applicant too. The
        # contract is about the *textarea*, so that is what is counted.
        textareas = [
            field.name
            for field in result.diff.unanswered_free_text
            if field.tag == "textarea"
        ]

        assert required == [REQUIRED_GAP]
        assert textareas == [TEXTAREA_GAP]
        assert result.diff.coverage_complete is True

    async def test_a_scanned_field_can_be_found_again_and_typed_into(
        self, page: Any, trigger: JobrightTrigger
    ) -> None:
        """The writer's half of the contract, end to end.

        The key is re-derived in the page from the control the writer
        resolved and compared with the key the scanner produced, so this
        passing means the two halves agree about what a control's identity
        is — the thing no unit test can check.
        """
        before = await trigger.baseline(page)
        result = await trigger.trigger(page, before)
        gap = next(
            field for field in result.diff.still_empty_required if field.name == REQUIRED_GAP
        )
        writer = PlaywrightFieldWriter(scanner=trigger.scanner)

        assert await writer.write_or_raise(page, gap, LAST_NAME) is True
        assert await page.input_value(f"#{REQUIRED_GAP}") == LAST_NAME

    async def test_the_guard_lets_an_ordinary_application_page_through(
        self, page: Any
    ) -> None:
        await PlaywrightPageGuard().inspect(page)

    async def test_the_guard_refuses_a_page_showing_a_challenge(
        self, page: Any
    ) -> None:
        """The container Google's own snippet asks a site to put on the page.

        A `div.g-recaptcha` carrying a site key is what an operator writes;
        the widget iframe appears inside it afterwards. Matching the
        container means the challenge is recognised whether or not the
        third-party script ever loaded — which, on a machine with no network,
        it will not.
        """
        await page.evaluate(
            """
            () => {
              const widget = document.createElement('div');
              widget.className = 'g-recaptcha';
              widget.setAttribute('data-sitekey', 'fixture-key');
              widget.style.width = '304px';
              widget.style.height = '78px';
              document.body.appendChild(widget);
            }
            """
        )

        with pytest.raises(CaptchaEncountered):
            await PlaywrightPageGuard().inspect(page)

    async def test_a_frame_that_never_answers_does_not_hold_the_guard(
        self, page: Any
    ) -> None:
        """An `about:blank` iframe has no execution context to evaluate in.

        The driver will wait for one indefinitely, so the guard bounds each
        frame itself. Observed here rather than only against a double,
        because the hang is a property of the browser rather than of the
        Python.
        """
        await page.evaluate(
            """
            () => {
              const frame = document.createElement('iframe');
              frame.src = 'about:blank#pending';
              document.body.appendChild(frame);
            }
            """
        )

        await asyncio.wait_for(
            PlaywrightPageGuard(frame_timeout_ms=1_000).inspect(page), timeout=15
        )

    async def test_the_guard_refuses_a_page_asking_for_a_password(
        self, page: Any
    ) -> None:
        await page.evaluate(
            """
            () => {
              const field = document.createElement('input');
              field.type = 'password';
              field.style.width = '200px';
              field.style.height = '30px';
              document.body.appendChild(field);
            }
            """
        )

        with pytest.raises(LoginWallEncountered):
            await PlaywrightPageGuard().inspect(page)


# --------------------------------------------------------------------------
# A whole application, staged and then approved
# --------------------------------------------------------------------------


class TestAStagedApplicationThatIsThenApproved:
    """The full path, with every browser-facing component the real one.

    The listing is a loopback fixture and the board adapter is a fake, so
    nothing here can reach a real application. Everything between those two
    ends — the scan, the trigger, the gap plan, the write, the guard, the
    approval gate, the click, and the confirmation — is production code.
    """

    async def test_it_stages_with_the_gaps_the_stub_left(
        self, runner: ApplicationRunner, database: Database, fixture_server: str
    ) -> None:
        queue_id = _queue(database, fixture_server)

        staged = await runner.run_application(queue_id)

        assert staged.status is RunStatus.AWAITING_APPROVAL
        assert staged.awaiting_approval
        assert "unanswered_gap" in staged.blocking_reasons

    async def test_it_types_the_answer_it_had_and_leaves_the_one_it_did_not(
        self, runner: ApplicationRunner, database: Database, fixture_server: str, page: Any
    ) -> None:
        """The required gap is answered from the answers file and typed.

        The free-text gap has no canonical answer and no model configured,
        so it stays empty and holds the application at the gate — which is
        the behaviour that makes the gate worth having.
        """
        queue_id = _queue(database, fixture_server)

        staged = await runner.run_application(queue_id)

        assert await page.input_value(f"#{REQUIRED_GAP}") == LAST_NAME
        assert await page.input_value(f"#{TEXTAREA_GAP}") == ""
        rows = database.get_application_fields(staged.application_id or 0)
        typed = [row for row in rows if row.metadata.get("name") == REQUIRED_GAP]
        assert [row.filled for row in typed] == [True]
        assert [row.source for row in typed] == [FieldSource.USER]

    async def test_an_approval_submits_the_form_and_the_page_confirms_it(
        self, runner: ApplicationRunner, database: Database, fixture_server: str, page: Any
    ) -> None:
        queue_id = _queue(database, fixture_server)
        staged = await runner.run_application(queue_id)

        result = await runner.resume_application(
            staged.thread_id,
            _approval(staged.application_id or 0),
        )

        assert result.status is RunStatus.SUBMITTED
        assert "confirmation" in (result.reason or "")
        assert await page.locator("#fixture-submit-confirmation").count() == 1
        assert await page.locator("#application-form").count() == 0

    async def test_a_rejection_leaves_the_form_exactly_where_it_was(
        self, runner: ApplicationRunner, database: Database, fixture_server: str, page: Any
    ) -> None:
        queue_id = _queue(database, fixture_server)
        staged = await runner.run_application(queue_id)

        result = await runner.resume_application(
            staged.thread_id,
            _approval(staged.application_id or 0, ApprovalDecision.REJECTED),
        )

        assert result.status is RunStatus.REJECTED
        assert await page.locator("#application-form").count() == 1
        assert await page.locator("#fixture-submit-confirmation").count() == 0

    async def test_two_final_submit_controls_are_a_refusal_rather_than_a_guess(
        self, runner: ApplicationRunner, database: Database, fixture_server: str, page: Any
    ) -> None:
        """An ambiguous page is not submitted, and is not half-submitted.

        Duplicating the button is exactly what a page does when it renders a
        sticky footer copy of its own submit control, and clicking "the
        first one" would be a guess about which form is being sent.
        """
        queue_id = _queue(database, fixture_server)
        staged = await runner.run_application(queue_id)
        await page.evaluate(
            """
            () => {
              const original = document.querySelector('#application-form button');
              const copy = original.cloneNode(true);
              document.body.appendChild(copy);
            }
            """
        )

        result = await runner.resume_application(
            staged.thread_id,
            _approval(staged.application_id or 0),
        )

        assert result.status is RunStatus.FAILED
        assert await page.locator("#fixture-submit-confirmation").count() == 0
        assert await page.locator("#application-form").count() == 1

    async def test_a_next_button_is_never_the_one_that_gets_clicked(
        self, runner: ApplicationRunner, database: Database, fixture_server: str, page: Any
    ) -> None:
        """The wizard case: the only visible control says "Next".

        A submitter that treated any submit-typed button as final would
        advance somebody's multi-step application on its own.
        """
        queue_id = _queue(database, fixture_server)
        staged = await runner.run_application(queue_id)
        await page.evaluate(
            """
            () => {
              const button = document.querySelector('#application-form button');
              button.textContent = 'Next';
            }
            """
        )

        result = await runner.resume_application(
            staged.thread_id,
            _approval(staged.application_id or 0),
        )

        assert result.status is RunStatus.FAILED
        assert await page.locator("#application-form").count() == 1
        assert await page.locator("#fixture-submit-confirmation").count() == 0


def _approval(
    application_id: int, decision: ApprovalDecision = ApprovalDecision.APPROVED
) -> Any:
    from app.agent.approval import ApprovalRequest

    return ApprovalRequest(
        application_id=application_id,
        decision=decision,
        actor=APPROVER,
        note="integration test",
    )


def test_the_lease_defaults_are_not_shortened_here() -> None:
    """A guard on the fixtures above: a browser run is slower than a fake.

    If somebody tunes these defaults down far enough that a real browser
    cannot finish a node inside one, this suite would start failing on
    lease expiry and look like a browser problem.
    """
    assert DEFAULT_LEASE_TTL >= timedelta(seconds=30)
    assert DEFAULT_HEARTBEAT_INTERVAL < DEFAULT_LEASE_TTL
