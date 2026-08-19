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
    SubmitAuthorization,
    SubmitPermit,
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
from app.storage.models import ApprovalDecision, Board, FieldSource, QueueState
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
# The two places a form can be that is not the top document
# --------------------------------------------------------------------------


#: A submission the graph would have authorised, restated as the data the
#: submitter is actually given. The component tests below drive the writer
#: and the submitter directly: the stub extension's content script only runs
#: in the top frame and fills by `document.querySelector`, so a whole-graph
#: run against these pages would be measuring the stub rather than the code
#: under test.
APPROVED = SubmitAuthorization(
    application_id=1,
    thread_id="application-1",
    decision=ApprovalDecision.APPROVED.value,
    decided_at="2026-08-19T12:00:00+00:00",
    gate="approval",
)


class _Claim:
    """The durable claim behind a permit, kept in memory for one test."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest_asyncio.fixture(loop_scope="function")
async def framed_page(context: Any, fixture_server: str) -> AsyncIterator[Any]:
    """The ATS form in a same-origin child frame, under a standing banner."""
    opened = await context.new_page()
    await opened.goto(
        loopback_only(f"{fixture_server}/ats/iframe_host.html"), wait_until="load"
    )
    yield opened
    await opened.close()


@pytest_asyncio.fixture(loop_scope="function")
async def shadow_page(context: Any, fixture_server: str) -> AsyncIterator[Any]:
    """A form whose last field and whose submit control are in shadow roots."""
    opened = await context.new_page()
    await opened.goto(
        loopback_only(f"{fixture_server}/ats/shadow_form.html"), wait_until="load"
    )
    yield opened
    await opened.close()


@pytest_asyncio.fixture(loop_scope="function")
async def live_status_pages(
    context: Any, fixture_server: str
) -> AsyncIterator[Any]:
    """Opens `live_status.html` in one of its modes, and closes what it opened."""
    opened: list[Any] = []

    async def open_in(mode: str) -> Any:
        page = await context.new_page()
        await page.goto(
            loopback_only(f"{fixture_server}/ats/live_status.html?mode={mode}"),
            wait_until="load",
        )
        opened.append(page)
        return page

    yield open_in
    for page in opened:
        await page.close()


def _gap(snapshot: Any, name: str) -> Any:
    return next(field for field in snapshot.fields if field.name == name)


def _application_frame(page: Any) -> Any:
    return next(frame for frame in page.frames if "iframe_form.html" in frame.url)


async def _write_the_last_name(page: Any) -> Any:
    """Fill the one gap, through the writer, and hand back the field.

    Deliberately the writer rather than `page.fill`: every submitter test
    below is submitting a form that this had to have filled correctly, so a
    writer that resolved the wrong control or refused the write takes the
    submission with it instead of being covered up by it.
    """
    scanner = FormScanner()
    snapshot = await scanner.snapshot(page)
    gap = _gap(snapshot, REQUIRED_GAP)
    writer = PlaywrightFieldWriter(scanner=scanner)

    assert await writer.write_or_raise(page, gap, LAST_NAME) is True
    return gap


class TestAFormInASameOriginChildFrame:
    """The shape every ATS ships, and the false success it used to produce.

    The employer's careers page holds the frame; the ATS's form is inside
    it. The submitter presses a control in the child frame, so the child
    frame is what has to be asked about the outcome — and the top page is a
    different document with a different URL, which never held the form, and
    which here is showing a banner that reads exactly like a confirmation
    before anything is clicked at all.

    Judged against a single shared baseline, that top page reported a
    navigation, a vanished form, and a confirmation on the first poll of
    every submission. All three were false, and the outcome was an
    application recorded as sent.
    """

    async def test_a_field_in_the_frame_is_found_again_and_typed_into(
        self, framed_page: Any
    ) -> None:
        """Key parity across a frame boundary, in a browser.

        The scanner derives the key from a control in the child frame; the
        writer has to find its way back to that frame, resolve the same
        control, and re-derive the same key — which is the check that
        refuses a write recorded against a control it did not go into.
        """
        gap = await _write_the_last_name(framed_page)
        frame = _application_frame(framed_page)

        assert gap.frame_url != framed_page.url
        assert await frame.input_value(f"#{REQUIRED_GAP}") == LAST_NAME

    async def test_the_frame_that_submitted_is_what_confirms_it(
        self, framed_page: Any
    ) -> None:
        await _write_the_last_name(framed_page)
        claim = _Claim()

        outcome = await PlaywrightSubmitter(confirm_timeout_ms=8_000).submit(
            framed_page, APPROVED, SubmitPermit(1, claim)
        )

        assert outcome.submitted
        assert claim.calls == 1
        frame = _application_frame(framed_page)
        assert await frame.locator("#fixture-submit-confirmation").count() == 1
        assert await frame.locator("#application-form").count() == 0

    async def test_a_confirmation_the_top_page_was_already_showing_is_not_one(
        self, framed_page: Any
    ) -> None:
        """The exact false success, reproduced and then refused.

        The frame swallows its own submit, so nothing about this
        application changed anywhere. The only confirmation-shaped text on
        the whole page is the banner the top document was already showing,
        the top document never held the form, and its URL is not the
        frame's. An honest answer is "not confirmed".
        """
        await _write_the_last_name(framed_page)
        frame = _application_frame(framed_page)
        await frame.evaluate(
            """
            () => {
              window.addEventListener(
                'submit',
                (event) => {
                  event.preventDefault();
                  event.stopPropagation();
                },
                true,
              );
            }
            """
        )
        claim = _Claim()

        outcome = await PlaywrightSubmitter(confirm_timeout_ms=3_000).submit(
            framed_page, APPROVED, SubmitPermit(1, claim)
        )

        assert outcome.submitted is False
        # The press was made, so the attempt is spent whatever the page said.
        assert claim.calls == 1
        assert await frame.locator("#application-form").count() == 1
        assert await framed_page.locator("#saved-applications").count() == 1
        assert await frame.locator("#fixture-submit-confirmation").count() == 0


class TestAControlInAnOpenShadowRoot:
    """A control no `querySelector` reaches, written to and then pressed.

    Both halves of the shadow path are here on purpose. The scanner builds
    it and the writer reads it, and if the two disagree the writer either
    finds nothing or finds a *different* control — which would record
    somebody's answer against a control it never went into. And the
    submitter has to find its final control through the same boundary,
    press the one in the shadow root, and confirm from the light-DOM banner
    the page puts up afterwards.
    """

    async def test_a_field_in_a_shadow_root_is_found_again_and_typed_into(
        self, shadow_page: Any
    ) -> None:
        gap = await _write_the_last_name(shadow_page)

        assert gap.shadow_depth > 0
        assert gap.shadow_path
        assert await _shadow_value(shadow_page) == LAST_NAME

    async def test_the_control_in_the_shadow_root_is_the_one_pressed(
        self, shadow_page: Any
    ) -> None:
        await _write_the_last_name(shadow_page)
        claim = _Claim()

        outcome = await PlaywrightSubmitter(confirm_timeout_ms=8_000).submit(
            shadow_page, APPROVED, SubmitPermit(1, claim)
        )

        assert outcome.submitted
        assert claim.calls == 1
        assert await shadow_page.locator("#fixture-submit-confirmation").count() == 1
        assert await shadow_page.locator("#application-form").count() == 0

    async def test_a_page_that_says_it_refused_the_press_is_not_a_submission(
        self, shadow_page: Any
    ) -> None:
        """Nothing wrote the last name, so the page rejects the press.

        The rejection is a `role="alert"` the page was not showing before,
        which ends the wait immediately instead of spending the whole
        confirmation timeout and then reporting an outcome nobody can
        check.
        """
        claim = _Claim()

        outcome = await PlaywrightSubmitter(confirm_timeout_ms=8_000).submit(
            shadow_page, APPROVED, SubmitPermit(1, claim)
        )

        assert outcome.submitted is False
        assert "required" in outcome.reason.lower()
        assert claim.calls == 1
        assert await shadow_page.locator("#fixture-submit-confirmation").count() == 0
        assert await shadow_page.locator("#application-form").count() == 1


class TestAConfirmationShapedPanelThatWillNotHoldStill:
    """The page that confirmed every click, in a real browser.

    Freshness was a comparison of the *text* of confirmation-shaped
    regions, and a careers page with a standing "thank you for applying to
    N roles this month" panel changes that text on a timer. Every poll after
    every click therefore found a confirmation nobody had been showing, and
    the submitter reported an application as sent within a fifth of a
    second of pressing a button that did nothing at all.

    Chromium is where this has to be proved: the identity that makes the
    panel one region is stamped on a real node, and it has to survive both a
    text change and the node being thrown away and rebuilt — which is what
    a framework does to its own subtree, and what no double can imitate.
    """

    async def _press(self, page: Any, *, wait_ms: int) -> Any:
        claim = _Claim()
        outcome = await PlaywrightSubmitter(confirm_timeout_ms=wait_ms).submit(
            page, APPROVED, SubmitPermit(1, claim)
        )
        # The press happened either way: an honest "not confirmed" is not a
        # reason to press again.
        assert claim.calls == 1
        return outcome

    async def _panel(self, page: Any) -> str:
        return str(
            await page.locator('#applied-count-wrapper [role="status"]').inner_text()
        )

    async def test_a_panel_that_counts_confirms_nothing(
        self, live_status_pages: Any
    ) -> None:
        page = await live_status_pages("tick")
        before = await self._panel(page)

        outcome = await self._press(page, wait_ms=2_000)

        assert await self._panel(page) != before, "the panel has to have ticked"
        assert outcome.submitted is False
        assert await page.locator("#application-form").count() == 1

    async def test_a_panel_rebuilt_rather_than_edited_confirms_nothing(
        self, live_status_pages: Any
    ) -> None:
        """The node is new every tick; the region is not.

        This is the case an identity minted per node would get wrong, and
        the reason the region's own id is what identifies it when the
        stamp does not survive.
        """
        page = await live_status_pages("rebuild")
        before = await self._panel(page)

        outcome = await self._press(page, wait_ms=2_000)

        assert await self._panel(page) != before
        assert outcome.submitted is False

    async def test_a_panel_with_no_id_of_its_own_confirms_nothing_either(
        self, live_status_pages: Any
    ) -> None:
        """Nothing in the markup names this region.

        Most live regions on the web are an anonymous `<div role="status">`,
        so an identity that relied on the page providing one would be back
        to comparing text on exactly the pages that count.
        """
        page = await live_status_pages("anonymous")
        before = await self._panel(page)

        outcome = await self._press(page, wait_ms=2_000)

        assert await self._panel(page) != before
        assert outcome.submitted is False

    async def test_a_panel_with_neither_an_id_nor_a_surviving_node_confirms_nothing(
        self, live_status_pages: Any
    ) -> None:
        """Both at once, which is the case with nothing to fall back on.

        No id to hold on to and no node that outlives a tick: an identity
        minted per node would be new every reading, and the panel would read
        as a stream of arriving confirmations. Where in the document it is,
        is the one thing about it that holds still.
        """
        page = await live_status_pages("ghost")
        before = await self._panel(page)

        outcome = await self._press(page, wait_ms=2_000)

        assert await self._panel(page) != before
        assert outcome.submitted is False

    async def test_a_panel_that_counts_the_press_itself_confirms_nothing(
        self, live_status_pages: Any
    ) -> None:
        """An optimistic counter is the page congratulating itself.

        Nothing has accepted anything: the count went up because a button
        was pressed, which is the one thing the submitter already knows.
        """
        page = await live_status_pages("optimistic")
        before = await self._panel(page)

        outcome = await self._press(page, wait_ms=2_000)

        assert await self._panel(page) != before
        assert outcome.submitted is False

    async def test_a_confirmation_beside_a_form_that_stayed_confirms_nothing(
        self, live_status_pages: Any
    ) -> None:
        """The words arrive; the form they are about does not go anywhere.

        This is the case where every rule about *which* region spoke is
        still a rule about text. A page that had taken an application would
        not go on showing the form it took, so the confirmation needs
        something structural beside it before it is one.
        """
        page = await live_status_pages("announces")

        outcome = await self._press(page, wait_ms=2_000)

        assert await page.locator("#form-status").inner_text() != "Ready to submit."
        assert outcome.submitted is False
        assert await page.locator("#application-form").is_visible()

    async def test_a_neutral_status_region_becoming_a_confirmation_confirms(
        self, live_status_pages: Any
    ) -> None:
        """The ordinary case, on the same restless page.

        One empty `role="status"` region that the click fills in is how most
        of the web confirms anything, and it is not in the confirmation
        baseline because it was not shaped like one. Here the form it was
        about is hidden with it — not removed, so that the "no longer
        visible" half of the corroboration is the half being driven. The
        panel beside it keeps counting throughout, and is still not what
        confirms.
        """
        page = await live_status_pages("confirms")
        before = await self._panel(page)

        outcome = await self._press(page, wait_ms=8_000)

        assert outcome.submitted
        assert "Your application was submitted" in outcome.reason
        assert "this month" not in outcome.reason
        assert await self._panel(page) != before
        assert await page.locator("#application-form").is_visible() is False


async def _shadow_value(page: Any) -> str:
    """What the shadow-root control actually holds, read past the boundary."""
    return str(
        await page.evaluate(
            """
            () => document
              .getElementById('last-name-host')
              .shadowRoot.querySelector('#last_name').value
            """
        )
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
        """A widget the page has actually rendered at a usable size.

        A `div.g-recaptcha` is what an operator writes and the widget
        iframe appears inside it afterwards; matching the container means
        the challenge is recognised whether or not the third-party script
        ever loaded — which, on a machine with no network, it will not. The
        rendered size is what separates this from the identical container an
        invisible widget is mounted in.
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

    async def test_the_guard_ignores_the_markup_recaptcha_v3_leaves_everywhere(
        self, page: Any, fixture_server: str
    ) -> None:
        """The false positive that would lose applications silently.

        This is the exact markup a v3 or invisible-v2 site key produces on
        a page that challenges nobody: a badge in the corner, an anchor
        iframe inside it, and a widget container that declares itself
        invisible — in a box the page has reserved for it anyway, which
        plenty of layouts do. A guard that matched any of those would
        abandon a perfectly fillable application and report
        `captcha_required`, which nobody can tell was wrong.
        """
        await page.evaluate(
            """
            (base) => {
              const badge = document.createElement('div');
              badge.className = 'grecaptcha-badge';
              badge.style.width = '256px';
              badge.style.height = '60px';
              const anchor = document.createElement('iframe');
              anchor.src = base + '/ats/recaptcha/api2/anchor';
              anchor.title = 'reCAPTCHA';
              badge.appendChild(anchor);
              document.body.appendChild(badge);

              const invisible = document.createElement('div');
              invisible.className = 'g-recaptcha';
              invisible.setAttribute('data-sitekey', 'fixture-key');
              invisible.setAttribute('data-size', 'invisible');
              invisible.style.width = '304px';
              invisible.style.height = '78px';
              document.body.appendChild(invisible);
            }
            """,
            loopback_only(fixture_server),
        )

        await PlaywrightPageGuard().inspect(page)

    async def test_the_guard_refuses_the_challenge_frame_itself(
        self, page: Any, fixture_server: str
    ) -> None:
        """The false negative that must not happen.

        `bframe` is the popup reCAPTCHA opens when it has decided to
        actually ask, as opposed to `anchor`, which it creates either way.
        Served from the loopback fixture server so the frame is a real
        same-origin document rather than one that never loads.
        """
        await page.evaluate(
            """
            (base) => {
              const frame = document.createElement('iframe');
              frame.src = base + '/ats/recaptcha/api2/bframe';
              frame.title = 'recaptcha challenge expires in two minutes';
              frame.style.width = '400px';
              frame.style.height = '580px';
              document.body.appendChild(frame);
            }
            """,
            loopback_only(fixture_server),
        )

        with pytest.raises(CaptchaEncountered) as raised:
            await PlaywrightPageGuard().inspect(page)

        assert "bframe" in raised.value.marker

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

    async def test_a_control_something_is_covering_is_not_clicked_blindly(
        self, runner: ApplicationRunner, database: Database, fixture_server: str, page: Any
    ) -> None:
        """A cookie banner over the button, in a real browser.

        This is why the press is the driver's own click rather than a
        pointer event at the box's remembered centre: the control is still
        there, still visible, still enabled, and still exactly where it was
        counted — and a coordinate press would land on the overlay instead.
        Whatever that overlay is, the application did not go anywhere, and
        the run must not say it did.
        """
        queue_id = _queue(database, fixture_server)
        staged = await runner.run_application(queue_id)
        await page.evaluate(
            """
            () => {
              const banner = document.createElement('div');
              banner.id = 'cookie-banner';
              banner.style.position = 'fixed';
              banner.style.inset = '0';
              banner.style.zIndex = '2147483647';
              banner.style.background = 'rgba(0, 0, 0, 0.01)';
              document.body.appendChild(banner);
            }
            """
        )

        result = await runner.resume_application(
            staged.thread_id,
            _approval(staged.application_id or 0),
        )

        assert result.status is RunStatus.FAILED
        # Named as a control that could not be reached, rather than as a
        # press that went unconfirmed. The difference is what an operator
        # does next: a refused click leaves nothing to check in the ATS.
        assert result.reason == "FinalSubmitControlNotActionable"
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


