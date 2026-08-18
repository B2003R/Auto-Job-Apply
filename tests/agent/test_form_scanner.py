"""Tests for deep form snapshotting, diffing, and mutation/value quiescence.

Every test here runs against fake async page/frame doubles: no browser, no
display, and no Playwright import is required. The doubles dispatch on the
exact script constants the scanner evaluates, so a scanner that stopped
using the deep-traversal script (or stopped probing mutations) fails loudly
rather than silently passing.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Sequence

import pytest

from app.agent.errors import FormSettleTimeout
from app.agent.form_scanner import (
    FIELD_SCAN_SCRIPT,
    MUTATION_PROBE_SCRIPT,
    FieldChange,
    FormDiff,
    FormField,
    FormScanner,
    FormSnapshot,
    FrameSkip,
    SettleResult,
)

MAIN_URL = "https://ats.example.com/apply"


def raw_field(**overrides: Any) -> dict[str, Any]:
    """Build one raw field record in the shape the in-page script returns."""
    record: dict[str, Any] = {
        "tag": "input",
        "type": "text",
        "name": "first_name",
        "id": "first-name",
        "label": "First name",
        "form": "application",
        "required": False,
        "disabled": False,
        "visible": True,
        "value": "",
        "shadowDepth": 0,
    }
    record.update(overrides)
    return record


class FakeFrame:
    """Async frame double returning scripted scan batches and mutation counts.

    Each scripted sequence advances one entry per call and then repeats its
    final entry forever, so a test only has to describe the interesting
    prefix of a page's evolution.
    """

    def __init__(
        self,
        url: str,
        *,
        batches: Sequence[Sequence[dict[str, Any]]] | None = None,
        mutations: Sequence[int] | None = None,
        evaluate_error: Exception | None = None,
    ) -> None:
        self.url = url
        self._batches: list[Any] = list(batches) if batches is not None else [[]]
        self._mutations: list[int] = list(mutations) if mutations is not None else [0]
        self._evaluate_error = evaluate_error
        self.scripts: list[str] = []
        self.scan_calls = 0
        self.probe_calls = 0

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.scripts.append(script)
        if self._evaluate_error is not None:
            raise self._evaluate_error
        if script == MUTATION_PROBE_SCRIPT:
            self.probe_calls += 1
            return self._advance(self._mutations)
        if script == FIELD_SCAN_SCRIPT:
            self.scan_calls += 1
            return self._advance(self._batches)
        raise AssertionError(f"unexpected script evaluated: {script[:60]!r}")

    @staticmethod
    def _advance(values: list[Any]) -> Any:
        return values.pop(0) if len(values) > 1 else values[0]


class FakePage:
    def __init__(self, main: FakeFrame, others: Iterable[FakeFrame] = ()) -> None:
        self.main_frame = main
        self.frames = [main, *others]
        self.url = main.url


class FakeClock:
    """Monotonic clock in seconds whose only advance is an awaited sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def snapshot_of(
    fields: Sequence[dict[str, Any]],
    *,
    url: str = MAIN_URL,
    **scanner_kwargs: Any,
) -> FormSnapshot:
    page = FakePage(FakeFrame(url, batches=[fields]))
    return await FormScanner(**scanner_kwargs).snapshot(page)


async def single_field(**overrides: Any) -> FormField:
    snapshot = await snapshot_of([raw_field(**overrides)])
    assert len(snapshot.fields) == 1
    return snapshot.fields[0]


