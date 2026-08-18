"""Tests for board adapters: selector-map safety, URL ownership, and the
per-board apply flows.

All pages are `FakePage` doubles (see `tests/boards/support.py`) built from
recorded HTML snippets modelled on each board's real Apply control, matched
with the actual selector strings shipped in `app/boards/selectors/*.yaml` —
never a browser, never a live site. Two safety properties are proven
directly against those recorded shapes rather than by inspection:

* LinkedIn's Easy Apply and Wellfound's in-app apply flows are detected and
  skipped with a distinct, typed reason *before* any other selector on the
  page is even queried, let alone clicked.
* Jobright's direct Apply-with-Autofill control is only ever clicked when
  the selector map names it explicitly and it resolves to exactly one
  visible element; an absent or ambiguous match falls back to the normal
  Apply control instead of guessing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.boards.base import (
    ApplyStatus,
    BoardAdapterError,
    ListingStatus,
    SelectorMap,
    SelectorMapError,
    SkipReason,
    UntrustedListingUrlError,
    load_selector_map,
)
from app.boards.handshake import HandshakeAdapter
from app.boards.jobright import JobrightAdapter
from app.boards.linkedin import LinkedInAdapter
from app.boards.registry import adapter_for
from app.boards.wellfound import WellfoundAdapter
from app.storage.models import Board
from tests.boards.support import FakePage

# --------------------------------------------------------------------------
# Recorded fixture snippets: small, representative Apply-control markup for
# each board, matched against the real selector strings from
# app/boards/selectors/*.yaml.
# --------------------------------------------------------------------------

LINKEDIN_EASY_APPLY_SNIPPET = """
<div class="jobs-apply-button--top-card">
  <button class="jobs-apply-button artdeco-button artdeco-button--3"
          aria-label="Easy Apply to Senior Backend Engineer at Acme Corp">
    <span class="artdeco-button__text">Easy Apply</span>
  </button>
</div>
"""

LINKEDIN_EXTERNAL_APPLY_SNIPPET = """
<div class="jobs-apply-button--top-card">
  <button class="jobs-apply-button artdeco-button artdeco-button--3"
          aria-label="Apply on company website for Senior Backend Engineer at Acme Corp">
    <span class="artdeco-button__text">Apply</span>
  </button>
</div>
"""

LINKEDIN_NO_APPLY_SNIPPET = """
<div class="jobs-apply-button--top-card">
  <p>This job is no longer accepting applications.</p>
</div>
"""

WELLFOUND_IN_APP_SNIPPET = """
<div class="styles_component__abc123">
  <button data-test="startup-apply-in-app" class="styles_cta__xyz789">
    Apply now
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

JOBRIGHT_NORMAL_ONLY_SNIPPET = """
<div class="listing-actions">
  <button data-test="jobright-apply" class="btn btn-primary">
    Apply
  </button>
</div>
"""

JOBRIGHT_AMBIGUOUS_AUTOFILL_SNIPPET = """
<div class="listing-actions">
  <button data-test="jobright-apply-autofill" class="btn btn-primary">
    Apply with Autofill
  </button>
  <button data-test="jobright-apply-autofill" class="btn btn-primary duplicate">
    Apply with Autofill
  </button>
  <button data-test="jobright-apply" class="btn btn-secondary">
    Apply
  </button>
</div>
"""