class _FakeBrowserSession:
    """Satisfies `ApplicationWorker`'s session protocol without a browser.

    The browser here is the one already opened by the `page` fixture; the
    worker under test must not open a second one, so its session factory
    hands back a context nobody looks at.
    """

    async def start(self) -> Any:
        return object()

    async def close(self) -> None:
        return None


class TestAWorkerRestartAcrossTheGate:
    """The regression: restarting the worker must not touch a healthy gate.

    `ApplicationWorker._recover_abandoned` runs once, at startup, to return
    to the queue whatever a *dead* worker was holding. But the approval gate
    releases its execution lease on the way to parking — deliberately, so a
    decision can arrive from a different process later — which means a
    listing sitting at `AWAITING_APPROVAL` looks, from the lease table
    alone, exactly like one a dead worker abandoned mid-flight. This proves
    the sweep tells the two apart against a real browser tab, a real
    LangGraph checkpoint file on disk, and the real `app.main.ApplicationWorker`
    — not merely against the offline fakes the rest of the suite uses.
    """

    async def test_a_restart_leaves_a_staged_application_alone(
        self,
        dependencies: GraphDependencies,
        database: Database,
        fixture_server: str,
        page: Any,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from app.main import ApplicationWorker

        checkpointer_path = tmp_path / "worker-restart-checkpoints.sqlite"

        def session_factory(_settings: Settings) -> Any:
            return _FakeBrowserSession()

        def dependencies_factory(
            _settings: Settings, _db: Database, _context: Any
        ) -> GraphDependencies:
            return dependencies

        def build_worker() -> Any:
            return ApplicationWorker(
                dependencies.settings,
                db=database,
                session_factory=session_factory,
                dependencies_factory=dependencies_factory,
                checkpointer_path=checkpointer_path,
                run_loop=False,
            )

        queue_id = _queue(database, fixture_server)

        # First worker: stage the application for real, up to the gate.
        first_worker = build_worker()
        await first_worker.start()
        try:
            staged = await first_worker.drain()
        finally:
            await first_worker.stop()

        assert [result.queue_id for result in staged] == [queue_id]
        assert staged[0].awaiting_approval
        assert await page.input_value(f"#{REQUIRED_GAP}") == LAST_NAME

        before = database.get_queue_item(queue_id)
        assert before is not None
        assert before.state is QueueState.RUNNING
        assert before.error_reason is None

        # "Stop" and "recreate" the worker: a fresh instance, over the same
        # database and the same checkpoint file, is what a restarted process
        # looks like. Its startup sweep is the code under test.
        second_worker = build_worker()
        with caplog.at_level("WARNING", logger="app.main"):
            await second_worker.start()
        try:
            after = database.get_queue_item(queue_id)
            assert after is not None
            assert after.state is QueueState.RUNNING
            assert after.error_reason is None
            assert "worker_abandoned" not in caplog.text

            # A drain must find nothing to do: the row was never put back
            # in `pending`, so there is nothing left for a second click to
            # restage — and the form the first worker filled in is still
            # exactly where it left it, not reopened or re-clicked.
            resweep = await second_worker.drain()
        finally:
            await second_worker.stop()

        assert resweep == []
        assert await page.locator("#application-form").count() == 1
        assert await page.input_value(f"#{REQUIRED_GAP}") == LAST_NAME

        after_drain = database.get_queue_item(queue_id)
        assert after_drain is not None
        assert after_drain.state is QueueState.RUNNING
        assert after_drain.error_reason is None


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