class TestStableFieldKeys:
    async def test_key_is_unchanged_when_only_the_value_changes(self) -> None:
        empty = await single_field(value="")
        filled = await single_field(value="Ada")

        assert empty.key == filled.key
        assert empty.value_digest != filled.value_digest

    async def test_key_is_deterministic_across_scanner_instances(self) -> None:
        first = await single_field()
        second = await single_field()

        assert first.key == second.key

    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": "last_name"},
            {"type": "email"},
            {"label": "Family name"},
            {"form": "eeo"},
            {"id": "other-control"},
        ],
    )
    async def test_key_changes_when_an_identity_component_changes(
        self, overrides: dict[str, Any]
    ) -> None:
        baseline = await single_field()
        variant = await single_field(**overrides)

        assert baseline.key != variant.key

    async def test_key_changes_with_frame_url(self) -> None:
        baseline = await single_field()
        other_frame = await snapshot_of(
            [raw_field()], url="https://ats.example.com/apply/step-2"
        )

        assert baseline.key != other_frame.fields[0].key

    async def test_key_ignores_url_fragment(self) -> None:
        base = await snapshot_of([raw_field()], url=MAIN_URL)
        fragment = await snapshot_of([raw_field()], url=f"{MAIN_URL}#section-2")

        assert base.fields[0].key == fragment.fields[0].key

    async def test_key_ignores_required_marker_and_case_in_label(self) -> None:
        plain = await single_field(label="First name")
        decorated = await single_field(label="  FIRST NAME *  ")

        assert plain.key == decorated.key

    async def test_key_is_stable_when_neighbouring_fields_change(self) -> None:
        before = await snapshot_of([raw_field(), raw_field(name="last_name", id="last")])
        after = await snapshot_of(
            [
                raw_field(name="middle_name", id="middle"),
                raw_field(name="last_name", id="last"),
                raw_field(),
            ]
        )

        before_first = next(f for f in before.fields if f.name == "first_name")
        after_first = next(f for f in after.fields if f.name == "first_name")
        assert before_first.key == after_first.key

    async def test_indistinguishable_duplicates_get_distinct_stable_keys(self) -> None:
        duplicate = raw_field(name="", id="", label="", form="")
        one = await snapshot_of([duplicate])
        two = await snapshot_of([dict(duplicate), dict(duplicate)])

        assert len({field.key for field in two.fields}) == 2
        assert two.fields[0].key == one.fields[0].key


class TestSnapshotParsing:
    async def test_parses_flags_and_identity(self) -> None:
        field = await single_field(
            tag="textarea",
            type="textarea",
            name="cover_letter",
            id="cover",
            label="Cover letter",
            required=True,
            disabled=False,
            visible=True,
            value="",
            shadowDepth=2,
        )

        assert field.tag == "textarea"
        assert field.field_type == "textarea"
        assert field.name == "cover_letter"
        assert field.control_id == "cover"
        assert field.label == "Cover letter"
        assert field.frame_url == MAIN_URL
        assert field.required is True
        assert field.disabled is False
        assert field.visible is True
        assert field.filled is False
        assert field.shadow_depth == 2

    async def test_filled_flag_ignores_whitespace_only_values(self) -> None:
        blank = await single_field(value="   \n ")
        filled = await single_field(value=" Ada ")

        assert blank.filled is False
        assert filled.filled is True

    async def test_values_are_not_captured_by_default(self) -> None:
        field = await single_field(value="ada@example.com")

        assert field.value is None
        assert field.value_digest

    async def test_values_captured_only_when_explicitly_enabled(self) -> None:
        snapshot = await snapshot_of(
            [raw_field(value="ada@example.com")], capture_values=True
        )

        assert snapshot.fields[0].value == "ada@example.com"

    async def test_digest_matches_for_equal_values_and_differs_otherwise(self) -> None:
        one = await single_field(value="Ada")
        same = await single_field(value="Ada")
        other = await single_field(value="Grace")

        assert one.value_digest == same.value_digest
        assert one.value_digest != other.value_digest

    async def test_digest_never_leaks_the_raw_value(self) -> None:
        field = await single_field(value="ada@example.com")

        assert "ada@example.com" not in field.value_digest
        assert "ada@example.com" not in repr(field)

    async def test_malformed_records_are_tolerated(self) -> None:
        snapshot = await snapshot_of([{"tag": "input"}, "not-a-record", None])

        assert len(snapshot.fields) == 1
        assert snapshot.fields[0].tag == "input"
        assert snapshot.fields[0].filled is False