JOBRIGHT_HIDDEN_AUTOFILL_SNIPPET = """
<div class="listing-actions">
  <button data-test="jobright-apply-autofill" class="btn btn-primary" hidden>
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


# --------------------------------------------------------------------------
# Selector map loading and validation
# --------------------------------------------------------------------------


class TestSelectorMapLoading:
    @pytest.mark.parametrize(
        "board,required",
        [
            (Board.LINKEDIN, ("easy_apply_indicator", "apply_button")),
            (Board.JOBRIGHT, ("apply_button",)),
            (Board.WELLFOUND, ("in_app_apply_indicator", "apply_button")),
            (Board.HANDSHAKE, ("apply_button",)),
        ],
    )
    def test_shipped_selector_maps_load_and_declare_their_board(
        self, board: Board, required: tuple[str, ...]
    ) -> None:
        selectors = load_selector_map(board, required=required)
        assert selectors.board is board
        for name in required:
            assert selectors.require(name)

    def test_missing_file_is_a_selector_map_error(self, tmp_path: Path) -> None:
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, tmp_path / "does-not-exist.yaml")

    def test_malformed_yaml_is_a_selector_map_error(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        path.write_text("board: linkedin\nselectors: [this is not a mapping\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_non_mapping_top_level_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_selectors_must_be_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        path.write_text("board: linkedin\nselectors: \"not a mapping\"\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_empty_selector_value_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        path.write_text("board: linkedin\nselectors:\n  apply_button: \"   \"\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_non_string_selector_value_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        path.write_text("board: linkedin\nselectors:\n  apply_button: 42\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_mismatched_board_declaration_is_rejected(self, tmp_path: Path) -> None:
        """A copy-pasted file for the wrong board must never load silently."""
        path = tmp_path / "linkedin.yaml"
        path.write_text("board: jobright\nselectors:\n  apply_button: \".apply\"\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_missing_required_selector_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        path.write_text("board: linkedin\nselectors:\n  apply_button: \".apply\"\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path, required=("easy_apply_indicator",))

    def test_oversized_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "linkedin.yaml"
        padding = "x" * 250_000
        path.write_text(f"board: linkedin\nselectors:\n  apply_button: \"{padding}\"\n")
        with pytest.raises(SelectorMapError):
            load_selector_map(Board.LINKEDIN, path)

    def test_selectors_may_churn_without_code_changes(self, tmp_path: Path) -> None:
        """Editing only the YAML file changes what `require()` returns."""
        path = tmp_path / "linkedin.yaml"
        path.write_text(
            "board: linkedin\nselectors:\n"
            "  apply_button: \".old-apply-button\"\n"
            "  easy_apply_indicator: \".old-easy-apply\"\n"
        )
        first = load_selector_map(Board.LINKEDIN, path)
        assert first.require("apply_button") == ".old-apply-button"

        path.write_text(
            "board: linkedin\nselectors:\n"
            "  apply_button: \".new-apply-button\"\n"
            "  easy_apply_indicator: \".new-easy-apply\"\n"
        )
        second = load_selector_map(Board.LINKEDIN, path)
        assert second.require("apply_button") == ".new-apply-button"

    def test_require_raises_for_unknown_selector(self) -> None:
        selectors = SelectorMap(board=Board.LINKEDIN, source="<memory>", selectors={})
        with pytest.raises(SelectorMapError):
            selectors.require("apply_button")

    def test_get_returns_none_for_unknown_selector(self) -> None:
        selectors = SelectorMap(board=Board.LINKEDIN, source="<memory>", selectors={})
        assert selectors.get("apply_button") is None


# --------------------------------------------------------------------------
# Listing URL ownership
# --------------------------------------------------------------------------


class TestListingUrlOwnership:
    async def test_opens_a_genuine_board_url(self) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_EXTERNAL_APPLY_SNIPPET)
        result = await adapter.open_listing(page, "https://www.linkedin.com/jobs/view/12345/")
        assert result.status is ListingStatus.OPENED
        assert page.goto_calls == ["https://www.linkedin.com/jobs/view/12345/"]

    async def test_accepts_a_subdomain_of_the_board_domain(self) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_EXTERNAL_APPLY_SNIPPET)
        result = await adapter.open_listing(page, "https://it.linkedin.com/jobs/view/12345/")
        assert result.status is ListingStatus.OPENED

    @pytest.mark.parametrize(
        "url",
        [
            "https://linkedin.com.evil.example/jobs/view/12345/",
            "https://not-linkedin.com/jobs/view/12345/",
            "https://evillinkedin.com/jobs/view/12345/",
            "https://jobright.ai/jobs/view/12345/",
            "not-a-url",
            "",
        ],
    )
    async def test_refuses_a_lookalike_or_wrong_host(self, url: str) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_EXTERNAL_APPLY_SNIPPET)
        with pytest.raises(UntrustedListingUrlError):
            await adapter.open_listing(page, url)
        assert page.goto_calls == []

    async def test_every_board_only_accepts_its_own_domain(self) -> None:
        wellfound = WellfoundAdapter()
        page = FakePage(WELLFOUND_EXTERNAL_SNIPPET)
        with pytest.raises(UntrustedListingUrlError):
            await wellfound.open_listing(page, "https://www.linkedin.com/jobs/view/1/")

    async def test_jobright_and_handshake_open_their_own_domains(self) -> None:
        jobright = JobrightAdapter()
        page = FakePage(JOBRIGHT_NORMAL_ONLY_SNIPPET)
        result = await jobright.open_listing(page, "https://jobright.ai/jobs/12345")
        assert result.status is ListingStatus.OPENED

        handshake = HandshakeAdapter()
        page2 = FakePage(HANDSHAKE_APPLY_SNIPPET)
        result2 = await handshake.open_listing(
            page2, "https://app.joinhandshake.com/jobs/12345"
        )
        assert result2.status is ListingStatus.OPENED


# --------------------------------------------------------------------------
# LinkedIn: Easy Apply must be skipped, never proceeded into
# --------------------------------------------------------------------------


class TestLinkedInAdapter:
    async def test_easy_apply_is_skipped_before_anything_is_queried(self) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_EASY_APPLY_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.SKIPPED
        assert result.reason == SkipReason.LINKEDIN_EASY_APPLY.value
        # Nothing was clicked: the modal is never entered.
        assert page.clicked == []

    async def test_external_apply_is_started_when_easy_apply_is_absent(self) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_EXTERNAL_APPLY_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert page.clicked  # exactly the external apply control was clicked
        assert len(page.clicked) == 1

    async def test_missing_apply_control_fails_without_skip_reason(self) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_NO_APPLY_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED
        assert result.reason != SkipReason.LINKEDIN_EASY_APPLY.value
        assert page.clicked == []

    async def test_easy_apply_check_happens_before_any_apply_button_query(self) -> None:
        """A distinct-reasons skip must short-circuit, not merely win a race."""
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_EASY_APPLY_SNIPPET)
        await adapter.start_application(page)

        assert page.queried_selectors[0] == adapter.selectors.require("easy_apply_indicator")
        assert adapter.selectors.require("apply_button") not in page.queried_selectors


# --------------------------------------------------------------------------
# Wellfound: in-app apply must be skipped, never proceeded into
# --------------------------------------------------------------------------


class TestWellfoundAdapter:
    async def test_in_app_apply_is_skipped_before_anything_is_queried(self) -> None:
        adapter = WellfoundAdapter()
        page = FakePage(WELLFOUND_IN_APP_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.SKIPPED
        assert result.reason == SkipReason.WELLFOUND_IN_APP_APPLY.value
        assert page.clicked == []

    async def test_external_apply_is_started_when_in_app_is_absent(self) -> None:
        adapter = WellfoundAdapter()
        page = FakePage(WELLFOUND_EXTERNAL_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert len(page.clicked) == 1

    def test_wellfound_and_linkedin_skip_reasons_are_distinct(self) -> None:
        assert SkipReason.WELLFOUND_IN_APP_APPLY != SkipReason.LINKEDIN_EASY_APPLY
        assert (
            SkipReason.WELLFOUND_IN_APP_APPLY.value != SkipReason.LINKEDIN_EASY_APPLY.value
        )


# --------------------------------------------------------------------------
# Jobright: explicit configured Apply-with-Autofill control, safely
# --------------------------------------------------------------------------


class TestJobrightAdapter:
    async def test_clicks_the_explicit_autofill_control_when_present_and_unique(self) -> None:
        adapter = JobrightAdapter()
        page = FakePage(JOBRIGHT_AUTOFILL_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "autofill_apply_button"
        assert page.clicked == ["jobright-apply-autofill"]

    async def test_normal_apply_still_works_when_autofill_control_is_absent(self) -> None:
        adapter = JobrightAdapter()
        page = FakePage(JOBRIGHT_NORMAL_ONLY_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert page.clicked == ["jobright-apply"]

    async def test_ambiguous_autofill_control_falls_back_to_normal_apply(self) -> None:
        """Two elements match the autofill selector: refuse to guess which one."""
        adapter = JobrightAdapter()
        page = FakePage(JOBRIGHT_AMBIGUOUS_AUTOFILL_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert page.clicked == ["jobright-apply"]

    async def test_hidden_autofill_control_falls_back_to_normal_apply(self) -> None:
        adapter = JobrightAdapter()
        page = FakePage(JOBRIGHT_HIDDEN_AUTOFILL_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"

    async def test_missing_every_control_fails(self) -> None:
        adapter = JobrightAdapter()
        page = FakePage("<div>no buttons here</div>")
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED
        assert page.clicked == []

    def test_autofill_selector_is_optional_in_the_selector_map(self, tmp_path: Path) -> None:
        """Jobright's normal apply must keep working with no autofill control configured."""
        path = tmp_path / "jobright.yaml"
        path.write_text("board: jobright\nselectors:\n  apply_button: \"[data-test='jobright-apply']\"\n")
        selectors = load_selector_map(Board.JOBRIGHT, path, required=("apply_button",))
        adapter = JobrightAdapter(selectors)
        assert adapter.selectors.get("autofill_apply_button") is None


