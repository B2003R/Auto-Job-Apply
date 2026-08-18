"""Tests for the strictly ordered four-tier Jobright Autofill trigger.

All page, frame, mouse, context, worker, and native-click collaborators are
fakes, so these tests need no browser, no display, no extension, and no
xdotool. The page fakes model focus and page lifetime faithfully enough that
clicking a closed popup, or clicking a page that was never opened by the
tier, fails loudly.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import pytest

from app.agent.errors import (
    FormSettleTimeout,
    NativeClickUnavailable,
    ServiceWorkerNotFoundError,
    TriggerFailed,
)
from app.agent.form_scanner import FormDiff, FormField, FormSnapshot, SettleResult
from app.agent.jobright_trigger import (
    AUTOFILL_QUERY_SCRIPT,
    TIER_ORDER,
    WORKER_ACTION_SCRIPT,
    DeepAutofillClicker,
    ExtensionPopupTier,
    InPageAutofillTier,
    JobrightTrigger,
    NativeToolbarTier,
    ServiceWorkerTier,
    TierActionError,
    TierAttempt,
    TriggerResult,
    TriggerTier,
)
from app.config import Settings

EXTENSION_ID = "abcextensionid1234567890abcdefg"
ATS_URL = "https://ats.example.com/apply"
POPUP_URL = f"chrome-extension://{EXTENSION_ID}/popup.html"
VIEWPORT = {"width": 1280.0, "height": 800.0}
BUTTON_BOX = {"x": 100.0, "y": 40.0, "width": 120.0, "height": 32.0}


def make_field(**overrides: Any) -> FormField:
    values: dict[str, Any] = {
        "key": "field-1",
        "frame_url": ATS_URL,
        "form": "application",
        "control_id": "first-name",
        "name": "first_name",
        "field_type": "text",
        "label": "First name",
        "tag": "input",
        "required": True,
        "disabled": False,
        "visible": True,
        "filled": False,
        "free_text": True,
        "value_digest": "empty-digest",
    }
    values.update(overrides)
    return FormField(**values)


def changed_snapshots() -> tuple[FormSnapshot, FormSnapshot]:
    before = FormSnapshot(fields=(make_field(),))
    after = FormSnapshot(fields=(make_field(filled=True, value_digest="filled-digest"),))
    return before, after


def settle_with_changes() -> tuple[FormSnapshot, SettleResult]:
    before, after = changed_snapshots()
    return before, SettleResult(
        snapshot=after,
        diff=before.diff(after),
        waited_ms=800.0,
        polls=8,
        mutations=12,
        settled=True,
        observed_change=True,
    )


def settle_without_changes() -> tuple[FormSnapshot, SettleResult]:
    before = FormSnapshot(fields=(make_field(),))
    return before, SettleResult(
        snapshot=before,
        diff=before.diff(before),
        waited_ms=800.0,
        polls=8,
        mutations=0,
        settled=True,
        observed_change=False,
    )


def timeout_with_changes() -> tuple[FormSnapshot, FormSettleTimeout]:
    before, after = changed_snapshots()
    unsettled = SettleResult(
        snapshot=after,
        diff=before.diff(after),
        waited_ms=15000.0,
        polls=90,
        mutations=400,
        settled=False,
        observed_change=True,
    )
    return before, FormSettleTimeout(quiet_ms=600, timeout_ms=15000, result=unsettled)


def timeout_without_changes() -> tuple[FormSnapshot, FormSettleTimeout]:
    before = FormSnapshot(fields=(make_field(),))
    unsettled = SettleResult(
        snapshot=before,
        diff=before.diff(before),
        waited_ms=15000.0,
        polls=90,
        mutations=400,
        settled=False,
        observed_change=False,
    )
    return before, FormSettleTimeout(quiet_ms=600, timeout_ms=15000, result=unsettled)


class RecordingTier:
    """Minimal tier double that logs its attempt and succeeds or raises."""

    def __init__(
        self,
        tier: TriggerTier,
        log: list[str],
        *,
        error: Exception | None = None,
        detail: str = "clicked something",
    ) -> None:
        self.tier = tier
        self._log = log
        self._error = error
        self._detail = detail
        self.attempts = 0

    async def attempt(self, page: Any) -> str:
        self.attempts += 1
        self._log.append(self.tier.value)
        if self._error is not None:
            raise self._error
        return self._detail


class FakeScanner:
    """Scanner double returning scripted settle results (or raising)."""

    def __init__(self, results: Sequence[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def wait_for_settle(
        self,
        page: Any,
        previous: FormSnapshot,
        quiet_ms: int,
        timeout_ms: int,
        first_change_timeout_ms: int | None = None,
    ) -> SettleResult:
        self.calls.append(
            {
                "page": page,
                "previous": previous,
                "quiet_ms": quiet_ms,
                "timeout_ms": timeout_ms,
                "first_change_timeout_ms": first_change_timeout_ms,
            }
        )
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, Exception):
            raise result
        return result


class FakeMouse:
    """Mouse double bound to a page; refuses to act on a closed page."""

    def __init__(self, page: "FakePage") -> None:
        self._page = page

    def _check(self) -> None:
        if self._page.closed:
            raise AssertionError(f"{self._page.name} was clicked after it was closed")

    async def move(self, x: float, y: float, **kwargs: Any) -> None:
        self._check()
        self._page.log.append(("move", (x, y)))

    async def down(self, **kwargs: Any) -> None:
        self._check()
        self._page.log.append(("down", self._page.name))

    async def up(self, **kwargs: Any) -> None:
        self._check()
        self._page.log.append(("up", self._page.name))


class FakeElement:
    def __init__(self, box: dict[str, float] | None) -> None:
        self._box = box
        self.scrolled = False

    async def scroll_into_view_if_needed(self) -> None:
        self.scrolled = True

    async def bounding_box(self) -> dict[str, float] | None:
        return self._box


class FakeHandle:
    def __init__(self, element: FakeElement | None) -> None:
        self._element = element
        self.disposed = False

    def as_element(self) -> FakeElement | None:
        return self._element

    async def dispose(self) -> None:
        self.disposed = True


class FakeFrame:
    def __init__(self, url: str, handle: FakeHandle | None = None) -> None:
        self.url = url
        self.name = ""
        self.parent_frame: FakeFrame | None = None
        self.child_frames: list[FakeFrame] = []
        self.handle = handle if handle is not None else FakeHandle(None)
        self.scripts: list[str] = []

    async def evaluate_handle(self, script: str, *args: Any) -> FakeHandle:
        self.scripts.append(script)
        return self.handle


class FakePage:
    def __init__(
        self,
        url: str,
        *,
        log: list[Any] | None = None,
        frames: Iterable[FakeFrame] | None = None,
        context: "FakeContext | None" = None,
        name: str = "page",
        viewport: dict[str, float] | None = None,
    ) -> None:
        self.url = url
        self.log: list[Any] = log if log is not None else []
        self.frames = list(frames) if frames is not None else [FakeFrame(url)]
        self.main_frame = self.frames[0]
        self.context = context
        self.name = name
        self.viewport_size = viewport if viewport is not None else dict(VIEWPORT)
        self.mouse = FakeMouse(self)
        self.closed = False

    async def bring_to_front(self) -> None:
        self.log.append((f"{self.name}.bring_to_front", None))

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.log.append((f"{self.name}.goto", url))
        self.url = url
        for frame in self.frames:
            frame.url = url

    async def close(self) -> None:
        self.closed = True
        self.log.append((f"{self.name}.close", None))


class FakeContext:
    def __init__(self, log: list[Any], popup: FakePage | None = None) -> None:
        self.log = log
        self._popup = popup
        self.pages: list[FakePage] = []
        self.new_pages = 0

    async def new_page(self) -> FakePage:
        self.new_pages += 1
        self.log.append(("context.new_page", None))
        popup = (
            self._popup
            if self._popup is not None
            else FakePage("about:blank", log=self.log, name="popup")
        )
        self.pages.append(popup)
        return popup


class FakeWorker:
    def __init__(self, result: Any, log: list[Any] | None = None) -> None:
        self._result = result
        self.log = log if log is not None else []
        self.scripts: list[str] = []

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.scripts.append(script)
        self.log.append(("worker.evaluate", None))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeNativeClick:
    def __init__(self, log: list[Any], error: Exception | None = None) -> None:
        self.log = log
        self._error = error
        self.clicks = 0

    async def click(self) -> None:
        self.clicks += 1
        self.log.append(("native.click", None))
        if self._error is not None:
            raise self._error


def autofill_frame(
    url: str = ATS_URL, box: dict[str, float] | None = None
) -> tuple[FakeFrame, FakeElement]:
    element = FakeElement(dict(box) if box is not None else dict(BUTTON_BOX))
    return FakeFrame(url, FakeHandle(element)), element


def popup_page(log: list[Any], *, with_control: bool = True) -> FakePage:
    frame = autofill_frame(POPUP_URL)[0] if with_control else FakeFrame(POPUP_URL)
    return FakePage(POPUP_URL, log=log, frames=[frame], name="popup")


def build_trigger(
    tiers: Sequence[Any], scanner: FakeScanner, **kwargs: Any
) -> JobrightTrigger:
    return JobrightTrigger(EXTENSION_ID, tiers=tiers, scanner=scanner, **kwargs)


def event_names(log: Sequence[Any]) -> list[str]:
    return [str(event[0]) for event in log]


class TestTierOrdering:
    async def test_tier_order_constant_matches_the_specified_order(self) -> None:
        assert TIER_ORDER == (
            TriggerTier.IN_PAGE,
            TriggerTier.EXTENSION_POPUP,
            TriggerTier.SERVICE_WORKER,
            TriggerTier.NATIVE_TOOLBAR,
        )

    async def test_all_four_tiers_are_attempted_in_order_when_each_fails(self) -> None:
        log: list[str] = []
        tiers = [
            RecordingTier(tier, log, error=TierActionError(f"{tier.value} unavailable"))
            for tier in TIER_ORDER
        ]
        before, _ = settle_with_changes()

        with pytest.raises(TriggerFailed):
            await build_trigger(tiers, FakeScanner([settle_with_changes()[1]])).trigger(
                FakePage(ATS_URL), before
            )

        assert log == [tier.value for tier in TIER_ORDER]

    async def test_first_success_short_circuits_the_remaining_tiers(self) -> None:
        log: list[str] = []
        before, settled = settle_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log, error=TierActionError("no control")),
            RecordingTier(TriggerTier.EXTENSION_POPUP, log, detail="clicked popup"),
            RecordingTier(TriggerTier.SERVICE_WORKER, log),
            RecordingTier(TriggerTier.NATIVE_TOOLBAR, log),
        ]

        result = await build_trigger(tiers, FakeScanner([settled])).trigger(
            FakePage(ATS_URL), before
        )

        assert log == [TriggerTier.IN_PAGE.value, TriggerTier.EXTENSION_POPUP.value]
        assert result.tier is TriggerTier.EXTENSION_POPUP
        assert tiers[2].attempts == 0
        assert tiers[3].attempts == 0


class TestDiagnosticsAndResult:
    async def test_every_failed_tier_diagnostic_is_retained_on_success(self) -> None:
        log: list[str] = []
        before, settled = settle_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log, error=TierActionError("no control")),
            RecordingTier(
                TriggerTier.EXTENSION_POPUP, log, error=TierActionError("popup blocked")
            ),
            RecordingTier(TriggerTier.SERVICE_WORKER, log, detail="worker opened popup"),
        ]

        result = await build_trigger(tiers, FakeScanner([settled])).trigger(
            FakePage(ATS_URL), before
        )

        assert isinstance(result, TriggerResult)
        assert [attempt.tier for attempt in result.attempts] == [
            TriggerTier.IN_PAGE,
            TriggerTier.EXTENSION_POPUP,
            TriggerTier.SERVICE_WORKER,
        ]
        assert [attempt.succeeded for attempt in result.attempts] == [False, False, True]
        assert "no control" in result.attempts[0].detail
        assert "popup blocked" in result.attempts[1].detail
        assert result.attempts[2].detail == "worker opened popup"
        assert all(isinstance(attempt, TierAttempt) for attempt in result.attempts)

    async def test_returns_the_settled_field_diff_and_snapshot(self) -> None:
        log: list[str] = []
        before, settled = settle_with_changes()
        tiers = [RecordingTier(TriggerTier.IN_PAGE, log)]

        result = await build_trigger(tiers, FakeScanner([settled])).trigger(
            FakePage(ATS_URL), before
        )

        assert isinstance(result.diff, FormDiff)
        assert [field.name for field in result.diff.newly_filled] == ["first_name"]
        assert result.after is settled.snapshot
        assert result.settle is settled
        assert result.settled is True

    async def test_diff_baseline_is_the_caller_supplied_before_snapshot(self) -> None:
        log: list[str] = []
        before, settled = settle_with_changes()
        scanner = FakeScanner([settled])
        page = FakePage(ATS_URL)

        await build_trigger([RecordingTier(TriggerTier.IN_PAGE, log)], scanner).trigger(
            page, before
        )

        assert scanner.calls[0]["previous"] is before
        assert scanner.calls[0]["page"] is page

    async def test_settle_window_is_configurable(self) -> None:
        log: list[str] = []
        before, settled = settle_with_changes()
        scanner = FakeScanner([settled])

        await build_trigger(
            [RecordingTier(TriggerTier.IN_PAGE, log)],
            scanner,
            quiet_ms=333,
            settle_timeout_ms=9999,
            first_change_timeout_ms=1234,
        ).trigger(FakePage(ATS_URL), before)

        assert scanner.calls[0]["quiet_ms"] == 333
        assert scanner.calls[0]["timeout_ms"] == 9999
        assert scanner.calls[0]["first_change_timeout_ms"] == 1234


class TestSettleTimeoutHandling:
    async def test_timeout_with_observed_changes_succeeds_without_more_tiers(self) -> None:
        log: list[str] = []
        before, timeout = timeout_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log, detail="clicked in-page control"),
            RecordingTier(TriggerTier.EXTENSION_POPUP, log),
            RecordingTier(TriggerTier.SERVICE_WORKER, log),
        ]

        result = await build_trigger(tiers, FakeScanner([timeout])).trigger(
            FakePage(ATS_URL), before
        )

        assert log == [TriggerTier.IN_PAGE.value]
        assert result.tier is TriggerTier.IN_PAGE
        assert result.diff.has_changes is True
        assert result.settled is False

    async def test_timeout_success_detail_names_the_timeout_explicitly(self) -> None:
        log: list[str] = []
        before, timeout = timeout_with_changes()
        tiers = [RecordingTier(TriggerTier.IN_PAGE, log, detail="clicked in-page control")]

        result = await build_trigger(tiers, FakeScanner([timeout])).trigger(
            FakePage(ATS_URL), before
        )

        detail = result.attempts[0].detail
        assert result.attempts[0].succeeded is True
        assert "clicked in-page control" in detail
        assert "never settled" in detail
        assert "15000" in detail

    async def test_timeout_without_changes_is_still_a_tier_failure(self) -> None:
        log: list[str] = []
        before, timeout = timeout_without_changes()
        _, settled = settle_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log, detail="clicked in-page control"),
            RecordingTier(TriggerTier.EXTENSION_POPUP, log, detail="clicked popup"),
        ]

        result = await build_trigger(tiers, FakeScanner([timeout, settled])).trigger(
            FakePage(ATS_URL), before
        )

        assert log == [TriggerTier.IN_PAGE.value, TriggerTier.EXTENSION_POPUP.value]
        assert result.tier is TriggerTier.EXTENSION_POPUP
        assert "never settled" in result.attempts[0].detail

    async def test_timeout_with_changes_is_a_failure_when_verification_is_off(self) -> None:
        """With verification disabled there is no change signal to trust, so a
        timeout stays a failure rather than becoming an unverified success."""
        log: list[str] = []
        before, timeout = timeout_with_changes()
        _, settled = settle_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log),
            RecordingTier(TriggerTier.EXTENSION_POPUP, log),
        ]

        result = await build_trigger(
            tiers, FakeScanner([timeout, settled]), require_field_changes=False
        ).trigger(FakePage(ATS_URL), before)

        assert result.tier is TriggerTier.EXTENSION_POPUP


class TestFailureModes:
    async def test_all_tiers_failing_raises_trigger_failed_with_each_diagnostic(
        self,
    ) -> None:
        log: list[str] = []
        before, _ = settle_with_changes()
        tiers = [
            RecordingTier(tier, log, error=TierActionError(f"{tier.value} said no"))
            for tier in TIER_ORDER
        ]

        with pytest.raises(TriggerFailed) as excinfo:
            await build_trigger(tiers, FakeScanner([settle_with_changes()[1]])).trigger(
                FakePage(ATS_URL), before
            )

        error = excinfo.value
        assert len(error.attempts) == 4
        assert all(attempt.succeeded is False for attempt in error.attempts)
        message = str(error)
        for tier in TIER_ORDER:
            assert tier.value in message
            assert f"{tier.value} said no" in message

    async def test_a_tier_that_changes_nothing_is_a_failure_and_the_next_tier_runs(
        self,
    ) -> None:
        log: list[str] = []
        before, unchanged = settle_without_changes()
        _, changed = settle_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log, detail="clicked in-page control"),
            RecordingTier(TriggerTier.EXTENSION_POPUP, log, detail="clicked popup"),
        ]

        result = await build_trigger(tiers, FakeScanner([unchanged, changed])).trigger(
            FakePage(ATS_URL), before
        )

        assert log == [TriggerTier.IN_PAGE.value, TriggerTier.EXTENSION_POPUP.value]
        assert result.tier is TriggerTier.EXTENSION_POPUP
        assert "no field values changed" in result.attempts[0].detail
        assert "clicked in-page control" in result.attempts[0].detail

    async def test_field_change_verification_can_be_disabled(self) -> None:
        log: list[str] = []
        before, unchanged = settle_without_changes()
        tiers = [RecordingTier(TriggerTier.IN_PAGE, log)]

        result = await build_trigger(
            tiers, FakeScanner([unchanged]), require_field_changes=False
        ).trigger(FakePage(ATS_URL), before)

        assert result.tier is TriggerTier.IN_PAGE
        assert result.diff.has_changes is False

    async def test_unexpected_tier_errors_become_diagnostics_not_crashes(self) -> None:
        log: list[str] = []
        before, settled = settle_with_changes()
        tiers = [
            RecordingTier(TriggerTier.IN_PAGE, log, error=RuntimeError("boom")),
            RecordingTier(TriggerTier.EXTENSION_POPUP, log),
        ]

        result = await build_trigger(tiers, FakeScanner([settled])).trigger(
            FakePage(ATS_URL), before
        )

        assert "boom" in result.attempts[0].detail
        assert "RuntimeError" in result.attempts[0].detail

    async def test_no_configured_tiers_raises_trigger_failed(self) -> None:
        before, settled = settle_with_changes()

        with pytest.raises(TriggerFailed):
            await build_trigger([], FakeScanner([settled])).trigger(
                FakePage(ATS_URL), before
            )


class TestBaselineScanner:
    """Digest keys are per-scanner, so the baseline must come from the same
    scanner the trigger later compares against — the trigger therefore has to
    expose it rather than hide it."""

    async def test_scanner_is_exposed_for_taking_the_baseline(self) -> None:
        scanner = FakeScanner([settle_with_changes()[1]])

        trigger = build_trigger([], scanner)

        assert trigger.scanner is scanner

    async def test_baseline_snapshots_with_the_triggers_own_scanner(self) -> None:
        snapshot = FormSnapshot(fields=(make_field(),))

        class SnapshottingScanner(FakeScanner):
            def __init__(self) -> None:
                super().__init__([])
                self.pages: list[Any] = []

            async def snapshot(self, page: Any) -> FormSnapshot:
                self.pages.append(page)
                return snapshot

        scanner = SnapshottingScanner()
        page = FakePage(ATS_URL)

        taken = await build_trigger([], scanner).baseline(page)

        assert taken is snapshot
        assert scanner.pages == [page]


class TestDefaultTierAssembly:
    def test_from_settings_builds_the_four_tiers_in_order(self) -> None:
        settings = Settings(
            _env_file=None, jobright_extension_id=EXTENSION_ID, toolbar_x=10, toolbar_y=20
        )

        trigger = JobrightTrigger.from_settings(settings)

        assert [tier.tier for tier in trigger.tiers] == list(TIER_ORDER)
        assert isinstance(trigger.tiers[0], InPageAutofillTier)
        assert isinstance(trigger.tiers[1], ExtensionPopupTier)
        assert isinstance(trigger.tiers[2], ServiceWorkerTier)
        assert isinstance(trigger.tiers[3], NativeToolbarTier)

    def test_default_tiers_are_used_when_none_are_injected(self) -> None:
        trigger = JobrightTrigger(EXTENSION_ID)

        assert [tier.tier for tier in trigger.tiers] == list(TIER_ORDER)

    def test_tiers_are_exposed_as_an_immutable_sequence(self) -> None:
        trigger = JobrightTrigger(EXTENSION_ID)

        assert isinstance(trigger.tiers, tuple)


class TestDeepAutofillQueryScript:
    def test_script_is_a_callable_javascript_expression(self) -> None:
        assert AUTOFILL_QUERY_SCRIPT.strip().startswith("(")
        assert "=>" in AUTOFILL_QUERY_SCRIPT

    def test_script_matches_autofill_accessible_text_case_insensitively(self) -> None:
        assert "autofill" in AUTOFILL_QUERY_SCRIPT.lower()
        assert "/i" in AUTOFILL_QUERY_SCRIPT

    def test_script_descends_open_shadow_roots(self) -> None:
        assert "shadowRoot" in AUTOFILL_QUERY_SCRIPT

    def test_script_only_considers_real_interactive_controls(self) -> None:
        for interactive in ("button", "[role=\"button\"]", "input[type=\"button\"]"):
            assert interactive in AUTOFILL_QUERY_SCRIPT
        for over_broad in ("'[data-testid]'", "'[aria-label]'", "'[onclick]'"):
            assert over_broad not in AUTOFILL_QUERY_SCRIPT

    def test_script_reads_accessible_name_sources(self) -> None:
        for source in ("aria-label", "textContent", "title", "value"):
            assert source in AUTOFILL_QUERY_SCRIPT

    def test_script_skips_invisible_and_disabled_controls(self) -> None:
        assert "getComputedStyle" in AUTOFILL_QUERY_SCRIPT
        assert "disabled" in AUTOFILL_QUERY_SCRIPT

    def test_script_rejects_page_sized_containers(self) -> None:
        assert "MAX_AREA_FRACTION" in AUTOFILL_QUERY_SCRIPT
        assert "innerWidth" in AUTOFILL_QUERY_SCRIPT
        assert "innerHeight" in AUTOFILL_QUERY_SCRIPT

    def test_script_caps_accessible_text_length(self) -> None:
        assert "MAX_TEXT_LENGTH" in AUTOFILL_QUERY_SCRIPT

    def test_script_prefers_the_smallest_deepest_candidate(self) -> None:
        assert "sort" in AUTOFILL_QUERY_SCRIPT
        assert "area" in AUTOFILL_QUERY_SCRIPT
        assert "depth" in AUTOFILL_QUERY_SCRIPT


class TestInPageTier:
    async def test_clicks_the_found_control_with_a_humanized_mouse_path(self) -> None:
        log: list[Any] = []
        frame, element = autofill_frame()
        page = FakePage(ATS_URL, log=log, frames=[frame])
        tier = InPageAutofillTier(clicker=DeepAutofillClicker(sleep=_noop_sleep, seed=7))

        detail = await tier.attempt(page)

        moves = [event for event in log if event[0] == "move"]
        assert len(moves) >= 4
        assert log[-2][0] == "down"
        assert log[-1][0] == "up"
        last_x, last_y = moves[-1][1]
        assert 100.0 <= last_x <= 220.0
        assert 40.0 <= last_y <= 72.0
        assert element.scrolled is True
        assert "autofill" in detail.lower()

    async def test_uses_the_deep_query_script_in_same_origin_frames_only(self) -> None:
        log: list[Any] = []
        main = FakeFrame(ATS_URL)
        embedded, _ = autofill_frame("https://ats.example.com/embed")
        foreign, _ = autofill_frame("https://tracker.example.net/pixel")
        main.child_frames = [foreign, embedded]
        foreign.parent_frame = main
        embedded.parent_frame = main
        page = FakePage(ATS_URL, log=log, frames=[main, foreign, embedded])
        tier = InPageAutofillTier(clicker=DeepAutofillClicker(sleep=_noop_sleep, seed=1))

        await tier.attempt(page)

        assert main.scripts == [AUTOFILL_QUERY_SCRIPT]
        assert embedded.scripts == [AUTOFILL_QUERY_SCRIPT]
        assert foreign.scripts == []

    async def test_raises_when_no_accessible_autofill_control_exists(self) -> None:
        page = FakePage(ATS_URL, frames=[FakeFrame(ATS_URL)])
        tier = InPageAutofillTier(clicker=DeepAutofillClicker(sleep=_noop_sleep))

        with pytest.raises(TierActionError) as excinfo:
            await tier.attempt(page)

        assert "autofill" in str(excinfo.value).lower()

    async def test_disposes_handles_that_did_not_resolve_to_an_element(self) -> None:
        empty = FakeFrame(ATS_URL)
        page = FakePage(ATS_URL, frames=[empty])
        tier = InPageAutofillTier(clicker=DeepAutofillClicker(sleep=_noop_sleep))

        with pytest.raises(TierActionError):
            await tier.attempt(page)

        assert empty.handle.disposed is True

    async def test_control_without_a_bounding_box_is_reported(self) -> None:
        frame = FakeFrame(ATS_URL, FakeHandle(FakeElement(None)))
        page = FakePage(ATS_URL, frames=[frame])
        tier = InPageAutofillTier(clicker=DeepAutofillClicker(sleep=_noop_sleep))

        with pytest.raises(TierActionError) as excinfo:
            await tier.attempt(page)

        assert "bounding box" in str(excinfo.value)


class TestOversizedControlGuard:
    async def test_refuses_to_click_a_page_sized_container(self) -> None:
        log: list[Any] = []
        frame, _ = autofill_frame(
            ATS_URL, {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 800.0}
        )
        page = FakePage(ATS_URL, log=log, frames=[frame])
        tier = InPageAutofillTier(clicker=DeepAutofillClicker(sleep=_noop_sleep))

        with pytest.raises(TierActionError) as excinfo:
            await tier.attempt(page)

        assert "viewport" in str(excinfo.value)
        assert not [event for event in log if event[0] == "down"]

    async def test_allows_a_small_control_inside_a_small_popup(self) -> None:
        log: list[Any] = []
        frame, _ = autofill_frame(
            POPUP_URL, {"x": 10.0, "y": 10.0, "width": 260.0, "height": 44.0}
        )
        page = FakePage(
            POPUP_URL,
            log=log,
            frames=[frame],
            name="popup",
            viewport={"width": 320.0, "height": 240.0},
        )
        clicker = DeepAutofillClicker(sleep=_noop_sleep, seed=3)

        await clicker.click_in(page)

        assert [event for event in log if event[0] == "down"]

    async def test_guard_is_skipped_when_the_viewport_is_unknown(self) -> None:
        log: list[Any] = []
        frame, _ = autofill_frame(
            ATS_URL, {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 800.0}
        )
        page = FakePage(ATS_URL, log=log, frames=[frame], viewport={})
        clicker = DeepAutofillClicker(sleep=_noop_sleep, seed=3)

        await clicker.click_in(page)

        assert [event for event in log if event[0] == "down"]


class TestExtensionPopupTier:
    def _page_with_popup(self, *, with_control: bool = True) -> tuple[FakePage, FakePage, list[Any]]:
        log: list[Any] = []
        popup = popup_page(log, with_control=with_control)
        context = FakeContext(log, popup)
        page = FakePage(ATS_URL, log=log, context=context, name="ats")
        return page, popup, log

    def _tier(self) -> ExtensionPopupTier:
        return ExtensionPopupTier(
            EXTENSION_ID,
            clicker=DeepAutofillClicker(sleep=_noop_sleep, seed=5),
            sleep=_noop_sleep,
        )

    async def test_brings_the_ats_page_to_front_before_opening_the_popup(self) -> None:
        page, _, log = self._page_with_popup()

        await self._tier().attempt(page)

        names = event_names(log)
        assert names.index("ats.bring_to_front") < names.index("context.new_page")

    async def test_ats_page_is_refocused_before_the_popup_control_is_clicked(self) -> None:
        page, _, log = self._page_with_popup()

        await self._tier().attempt(page)

        names = event_names(log)
        focus_events = [index for index, name in enumerate(names) if name == "ats.bring_to_front"]
        first_click = names.index("down")
        assert any(index < first_click for index in focus_events[1:])
        assert names.index("popup.goto") < focus_events[1]

    async def test_navigates_to_the_extension_popup_url_and_closes_it(self) -> None:
        page, popup, log = self._page_with_popup()

        detail = await self._tier().attempt(page)

        assert ("popup.goto", POPUP_URL) in log
        assert "popup" in detail.lower()
        assert popup.closed is True

    async def test_restores_the_ats_page_and_closes_the_popup_after_failure(self) -> None:
        page, popup, log = self._page_with_popup(with_control=False)

        with pytest.raises(TierActionError):
            await self._tier().attempt(page)

        assert popup.closed is True
        assert event_names(log).count("ats.bring_to_front") >= 2

    async def test_requires_a_browser_context(self) -> None:
        page = FakePage(ATS_URL, context=None)

        with pytest.raises(TierActionError) as excinfo:
            await self._tier().attempt(page)

        assert "context" in str(excinfo.value)


class TestServiceWorkerTier:
    def _tier(self, worker: Any, **kwargs: Any) -> ServiceWorkerTier:
        async def finder(context: Any, extension_id: str, timeout_ms: int) -> Any:
            if isinstance(worker, Exception):
                raise worker
            return worker

        async def probe(worker_handle: Any, extension_id: str, timeout_ms: int) -> None:
            return None

        defaults: dict[str, Any] = {
            "worker_finder": finder,
            "worker_probe": probe,
            "clicker": DeepAutofillClicker(sleep=_noop_sleep, seed=11),
            "sleep": _noop_sleep,
            "popup_timeout_ms": 0,
        }
        defaults.update(kwargs)
        return ServiceWorkerTier(EXTENSION_ID, **defaults)

    def _page(self, context: FakeContext | None = None, log: list[Any] | None = None) -> FakePage:
        shared = log if log is not None else []
        return FakePage(
            ATS_URL, log=shared, context=context or FakeContext(shared), name="ats"
        )

    async def test_worker_script_is_feature_guarded(self) -> None:
        assert "openPopup" in WORKER_ACTION_SCRIPT
        assert "typeof" in WORKER_ACTION_SCRIPT
        assert "chrome" in WORKER_ACTION_SCRIPT
        assert "ok" in WORKER_ACTION_SCRIPT

    async def test_missing_worker_is_reported_with_the_extension_id(self) -> None:
        tier = self._tier(ServiceWorkerNotFoundError(EXTENSION_ID, 5000))

        with pytest.raises(Exception) as excinfo:
            await tier.attempt(self._page())

        assert EXTENSION_ID in str(excinfo.value)

    async def test_unavailable_action_api_is_reported_with_its_reason(self) -> None:
        worker = FakeWorker(
            {"ok": False, "method": "", "reason": "chrome.action.openPopup is not available"}
        )
        tier = self._tier(worker)

        with pytest.raises(TierActionError) as excinfo:
            await tier.attempt(self._page())

        assert "openPopup is not available" in str(excinfo.value)
        assert worker.scripts == [WORKER_ACTION_SCRIPT]

    async def test_unexpected_worker_response_is_reported(self) -> None:
        tier = self._tier(FakeWorker("not-a-mapping"))

        with pytest.raises(TierActionError):
            await tier.attempt(self._page())

    async def test_ats_page_is_foreground_when_the_worker_dispatches(self) -> None:
        log: list[Any] = []
        worker = FakeWorker({"ok": True, "method": "action.openPopup", "reason": ""}, log)
        page = self._page(log=log)

        await self._tier(worker).attempt(page)

        names = event_names(log)
        assert names.index("ats.bring_to_front") < names.index("worker.evaluate")

    async def test_clicks_only_a_newly_opened_popup_and_closes_it(self) -> None:
        log: list[Any] = []
        context = FakeContext(log)
        page = FakePage(ATS_URL, log=log, context=context, name="ats")
        popup = popup_page(log)
        worker = FakeWorker({"ok": True, "method": "action.openPopup", "reason": ""}, log)

        class OpeningWorker(FakeWorker):
            async def evaluate(self, script: str, *args: Any) -> Any:
                result = await super().evaluate(script, *args)
                context.pages.append(popup)
                return result

        opening = OpeningWorker({"ok": True, "method": "action.openPopup", "reason": ""}, log)
        tier = self._tier(opening, popup_timeout_ms=500)

        detail = await tier.attempt(page)

        assert "action.openPopup" in detail
        assert ("down", "popup") in log
        assert popup.closed is True
        assert worker.scripts == []

    async def test_ats_page_is_refocused_before_the_popup_click(self) -> None:
        log: list[Any] = []
        context = FakeContext(log)
        page = FakePage(ATS_URL, log=log, context=context, name="ats")
        popup = popup_page(log)

        class OpeningWorker(FakeWorker):
            async def evaluate(self, script: str, *args: Any) -> Any:
                result = await super().evaluate(script, *args)
                context.pages.append(popup)
                return result

        tier = self._tier(
            OpeningWorker({"ok": True, "method": "action.openPopup", "reason": ""}, log),
            popup_timeout_ms=500,
        )

        await tier.attempt(page)

        names = event_names(log)
        focus_events = [i for i, name in enumerate(names) if name == "ats.bring_to_front"]
        assert len(focus_events) >= 2
        assert focus_events[1] < names.index("down")

    async def test_pre_existing_popup_page_is_never_reused(self) -> None:
        log: list[Any] = []
        context = FakeContext(log)
        stale_popup = popup_page(log)
        context.pages.append(stale_popup)
        page = FakePage(ATS_URL, log=log, context=context, name="ats")
        tier = self._tier(
            FakeWorker({"ok": True, "method": "action.openPopup", "reason": ""}, log)
        )

        detail = await tier.attempt(page)

        assert "not reachable" in detail
        assert stale_popup.closed is False
        assert not [event for event in log if event[0] == "down"]

    async def test_reports_when_no_popup_page_becomes_reachable(self) -> None:
        tier = self._tier(FakeWorker({"ok": True, "method": "action.openPopup", "reason": ""}))

        detail = await tier.attempt(self._page())

        assert "action.openPopup" in detail
        assert "not reachable" in detail


class TestNativeToolbarTier:
    def _tier(self, native: Any, **kwargs: Any) -> NativeToolbarTier:
        defaults: dict[str, Any] = {
            "extension_id": EXTENSION_ID,
            "clicker": DeepAutofillClicker(sleep=_noop_sleep, seed=13),
            "sleep": _noop_sleep,
            "popup_timeout_ms": 0,
        }
        defaults.update(kwargs)
        return NativeToolbarTier(native, **defaults)

    async def test_brings_the_page_to_front_then_clicks_natively(self) -> None:
        log: list[Any] = []
        page = FakePage(ATS_URL, log=log, context=FakeContext(log), name="ats")
        native = FakeNativeClick(log)

        detail = await self._tier(native).attempt(page)

        names = event_names(log)
        assert names.index("ats.bring_to_front") < names.index("native.click")
        assert native.clicks == 1
        assert "xdotool" in detail

    async def test_unavailable_native_click_is_reported(self) -> None:
        log: list[Any] = []
        page = FakePage(ATS_URL, log=log, context=FakeContext(log), name="ats")
        native = FakeNativeClick(log, NativeClickUnavailable("xdotool is not installed"))

        with pytest.raises(NativeClickUnavailable) as excinfo:
            await self._tier(native).attempt(page)

        assert "xdotool is not installed" in str(excinfo.value)

    async def test_missing_native_click_configuration_is_reported(self) -> None:
        page = FakePage(ATS_URL)

        with pytest.raises(TierActionError) as excinfo:
            await self._tier(None).attempt(page)

        assert "calibrat" in str(excinfo.value).lower()

    async def test_drives_and_closes_a_newly_opened_popup_page(self) -> None:
        log: list[Any] = []
        context = FakeContext(log)
        page = FakePage(ATS_URL, log=log, context=context, name="ats")
        popup = popup_page(log)

        class OpeningNativeClick(FakeNativeClick):
            async def click(self) -> None:
                await super().click()
                context.pages.append(popup)

        detail = await self._tier(
            OpeningNativeClick(log), popup_timeout_ms=500
        ).attempt(page)

        assert ("down", "popup") in log
        assert popup.closed is True
        assert "popup" in detail.lower()

    async def test_pre_existing_popup_page_is_never_reused(self) -> None:
        log: list[Any] = []
        context = FakeContext(log)
        stale_popup = popup_page(log)
        context.pages.append(stale_popup)
        page = FakePage(ATS_URL, log=log, context=context, name="ats")

        detail = await self._tier(FakeNativeClick(log)).attempt(page)

        assert stale_popup.closed is False
        assert not [event for event in log if event[0] == "down"]
        assert "xdotool" in detail


async def _noop_sleep(seconds: float) -> None:
    return None