class TestFrameTraversal:
    async def test_aggregates_fields_from_every_same_origin_frame(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        child = FakeFrame(
            "https://ats.example.com/embedded",
            batches=[[raw_field(name="resume", id="resume", label="Resume")]],
        )
        snapshot = await FormScanner().snapshot(FakePage(main, [child]))

        assert {field.name for field in snapshot.fields} == {"first_name", "resume"}
        assert {field.frame_url for field in snapshot.fields} == {
            MAIN_URL,
            "https://ats.example.com/embedded",
        }

    async def test_cross_origin_frames_are_skipped_without_evaluation(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        foreign = FakeFrame(
            "https://tracker.example.net/pixel", batches=[[raw_field(name="hidden")]]
        )
        snapshot = await FormScanner().snapshot(FakePage(main, [foreign]))

        assert foreign.scan_calls == 0
        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert len(snapshot.skipped_frames) == 1
        skip = snapshot.skipped_frames[0]
        assert isinstance(skip, FrameSkip)
        assert skip.frame_url == "https://tracker.example.net/pixel"
        assert "cross-origin" in skip.reason

    async def test_different_port_or_scheme_counts_as_cross_origin(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        other_port = FakeFrame("https://ats.example.com:8443/embed")
        insecure = FakeFrame("http://ats.example.com/embed")
        snapshot = await FormScanner().snapshot(FakePage(main, [other_port, insecure]))

        assert other_port.scan_calls == 0
        assert insecure.scan_calls == 0
        assert len(snapshot.skipped_frames) == 2

    async def test_about_blank_and_srcdoc_frames_inherit_the_main_origin(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        blank = FakeFrame("about:blank", batches=[[raw_field(name="blank_field")]])
        srcdoc = FakeFrame("about:srcdoc", batches=[[raw_field(name="srcdoc_field")]])
        empty = FakeFrame("", batches=[[raw_field(name="empty_field")]])
        snapshot = await FormScanner().snapshot(FakePage(main, [blank, srcdoc, empty]))

        assert {field.name for field in snapshot.fields} == {
            "first_name",
            "blank_field",
            "srcdoc_field",
            "empty_field",
        }
        assert snapshot.skipped_frames == ()

    async def test_frame_evaluation_failure_is_recorded_not_raised(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        detached = FakeFrame(
            "https://ats.example.com/detached",
            evaluate_error=RuntimeError("frame was detached"),
        )
        snapshot = await FormScanner().snapshot(FakePage(main, [detached]))

        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert len(snapshot.skipped_frames) == 1
        assert "frame was detached" in snapshot.skipped_frames[0].reason

    async def test_page_without_frames_collection_is_scanned_directly(self) -> None:
        page = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        snapshot = await FormScanner().snapshot(page)

        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert page.scan_calls == 1


class TestTraversalScript:
    def test_scan_script_is_a_callable_javascript_expression(self) -> None:
        assert FIELD_SCAN_SCRIPT.strip().startswith("(")
        assert "=>" in FIELD_SCAN_SCRIPT

    def test_scan_script_walks_open_shadow_roots(self) -> None:
        assert "shadowRoot" in FIELD_SCAN_SCRIPT
        assert "shadowDepth" in FIELD_SCAN_SCRIPT

    def test_scan_script_collects_every_control_kind(self) -> None:
        for selector in ("input", "textarea", "select"):
            assert selector in FIELD_SCAN_SCRIPT

    def test_scan_script_resolves_accessible_labels(self) -> None:
        for source in ("aria-label", "aria-labelledby", "labels", "placeholder"):
            assert source in FIELD_SCAN_SCRIPT

    def test_scan_script_reports_required_including_aria(self) -> None:
        assert "aria-required" in FIELD_SCAN_SCRIPT
        assert "required" in FIELD_SCAN_SCRIPT

    def test_scan_script_computes_visibility(self) -> None:
        assert "getComputedStyle" in FIELD_SCAN_SCRIPT
        assert "getBoundingClientRect" in FIELD_SCAN_SCRIPT

    def test_scan_script_reads_checked_state_for_toggles(self) -> None:
        assert "checked" in FIELD_SCAN_SCRIPT

    def test_mutation_probe_observes_dom_and_value_events(self) -> None:
        assert "MutationObserver" in MUTATION_PROBE_SCRIPT
        assert "subtree" in MUTATION_PROBE_SCRIPT
        assert "characterData" in MUTATION_PROBE_SCRIPT
        assert "input" in MUTATION_PROBE_SCRIPT
        assert "change" in MUTATION_PROBE_SCRIPT

    def test_mutation_probe_installs_itself_once(self) -> None:
        assert "__jobrightScanState" in MUTATION_PROBE_SCRIPT


class TestGapDetection:
    async def test_required_and_empty_is_a_required_gap(self) -> None:
        snapshot = await snapshot_of(
            [
                raw_field(name="email", id="email", required=True, value=""),
                raw_field(name="phone", id="phone", required=True, value="555-0100"),
                raw_field(name="nickname", id="nick", required=False, value=""),
            ]
        )

        assert [field.name for field in snapshot.required_gaps()] == ["email"]

    async def test_hidden_and_disabled_controls_are_not_gaps(self) -> None:
        snapshot = await snapshot_of(
            [
                raw_field(name="hidden_token", required=True, value="", visible=False),
                raw_field(name="locked", required=True, value="", disabled=True),
            ]
        )

        assert snapshot.required_gaps() == ()

    async def test_empty_free_text_is_an_unanswered_gap_even_when_optional(self) -> None:
        snapshot = await snapshot_of(
            [
                raw_field(
                    tag="textarea",
                    type="textarea",
                    name="cover_letter",
                    id="cover",
                    required=False,
                    value="",
                ),
                raw_field(name="why_us", id="why", type="text", value=""),
                raw_field(name="start_date", id="start", type="date", value=""),
                raw_field(
                    tag="select", type="select-one", name="source", id="src", value=""
                ),
            ]
        )

        assert [field.name for field in snapshot.unanswered_free_text()] == [
            "cover_letter",
            "why_us",
        ]

    async def test_answered_free_text_is_not_a_gap(self) -> None:
        snapshot = await snapshot_of(
            [
                raw_field(
                    tag="textarea",
                    type="textarea",
                    name="cover_letter",
                    id="cover",
                    value="Dear team",
                )
            ]
        )

        assert snapshot.unanswered_free_text() == ()

    async def test_free_text_flag_is_exposed_per_field(self) -> None:
        textarea = await single_field(tag="textarea", type="textarea")
        checkbox = await single_field(type="checkbox")

        assert textarea.free_text is True
        assert checkbox.free_text is False


class TestDiffAttribution:
    async def _before_after(
        self, before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]]
    ) -> FormDiff:
        before_snapshot = await snapshot_of(before)
        after_snapshot = await snapshot_of(after)
        return before_snapshot.diff(after_snapshot)

    async def test_newly_filled_fields_are_attributed(self) -> None:
        diff = await self._before_after(
            [raw_field(value=""), raw_field(name="email", id="email", value="")],
            [
                raw_field(value="Ada"),
                raw_field(name="email", id="email", value="ada@example.com"),
            ],
        )

        assert {field.name for field in diff.newly_filled} == {"first_name", "email"}
        assert len(diff.changed) == 2
        assert all(isinstance(change, FieldChange) for change in diff.changed)
        assert all(change.became_filled for change in diff.changed)

    async def test_edited_values_are_changed_but_not_newly_filled(self) -> None:
        diff = await self._before_after(
            [raw_field(value="Ada")], [raw_field(value="Ada Lovelace")]
        )

        assert len(diff.changed) == 1
        assert diff.newly_filled == ()
        assert diff.changed[0].before.value_digest != diff.changed[0].after.value_digest

    async def test_cleared_fields_are_reported(self) -> None:
        diff = await self._before_after(
            [raw_field(value="Ada")], [raw_field(value="")]
        )

        assert [field.name for field in diff.cleared] == ["first_name"]
        assert diff.changed[0].became_empty is True

    async def test_unchanged_fields_are_absent_from_the_diff(self) -> None:
        diff = await self._before_after([raw_field(value="Ada")], [raw_field(value="Ada")])

        assert diff.changed == ()
        assert diff.newly_filled == ()
        assert diff.has_changes is False

    async def test_added_and_removed_fields_are_reported(self) -> None:
        diff = await self._before_after(
            [raw_field()],
            [raw_field(name="visa_status", id="visa", label="Visa status")],
        )

        assert [field.name for field in diff.added] == ["visa_status"]
        assert [field.name for field in diff.removed] == ["first_name"]

    async def test_still_empty_required_fields_come_from_the_after_snapshot(self) -> None:
        diff = await self._before_after(
            [
                raw_field(name="email", id="email", required=True, value=""),
                raw_field(name="phone", id="phone", required=True, value=""),
            ],
            [
                raw_field(name="email", id="email", required=True, value="a@b.co"),
                raw_field(name="phone", id="phone", required=True, value=""),
            ],
        )

        assert [field.name for field in diff.still_empty_required] == ["phone"]

    async def test_unanswered_free_text_comes_from_the_after_snapshot(self) -> None:
        diff = await self._before_after(
            [
                raw_field(
                    tag="textarea", type="textarea", name="cover", id="cover", value=""
                )
            ],
            [
                raw_field(
                    tag="textarea", type="textarea", name="cover", id="cover", value=""
                )
            ],
        )

        assert [field.name for field in diff.unanswered_free_text] == ["cover"]

    async def test_has_changes_is_true_when_anything_was_filled(self) -> None:
        diff = await self._before_after([raw_field(value="")], [raw_field(value="Ada")])

        assert diff.has_changes is True


class TestImmutability:
    async def test_field_snapshot_and_diff_are_frozen(self) -> None:
        snapshot = await snapshot_of([raw_field()])
        diff = snapshot.diff(snapshot)

        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.fields[0].key = "tampered"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.fields = ()  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            diff.changed = ()  # type: ignore[misc]

    async def test_collections_are_tuples_not_lists(self) -> None:
        snapshot = await snapshot_of([raw_field(required=True, value="")])
        diff = snapshot.diff(snapshot)

        assert isinstance(snapshot.fields, tuple)
        assert isinstance(snapshot.skipped_frames, tuple)
        assert isinstance(diff.changed, tuple)
        assert isinstance(diff.newly_filled, tuple)
        assert isinstance(diff.still_empty_required, tuple)
        assert isinstance(snapshot.required_gaps(), tuple)


class TestSettleDetection:
    def _scanner(self, clock: FakeClock, **kwargs: Any) -> FormScanner:
        return FormScanner(
            clock=clock.monotonic, sleep=clock.sleep, poll_interval_ms=100, **kwargs
        )

    async def test_settles_only_after_values_stop_changing(self) -> None:
        clock = FakeClock()
        frame = FakeFrame(
            MAIN_URL,
            batches=[
                [raw_field(value="")],
                [raw_field(value="A")],
                [raw_field(value="Ada")],
            ],
            mutations=[0, 1, 2],
        )
        page = FakePage(frame)
        previous = FormSnapshot(fields=())

        result = await self._scanner(clock).wait_for_settle(
            page, previous, quiet_ms=250, timeout_ms=5000
        )

        assert isinstance(result, SettleResult)
        assert result.waited_ms >= 250
        assert result.snapshot.fields[0].filled is True
        assert frame.scan_calls >= 4

    async def test_quiet_page_settles_after_the_quiet_period_not_the_timeout(self) -> None:
        clock = FakeClock()
        page = FakePage(FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]]))

        result = await self._scanner(clock).wait_for_settle(
            page, FormSnapshot(fields=()), quiet_ms=250, timeout_ms=30000
        )

        assert 250 <= result.waited_ms < 1000

    async def test_dom_mutations_without_value_changes_delay_settling(self) -> None:
        clock = FakeClock()
        frame = FakeFrame(
            MAIN_URL,
            batches=[[raw_field(value="Ada")]],
            mutations=[0, 1, 2, 3, 4, 4],
        )

        result = await self._scanner(clock).wait_for_settle(
            FakePage(frame), FormSnapshot(fields=()), quiet_ms=250, timeout_ms=5000
        )

        assert result.waited_ms >= 250 + 4 * 100

    async def test_waiting_uses_repeated_polls_not_one_fixed_sleep(self) -> None:
        clock = FakeClock()
        page = FakePage(FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]]))

        await self._scanner(clock).wait_for_settle(
            page, FormSnapshot(fields=()), quiet_ms=250, timeout_ms=5000
        )

        assert len(clock.sleeps) >= 2
        assert set(clock.sleeps) == {0.1}

    async def test_returns_the_diff_against_the_previous_snapshot(self) -> None:
        clock = FakeClock()
        before = await snapshot_of([raw_field(value="")])
        page = FakePage(FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]]))

        result = await self._scanner(clock).wait_for_settle(
            page, before, quiet_ms=200, timeout_ms=5000
        )

        assert isinstance(result.diff, FormDiff)
        assert [field.name for field in result.diff.newly_filled] == ["first_name"]

    async def test_probes_mutations_in_every_same_origin_frame(self) -> None:
        clock = FakeClock()
        main = FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]])
        child = FakeFrame("https://ats.example.com/embed", batches=[[]])
        foreign = FakeFrame("https://tracker.example.net/pixel")

        await self._scanner(clock).wait_for_settle(
            FakePage(main, [child, foreign]),
            FormSnapshot(fields=()),
            quiet_ms=200,
            timeout_ms=5000,
        )

        assert main.probe_calls >= 1
        assert child.probe_calls >= 1
        assert foreign.probe_calls == 0

    async def test_raises_when_the_page_never_goes_quiet(self) -> None:
        clock = FakeClock()
        frame = FakeFrame(
            MAIN_URL,
            batches=[[raw_field(value=f"v{index}")] for index in range(200)],
            mutations=list(range(200)),
        )

        with pytest.raises(FormSettleTimeout) as excinfo:
            await self._scanner(clock).wait_for_settle(
                FakePage(frame), FormSnapshot(fields=()), quiet_ms=300, timeout_ms=1000
            )

        error = excinfo.value
        assert error.quiet_ms == 300
        assert error.timeout_ms == 1000
        assert isinstance(error.snapshot, FormSnapshot)
        assert "1000" in str(error)

    async def test_timeout_error_carries_the_last_observed_diff(self) -> None:
        clock = FakeClock()
        before = await snapshot_of([raw_field(value="")])
        frame = FakeFrame(
            MAIN_URL,
            batches=[[raw_field(value=f"v{index}")] for index in range(200)],
            mutations=list(range(200)),
        )

        with pytest.raises(FormSettleTimeout) as excinfo:
            await self._scanner(clock).wait_for_settle(
                FakePage(frame), before, quiet_ms=300, timeout_ms=1000
            )

        assert isinstance(excinfo.value.diff, FormDiff)
        assert excinfo.value.diff.has_changes is True