# --------------------------------------------------------------------------
# Handshake: a normal, single Apply control
# --------------------------------------------------------------------------


class TestHandshakeAdapter:
    async def test_clicks_the_configured_apply_control(self) -> None:
        adapter = HandshakeAdapter()
        page = FakePage(HANDSHAKE_APPLY_SNIPPET)
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.STARTED
        assert result.clicked == "apply_button"
        assert page.clicked == ["job-apply-button"]

    async def test_missing_control_fails(self) -> None:
        adapter = HandshakeAdapter()
        page = FakePage("<div>nothing to click</div>")
        result = await adapter.start_application(page)

        assert result.status is ApplyStatus.FAILED


# --------------------------------------------------------------------------
# Cross-board safety: adapters refuse a mismatched selector map
# --------------------------------------------------------------------------


class TestAdapterSelectorMapMismatch:
    def test_adapter_refuses_a_selector_map_for_a_different_board(self) -> None:
        wrong = load_selector_map(Board.WELLFOUND)
        with pytest.raises(SelectorMapError):
            LinkedInAdapter(wrong)

    async def test_start_application_degrades_gracefully_with_no_query_api(self) -> None:
        """A page double missing `query_selector_all` fails closed, not with a crash."""

        class NoQueryPage:
            url = "https://www.linkedin.com/jobs/view/1/"

        adapter = LinkedInAdapter()
        result = await adapter.start_application(NoQueryPage())
        assert result.status is ApplyStatus.FAILED

    async def test_open_listing_raises_when_page_has_no_goto(self) -> None:
        class NoGotoPage:
            url = ""

        adapter = LinkedInAdapter()
        with pytest.raises(BoardAdapterError):
            await adapter.open_listing(NoGotoPage(), "https://www.linkedin.com/jobs/view/1/")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize(
        "board,adapter_type",
        [
            (Board.LINKEDIN, LinkedInAdapter),
            (Board.JOBRIGHT, JobrightAdapter),
            (Board.WELLFOUND, WellfoundAdapter),
            (Board.HANDSHAKE, HandshakeAdapter),
        ],
    )
    def test_adapter_for_returns_the_right_type(
        self, board: Board, adapter_type: type
    ) -> None:
        adapter = adapter_for(board)
        assert isinstance(adapter, adapter_type)
        assert adapter.board is board

    def test_adapter_for_builds_a_fresh_instance_each_call(self) -> None:
        first = adapter_for(Board.LINKEDIN)
        second = adapter_for(Board.LINKEDIN)
        assert first is not second

    def test_every_board_enum_member_is_registered(self) -> None:
        for board in Board:
            adapter = adapter_for(board)
            assert adapter.board is board
