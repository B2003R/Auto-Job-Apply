"""Click-safety tests: Playwright exceptions never propagate raw, and a
Jobright direct-apply click that was actually attempted is never followed by
a second, unsafe click.

`click()` failing is not the same as a control being absent, ambiguous, or
hidden. Finding nothing to click, finding more than one match, or finding an
invisible match all mean nothing was ever dispatched to the page — falling
back to a different control is exactly as safe as trying the first one would
have been. But once `.click()` has actually been invoked, a Playwright
exception does not mean nothing happened: the pointer-down may have already
been dispatched before a timeout, a detach, or a mid-click navigation raised.
Treating that the same as "not found" and clicking something else next would
risk a second click on a page already mid-transition from the first. Every
test below drives that distinction through a real adapter with a page double
that raises exactly where a real Playwright call would, rather than
asserting against `attempt_click`/`probe_selector` in isolation.
"""

from __future__ import annotations

import pytest

from app.boards.base import ApplyStatus, SkipReason
from app.boards.handshake import HandshakeAdapter
from app.boards.jobright import JobrightAdapter
from app.boards.linkedin import LinkedInAdapter
from app.boards.wellfound import WellfoundAdapter
from tests.boards.support import FakePage, FaultInjectingPage

LINKEDIN_EXTERNAL_APPLY_SNIPPET = """
<div class="jobs-apply-button--top-card">
  <button class="jobs-apply-button artdeco-button artdeco-button--3"
          aria-label="Apply on company website for Senior Backend Engineer at Acme Corp">
    <span class="artdeco-button__text">Apply</span>
  </button>
</div>
"""

LINKEDIN_EASY_APPLY_SNIPPET = """
<div class="jobs-apply-button--top-card">
  <button class="jobs-apply-button artdeco-button artdeco-button--3"
          aria-label="Easy Apply to Senior Backend Engineer at Acme Corp">
    <span class="artdeco-button__text">Easy Apply</span>
  </button>
</div>
"""

WELLFOUND_EXTERNAL_SNIPPET = """
<div class="styles_component__abc123">
  <a href="https://careers.acme.example/apply" data-test="startup-apply-external"
     class="styles_cta__xyz789">
    Apply on company site
  </a>
</div>
"""

WELLFOUND_IN_APP_SNIPPET = """
<div class="styles_component__abc123">
  <button data-test="startup-apply-in-app" class="styles_cta__xyz789">
    Apply now
  </button>
</div>
"""

JOBRIGHT_AUTOFILL_SNIPPET = """
<div class="listing-actions">
  <button data-test="jobright-apply-autofill" class="btn btn-primary">
    Apply with Autofill
  </button>
  <button data-test="jobright-apply" class="btn btn-secondary">
    Apply
  </button>
</div>
"""

HANDSHAKE_APPLY_SNIPPET = """
<div class="job-detail-actions">
  <button data-hook="job-apply-button" class="sc-button">Apply</button>
</div>
"""


class _TimeoutLikeError(RuntimeError):
    """Stands in for a Playwright `TimeoutError` without depending on Playwright."""


# --------------------------------------------------------------------------
# Jobright: the central "do not click twice" scenario
# --------------------------------------------------------------------------


class TestJobrightClickSafety:
    async def test_autofill_click_failure_does_not_fall_back_to_normal_apply(self) -> None:
        """The autofill control was found, was visible, and `.click()` was
        actually invoked and raised. The click may already have been
        dispatched, so a second click on the normal apply control must never
        be attempted."""
        adapter = JobrightAdapter()
        base_page = FakePage(JOBRIGHT_AUTOFILL_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("autofill_apply_button"),
            fault="click",
            exc=_TimeoutLikeError("Timeout 30000ms exceeded waiting for element to be stable"),
        )

        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED
        assert "autofill" in result.reason.lower() or "click" in result.reason.lower()
        # Nothing was ever actually clicked (the raising double never
        # delegates to a real click), and the normal apply control's
        # selector must never even be queried afterwards.
        assert base_page.clicked == []
        assert adapter.selectors.require("apply_button") not in base_page.queried_selectors

    async def test_autofill_query_failure_falls_back_to_normal_apply(self) -> None:
        """Nothing was dispatched — the query for the autofill control itself
        raised before any element was even found — so falling back to the
        normal apply control is safe and expected."""
        adapter = JobrightAdapter()
        base_page = FakePage(JOBRIGHT_AUTOFILL_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("autofill_apply_button"),
            fault="query",
            exc=RuntimeError("frame was detached"),
        )

        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert base_page.clicked == ["jobright-apply"]

    async def test_autofill_visibility_check_failure_falls_back_to_normal_apply(self) -> None:
        """The visibility check itself raised. Nothing was clicked yet, so
        falling back is exactly as safe as if the control had simply been
        confirmed hidden."""
        adapter = JobrightAdapter()
        base_page = FakePage(JOBRIGHT_AUTOFILL_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("autofill_apply_button"),
            fault="visible",
            exc=RuntimeError("execution context was destroyed"),
        )

        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert base_page.clicked == ["jobright-apply"]

    async def test_normal_apply_click_failure_is_a_typed_failure_not_an_exception(self) -> None:
        adapter = JobrightAdapter()
        base_page = FakePage("<div><button data-test='jobright-apply'>Apply</button></div>")
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("apply_button"),
            fault="click",
            exc=_TimeoutLikeError("Timeout 30000ms exceeded waiting for element to be stable"),
        )

        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED
        assert base_page.clicked == []


# --------------------------------------------------------------------------
# Every adapter: a click() failure on the last-resort control is a typed
# FAILED result, never a propagating exception.
# --------------------------------------------------------------------------


class TestNormalApplyClickFailureIsTypedAcrossAdapters:
    async def test_linkedin_apply_click_failure_returns_failed(self) -> None:
        adapter = LinkedInAdapter()
        base_page = FakePage(LINKEDIN_EXTERNAL_APPLY_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("apply_button"),
            fault="click",
            exc=_TimeoutLikeError("timed out"),
        )
        result = await adapter.start_application(page)
        assert result.status is ApplyStatus.FAILED
        assert base_page.clicked == []

    async def test_wellfound_apply_click_failure_returns_failed(self) -> None:
        adapter = WellfoundAdapter()
        base_page = FakePage(WELLFOUND_EXTERNAL_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("apply_button"),
            fault="click",
            exc=_TimeoutLikeError("timed out"),
        )
        result = await adapter.start_application(page)
        assert result.status is ApplyStatus.FAILED
        assert base_page.clicked == []

    async def test_handshake_apply_click_failure_returns_failed(self) -> None:
        adapter = HandshakeAdapter()
        base_page = FakePage(HANDSHAKE_APPLY_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("apply_button"),
            fault="click",
            exc=_TimeoutLikeError("timed out"),
        )
        result = await adapter.start_application(page)
        assert result.status is ApplyStatus.FAILED
        assert base_page.clicked == []


# --------------------------------------------------------------------------
# Detection probes (Easy Apply / in-app indicator): an exception while
# detecting an unsupported flow must never be silently read as "absent".
# --------------------------------------------------------------------------


class TestUnsupportedFlowDetectionFailureIsIndeterminate:
    async def test_linkedin_easy_apply_probe_failure_does_not_proceed_to_click(self) -> None:
        """If we cannot tell whether this is an Easy Apply listing, the safe
        choice is to fail closed, not to guess "absent" and click whatever
        `apply_button` matches on what might actually be an Easy Apply
        page."""
        adapter = LinkedInAdapter()
        base_page = FakePage(LINKEDIN_EASY_APPLY_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("easy_apply_indicator"),
            fault="query",
            exc=RuntimeError("frame was detached"),
        )

        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED
        assert result.reason != SkipReason.LINKEDIN_EASY_APPLY.value
        assert base_page.clicked == []
        assert adapter.selectors.require("apply_button") not in base_page.queried_selectors

    async def test_wellfound_in_app_probe_failure_does_not_proceed_to_click(self) -> None:
        adapter = WellfoundAdapter()
        base_page = FakePage(WELLFOUND_IN_APP_SNIPPET)
        page = FaultInjectingPage(
            base_page,
            selector=adapter.selectors.require("in_app_apply_indicator"),
            fault="query",
            exc=RuntimeError("frame was detached"),
        )

        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED
        assert result.reason != SkipReason.WELLFOUND_IN_APP_APPLY.value
        assert base_page.clicked == []
        assert adapter.selectors.require("apply_button") not in base_page.queried_selectors


# --------------------------------------------------------------------------
# `attempt_click` / `probe_selector` as standalone primitives.
# --------------------------------------------------------------------------


class TestAttemptClickPrimitive:
    async def test_click_exception_is_not_safe_to_retry_elsewhere(self) -> None:
        from app.boards.base import ClickOutcome, attempt_click

        base_page = FakePage("<button data-test='x'>Go</button>")
        page = FaultInjectingPage(
            base_page, selector="[data-test='x']", fault="click", exc=RuntimeError("boom")
        )
        outcome = await attempt_click(page, "[data-test='x']")
        assert outcome.outcome is ClickOutcome.CLICK_FAILED
        assert outcome.clicked is False
        assert outcome.safe_to_try_another_control is False

    async def test_not_found_and_not_visible_are_safe_to_retry_elsewhere(self) -> None:
        from app.boards.base import ClickOutcome, attempt_click

        page = FakePage("<div>nothing here</div>")
        outcome = await attempt_click(page, "[data-test='x']")
        assert outcome.outcome is ClickOutcome.NOT_FOUND
        assert outcome.safe_to_try_another_control is True

        hidden_page = FakePage("<button data-test='x' hidden>Go</button>")
        outcome2 = await attempt_click(hidden_page, "[data-test='x']")
        assert outcome2.outcome is ClickOutcome.NOT_VISIBLE
        assert outcome2.safe_to_try_another_control is True

    async def test_a_successful_click_reports_clicked(self) -> None:
        from app.boards.base import ClickOutcome, attempt_click

        page = FakePage("<button data-test='x'>Go</button>")
        outcome = await attempt_click(page, "[data-test='x']")
        assert outcome.outcome is ClickOutcome.CLICKED
        assert outcome.clicked is True
        assert page.clicked == ["x"]


class TestProbeSelectorPrimitive:
    async def test_query_exception_is_indeterminate_not_absent(self) -> None:
        from app.boards.base import ProbeOutcome, probe_selector

        base_page = FakePage("<button data-test='x'>Go</button>")
        page = FaultInjectingPage(
            base_page, selector="[data-test='x']", fault="query", exc=RuntimeError("boom")
        )
        probe = await probe_selector(page, "[data-test='x']")
        assert probe.outcome is ProbeOutcome.INDETERMINATE
        assert probe.present is False

    async def test_absent_selector_is_absent_not_indeterminate(self) -> None:
        from app.boards.base import ProbeOutcome, probe_selector

        page = FakePage("<div>nothing here</div>")
        probe = await probe_selector(page, "[data-test='x']")
        assert probe.outcome is ProbeOutcome.ABSENT

    async def test_present_selector_is_present(self) -> None:
        from app.boards.base import ProbeOutcome, probe_selector

        page = FakePage("<button data-test='x'>Go</button>")
        probe = await probe_selector(page, "[data-test='x']")
        assert probe.outcome is ProbeOutcome.PRESENT
        assert probe.present is True
