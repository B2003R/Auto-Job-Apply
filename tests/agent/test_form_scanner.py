"""Tests for deep form snapshotting, diffing, and mutation/value quiescence.

Every test here runs against fake async page/frame doubles: no browser, no
display, and no Playwright import is required. The frame double *emulates the
in-page contract* — it honours the `captureValues`/`digestKey` options the
scanner passes and refuses to hand back plaintext values unless capture was
explicitly requested — so a scanner that started shipping values over CDP
fails loudly rather than silently passing.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Iterable, Sequence

import pytest

from app.agent.errors import FormSettleTimeout, SnapshotScannerMismatch
from app.agent.form_scanner import (
    DEFAULT_FIRST_CHANGE_TIMEOUT_MS,
    FIELD_IDENTITY_JS,
    PAGE_TRAVERSAL_JS,
    FIELD_SCAN_SCRIPT,
    MUTATION_PROBE_SCRIPT,
    FieldChange,
    FormDiff,
    FormField,
    FormScanner,
    FormSnapshot,
    FrameSkip,
    SettleResult,
    compute_value_digest,
    is_control_filled,
)

MAIN_URL = "https://ats.example.com/apply"


def raw_field(**overrides: Any) -> dict[str, Any]:
    """Build one logical field; the frame double renders it like the script."""
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
        "selectedText": "",
        "shadowDepth": 0,
        "shadowPath": "",
    }
    record.update(overrides)
    return record


class FakeFrame:
    """Async frame double that renders records the way the page script does.

    Scripted sequences advance one entry per call and then repeat their final
    entry forever, so a test only describes the interesting prefix of a
    page's evolution.
    """

    def __init__(
        self,
        url: str,
        *,
        batches: Sequence[Sequence[dict[str, Any]]] | None = None,
        mutations: Sequence[int] | None = None,
        evaluate_error: Exception | None = None,
        name: str = "",
        parent: "FakeFrame | None" = None,
        omit_digest: bool = False,
        never_answers: bool = False,
    ) -> None:
        self.url = url
        self.name = name
        self.parent_frame = parent
        self.child_frames: list[FakeFrame] = []
        if parent is not None:
            parent.child_frames.append(self)
        self._batches: list[Any] = list(batches) if batches is not None else [[]]
        self._mutations: list[int] = list(mutations) if mutations is not None else [0]
        self._evaluate_error = evaluate_error
        self._omit_digest = omit_digest
        self._never_answers = never_answers
        self.scripts: list[str] = []
        self.scan_options: list[Any] = []
        self.scan_calls = 0
        self.probe_calls = 0

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.scripts.append(script)
        if self._never_answers:
            # A frame with no execution context: the driver waits for one
            # that never arrives. Never returns and never raises, so a
            # scanner with no deadline hangs rather than passing slowly.
            await asyncio.Event().wait()
        if self._evaluate_error is not None:
            raise self._evaluate_error
        if script == MUTATION_PROBE_SCRIPT:
            self.probe_calls += 1
            return self._advance(self._mutations)
        if script == FIELD_SCAN_SCRIPT:
            self.scan_calls += 1
            options = args[0] if args else {}
            self.scan_options.append(options)
            return [self._render(record, options) for record in self._advance(self._batches)]
        raise AssertionError(f"unexpected script evaluated: {script[:60]!r}")

    def _render(self, record: Any, options: Any) -> Any:
        """Mimic the in-page script: digest in page, values only on request."""
        if not isinstance(record, dict):
            return record
        rendered = {
            key: value
            for key, value in record.items()
            if key not in {"value", "selectedText"}
        }
        value = str(record.get("value", ""))
        key = bytes.fromhex(str(options.get("digestKey", "")))
        rendered["valueDigest"] = compute_value_digest(key, value)
        rendered["filled"] = is_control_filled(
            str(record.get("tag", "")),
            str(record.get("type", "")),
            value,
            str(record.get("selectedText", "")),
        )
        if options.get("captureValues"):
            rendered["value"] = value
        if self._omit_digest:
            rendered.pop("valueDigest", None)
        return rendered

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
    scanner: FormScanner | None = None,
    **scanner_kwargs: Any,
) -> FormSnapshot:
    page = FakePage(FakeFrame(url, batches=[fields]))
    return await (scanner or FormScanner(**scanner_kwargs)).snapshot(page)


async def diff_of(
    before: Sequence[dict[str, Any]],
    after: Sequence[dict[str, Any]],
    **scanner_kwargs: Any,
) -> FormDiff:
    """Snapshot twice with one scanner (digests are per-scanner secrets)."""
    scanner = FormScanner(**scanner_kwargs)
    page = FakePage(FakeFrame(MAIN_URL, batches=[before, after]))
    first = await scanner.snapshot(page)
    second = await scanner.snapshot(page)
    return first.diff(second)


async def single_field(**overrides: Any) -> FormField:
    snapshot = await snapshot_of([raw_field(**overrides)])
    assert len(snapshot.fields) == 1
    return snapshot.fields[0]


class TestStableFieldKeys:
    async def test_key_is_unchanged_when_only_the_value_changes(self) -> None:
        scanner = FormScanner()
        empty = await snapshot_of([raw_field(value="")], scanner=scanner)
        filled = await snapshot_of([raw_field(value="Ada")], scanner=scanner)

        assert empty.fields[0].key == filled.fields[0].key
        assert empty.fields[0].value_digest != filled.fields[0].value_digest

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
            {"shadowPath": "jobright-host>form"},
            {"shadowDepth": 2},
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


class TestReproducibleKeyDerivation:
    """The key derivation is reachable from outside `snapshot`.

    A field writer resolves a control from the metadata on a `FormField` and
    then has to prove the control it found is the one that field's key
    names — otherwise "the answer was typed into `key`" is a claim about a
    control nobody checked. That check needs the same derivation `snapshot`
    uses, not a second implementation of it.
    """

    def _key_of(self, scanner: FormScanner, field: FormField, ordinal: int = 0) -> str:
        return scanner.stable_key(
            frame_chain=field.frame_chain,
            frame_url=field.frame_url,
            shadow_path=field.shadow_path,
            shadow_depth=field.shadow_depth,
            form=field.form,
            control_id=field.control_id,
            name=field.name,
            field_type=field.field_type,
            label=field.label,
            ordinal=ordinal,
        )

    async def test_it_reproduces_the_key_a_snapshot_assigned(self) -> None:
        scanner = FormScanner()
        field = (await snapshot_of([raw_field()], scanner=scanner)).fields[0]

        assert self._key_of(scanner, field) == field.key

    async def test_it_reproduces_a_shadow_root_fields_key(self) -> None:
        scanner = FormScanner()
        field = (
            await snapshot_of(
                [raw_field(shadowPath="jobright-host#0>form#apply", shadowDepth=2)],
                scanner=scanner,
            )
        ).fields[0]

        assert self._key_of(scanner, field) == field.key

    async def test_it_folds_the_label_the_way_a_snapshot_does(self) -> None:
        """The caller passes the label the page renders, not a folded one."""
        scanner = FormScanner()
        field = (
            await snapshot_of([raw_field(label="First name")], scanner=scanner)
        ).fields[0]
        decorated = dataclasses.replace(field, label="  FIRST NAME *  ")

        assert self._key_of(scanner, decorated) == field.key

    async def test_it_ignores_the_url_fragment_the_way_a_snapshot_does(self) -> None:
        scanner = FormScanner()
        field = (await snapshot_of([raw_field()], scanner=scanner)).fields[0]
        routed = dataclasses.replace(field, frame_url=f"{MAIN_URL}#step-2")

        assert self._key_of(scanner, routed) == field.key

    async def test_an_indistinguishable_duplicate_needs_its_ordinal(self) -> None:
        """Which is why a writer that cannot tell two apart must refuse.

        Two controls with no name, id, label, or form share one identity and
        are told apart only by the order they were scanned in. Re-deriving
        the second one's key from its metadata alone is impossible, so this
        is the case where "exactly one match" is the only safe rule.
        """
        scanner = FormScanner()
        duplicate = raw_field(name="", id="", label="", form="")
        fields = (
            await snapshot_of([dict(duplicate), dict(duplicate)], scanner=scanner)
        ).fields

        assert self._key_of(scanner, fields[0]) == fields[0].key
        assert self._key_of(scanner, fields[1]) != fields[1].key
        assert self._key_of(scanner, fields[1], ordinal=1) == fields[1].key

    async def test_a_different_scanner_derives_the_same_key(self) -> None:
        """Keys are identity, not secrets: only digests are per-scanner."""
        scanner = FormScanner()
        field = (await snapshot_of([raw_field()], scanner=scanner)).fields[0]

        assert self._key_of(FormScanner(), field) == field.key


class TestSharedInPageIdentity:
    """One in-page notion of what a control is, used by every script.

    The scan script decides a control's label, form, id, type, visibility,
    and shadow path, and those six things *are* its stable key. Anything
    else that has to find the same control again — the field writer — must
    ask the same questions in the same way, or it will resolve a control
    whose key it then cannot reproduce.
    """

    def test_the_scan_script_is_built_from_the_shared_helper(self) -> None:
        assert FIELD_IDENTITY_JS in FIELD_SCAN_SCRIPT

    @pytest.mark.parametrize(
        "helper",
        [
            "collectRoots",
            "controlIdentity",
            "controlType",
            "formIdentity",
            "isVisible",
            "labelText",
        ],
    )
    def test_the_helper_owns_every_component_of_a_key(self, helper: str) -> None:
        assert f"const {helper} = " in FIELD_IDENTITY_JS

    def test_the_helper_is_not_a_callable_expression_of_its_own(self) -> None:
        """It is spliced into a script body, not evaluated on its own."""
        assert not FIELD_IDENTITY_JS.strip().startswith("(")

    def test_walking_the_page_is_separable_from_reading_a_control(self) -> None:
        """The page guard needs the walk without the label reader.

        A guard that could read prose would eventually match it, and
        matching "Sign in" in a header abandons applications that were
        perfectly fillable. Handing it a helper that cannot read text at all
        is a stronger guarantee than a review comment asking it not to.
        """
        assert PAGE_TRAVERSAL_JS in FIELD_IDENTITY_JS
        assert "const collectRoots = " in PAGE_TRAVERSAL_JS
        assert "const isVisible = " in PAGE_TRAVERSAL_JS
        assert "labelText" not in PAGE_TRAVERSAL_JS
        assert "textContent" not in PAGE_TRAVERSAL_JS


class TestFrameChainIdentity:
    async def test_identical_sibling_frames_produce_distinct_keys(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[]])
        first = FakeFrame(
            "https://ats.example.com/widget", batches=[[raw_field()]], parent=main
        )
        second = FakeFrame(
            "https://ats.example.com/widget", batches=[[raw_field()]], parent=main
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [first, second]))

        assert len(snapshot.fields) == 2
        assert snapshot.fields[0].key != snapshot.fields[1].key
        assert snapshot.fields[0].frame_chain != snapshot.fields[1].frame_chain

    async def test_frame_chain_is_stable_across_snapshots(self) -> None:
        def build() -> FakePage:
            main = FakeFrame(MAIN_URL, batches=[[]])
            sibling = FakeFrame(
                "https://ats.example.com/widget", batches=[[]], parent=main
            )
            target = FakeFrame(
                "https://ats.example.com/widget", batches=[[raw_field()]], parent=main
            )
            return FakePage(main, [sibling, target])

        first = await FormScanner().snapshot(build())
        second = await FormScanner().snapshot(build())

        assert first.fields[0].key == second.fields[0].key

    async def test_nested_frame_depth_participates_in_identity(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[]])
        middle = FakeFrame("https://ats.example.com/embed", batches=[[]], parent=main)
        deep = FakeFrame(
            "https://ats.example.com/widget", batches=[[raw_field()]], parent=middle
        )
        shallow = FakeFrame(
            "https://ats.example.com/widget", batches=[[raw_field()]], parent=main
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [middle, deep, shallow]))

        keys = {field.key for field in snapshot.fields}
        assert len(keys) == 2

    async def test_collision_ordinals_are_snapshot_wide(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field(name="", id="", label="", form="")]])
        twin = FakeFrame(
            MAIN_URL, batches=[[raw_field(name="", id="", label="", form="")]]
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [twin]))

        assert len(snapshot.fields) == 2
        assert snapshot.fields[0].key != snapshot.fields[1].key

    async def test_shadow_path_and_depth_are_exposed(self) -> None:
        field = await single_field(shadowPath="jobright-host>div", shadowDepth=2)

        assert field.shadow_path == "jobright-host>div"
        assert field.shadow_depth == 2


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

    async def test_malformed_records_are_tolerated(self) -> None:
        snapshot = await snapshot_of([{"tag": "input"}, "not-a-record", None])

        assert len(snapshot.fields) == 1
        assert snapshot.fields[0].tag == "input"
        assert snapshot.fields[0].filled is False


class TestValuePrivacy:
    async def test_values_are_never_requested_by_default(self) -> None:
        frame = FakeFrame(MAIN_URL, batches=[[raw_field(value="ada@example.com")]])

        snapshot = await FormScanner().snapshot(FakePage(frame))

        assert frame.scan_options[0]["captureValues"] is False
        assert snapshot.fields[0].value is None
        assert snapshot.fields[0].value_digest

    async def test_capture_values_opt_in_returns_values(self) -> None:
        frame = FakeFrame(MAIN_URL, batches=[[raw_field(value="ada@example.com")]])

        snapshot = await FormScanner(capture_values=True).snapshot(FakePage(frame))

        assert frame.scan_options[0]["captureValues"] is True
        assert snapshot.fields[0].value == "ada@example.com"

    async def test_digest_is_computed_in_page_with_the_scanner_key(self) -> None:
        frame = FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]])
        scanner = FormScanner()

        snapshot = await scanner.snapshot(FakePage(frame))

        key = bytes.fromhex(frame.scan_options[0]["digestKey"])
        assert snapshot.fields[0].value_digest == compute_value_digest(key, "Ada")

    async def test_digest_key_is_random_per_scanner_instance(self) -> None:
        one = await snapshot_of([raw_field(value="Ada")])
        two = await snapshot_of([raw_field(value="Ada")])

        assert one.fields[0].value_digest != two.fields[0].value_digest

    async def test_digest_is_stable_within_one_scanner_instance(self) -> None:
        scanner = FormScanner()
        one = await snapshot_of([raw_field(value="Ada")], scanner=scanner)
        two = await snapshot_of([raw_field(value="Ada")], scanner=scanner)

        assert one.fields[0].value_digest == two.fields[0].value_digest

    async def test_digest_differs_for_different_values(self) -> None:
        scanner = FormScanner()
        one = await snapshot_of([raw_field(value="Ada")], scanner=scanner)
        other = await snapshot_of([raw_field(value="Grace")], scanner=scanner)

        assert one.fields[0].value_digest != other.fields[0].value_digest

    async def test_digest_never_leaks_the_raw_value(self) -> None:
        field = await single_field(value="ada@example.com")

        assert "ada@example.com" not in field.value_digest
        assert "ada@example.com" not in repr(field)

    def test_digest_helper_is_keyed_not_a_bare_hash(self) -> None:
        first = compute_value_digest(b"key-one", "Ada")
        second = compute_value_digest(b"key-two", "Ada")

        assert first != second
        assert first == compute_value_digest(b"key-one", "Ada")

    def test_scan_script_only_returns_values_when_asked(self) -> None:
        assert "captureValues" in FIELD_SCAN_SCRIPT
        assert "digestKey" in FIELD_SCAN_SCRIPT

    def test_scan_script_digests_in_page_with_hmac(self) -> None:
        assert "crypto.subtle" in FIELD_SCAN_SCRIPT
        assert "HMAC" in FIELD_SCAN_SCRIPT
        assert "SHA-256" in FIELD_SCAN_SCRIPT


class TestScannerIdentity:
    async def test_snapshot_is_stamped_with_the_scanner_that_took_it(self) -> None:
        scanner = FormScanner()

        snapshot = await snapshot_of([raw_field()], scanner=scanner)

        assert snapshot.scanner_id == scanner.scanner_id
        assert snapshot.scanner_id

    async def test_scanner_ids_differ_between_instances(self) -> None:
        assert FormScanner().scanner_id != FormScanner().scanner_id

    async def test_scanners_sharing_a_digest_key_are_compatible(self) -> None:
        key = b"a-shared-key-for-two-scanners---"
        one = FormScanner(digest_key=key)
        two = FormScanner(digest_key=key)

        before = await snapshot_of([raw_field(value="")], scanner=one)
        after = await snapshot_of([raw_field(value="Ada")], scanner=two)

        assert one.scanner_id == two.scanner_id
        assert before.diff(after).has_changes is True

    async def test_diffing_across_scanners_fails_loudly(self) -> None:
        before = await snapshot_of([raw_field(value="")])
        after = await snapshot_of([raw_field(value="")])

        with pytest.raises(SnapshotScannerMismatch) as excinfo:
            before.diff(after)

        message = str(excinfo.value)
        assert before.scanner_id in message
        assert after.scanner_id in message

    async def test_unstamped_snapshots_stay_comparable(self) -> None:
        stamped = await snapshot_of([raw_field()])

        assert stamped.diff(FormSnapshot()).removed
        assert FormSnapshot().diff(stamped).added

    async def test_settling_against_a_foreign_baseline_scans_nothing(self) -> None:
        previous = await snapshot_of([raw_field()])
        frame = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        scanner = FormScanner()

        with pytest.raises(SnapshotScannerMismatch):
            await scanner.wait_for_settle(FakePage(frame), previous)

        assert frame.scan_calls == 0


class TestMissingDigests:
    async def test_records_without_a_digest_are_not_hashed_as_empty(self) -> None:
        """Digesting a value the page never sent would give every field the
        same digest, making a filled form look unchanged."""
        frame = FakeFrame(
            MAIN_URL,
            batches=[[raw_field(value="Ada"), raw_field(name="email", value="a@b.co")]],
            omit_digest=True,
        )

        snapshot = await FormScanner().snapshot(FakePage(frame))

        assert snapshot.fields == ()
        assert len(snapshot.skipped_frames) == 1
        assert "digest" in snapshot.skipped_frames[0].reason
        assert snapshot.coverage_complete is False

    async def test_a_digestless_frame_does_not_hide_the_others(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        broken = FakeFrame(
            "https://ats.example.com/embed",
            batches=[[raw_field(name="broken")]],
            parent=main,
            omit_digest=True,
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [broken]))

        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert snapshot.skipped_frames[0].frame_url == "https://ats.example.com/embed"

    async def test_captured_values_are_digested_locally_when_the_page_omits_it(self) -> None:
        frame = FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]], omit_digest=True)
        scanner = FormScanner(capture_values=True)

        snapshot = await scanner.snapshot(FakePage(frame))

        key = bytes.fromhex(frame.scan_options[0]["digestKey"])
        assert snapshot.fields[0].value_digest == compute_value_digest(key, "Ada")
        assert snapshot.skipped_frames == ()


class TestFrameTraversal:
    async def test_aggregates_fields_from_every_same_origin_frame(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        child = FakeFrame(
            "https://ats.example.com/embedded",
            batches=[[raw_field(name="resume", id="resume", label="Resume")]],
            parent=main,
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
            "https://tracker.example.net/pixel",
            batches=[[raw_field(name="hidden")]],
            parent=main,
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
        other_port = FakeFrame("https://ats.example.com:8443/embed", parent=main)
        insecure = FakeFrame("http://ats.example.com/embed", parent=main)
        snapshot = await FormScanner().snapshot(FakePage(main, [other_port, insecure]))

        assert other_port.scan_calls == 0
        assert insecure.scan_calls == 0
        assert len(snapshot.skipped_frames) == 2

    async def test_frame_evaluation_failure_is_recorded_not_raised(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        detached = FakeFrame(
            "https://ats.example.com/detached",
            evaluate_error=RuntimeError("frame was detached"),
            parent=main,
        )
        snapshot = await FormScanner().snapshot(FakePage(main, [detached]))

        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert len(snapshot.skipped_frames) == 1
        assert "frame was detached" in snapshot.skipped_frames[0].reason

    async def test_a_frame_that_never_answers_is_recorded_and_left_behind(
        self,
    ) -> None:
        """A frame with no execution context must not stall a staging run.

        An `about:blank` iframe still notionally navigating never gains a
        context, and the driver waits for one indefinitely. Unbounded, one
        such frame holds the tab, the worker's renewing lease, and every
        listing queued behind it — and the operator sees a run that is
        neither finished nor failed.

        The frame is reported as unscanned, which is already a coverage gap
        and therefore a blocking reason at the approval gate, so nothing is
        submitted on the strength of a form that was only partly read.
        """
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        pending = FakeFrame(
            "https://ats.example.com/pending", never_answers=True, parent=main
        )

        snapshot = await asyncio.wait_for(
            FormScanner(frame_timeout_ms=20).snapshot(FakePage(main, [pending])),
            timeout=2,
        )

        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert len(snapshot.skipped_frames) == 1
        assert snapshot.skipped_frames[0].frame_url == "https://ats.example.com/pending"
        assert "did not answer" in snapshot.skipped_frames[0].reason

    async def test_page_without_frames_collection_is_scanned_directly(self) -> None:
        page = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        snapshot = await FormScanner().snapshot(page)

        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert page.scan_calls == 1


class TestInheritedOriginResolution:
    async def test_about_blank_child_of_the_main_frame_is_scanned(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        blank = FakeFrame("about:blank", batches=[[raw_field(name="blank_field")]], parent=main)
        srcdoc = FakeFrame(
            "about:srcdoc", batches=[[raw_field(name="srcdoc_field")]], parent=main
        )
        empty = FakeFrame("", batches=[[raw_field(name="empty_field")]], parent=main)

        snapshot = await FormScanner().snapshot(FakePage(main, [blank, srcdoc, empty]))

        assert {field.name for field in snapshot.fields} == {
            "first_name",
            "blank_field",
            "srcdoc_field",
            "empty_field",
        }
        assert snapshot.skipped_frames == ()

    async def test_blob_frame_inherits_a_same_origin_parent(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[]])
        embed = FakeFrame("https://ats.example.com/embed", batches=[[]], parent=main)
        blob = FakeFrame(
            "blob:https://ats.example.com/abc",
            batches=[[raw_field(name="blob_field")]],
            parent=embed,
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [embed, blob]))

        assert [field.name for field in snapshot.fields] == ["blob_field"]

    async def test_inherited_frame_under_a_cross_origin_parent_is_never_scanned(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        foreign = FakeFrame("https://tracker.example.net/pixel", batches=[[]], parent=main)
        inherited = FakeFrame(
            "about:blank", batches=[[raw_field(name="third_party")]], parent=foreign
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [foreign, inherited]))

        assert inherited.scan_calls == 0
        assert [field.name for field in snapshot.fields] == ["first_name"]
        skipped = {skip.frame_url for skip in snapshot.skipped_frames}
        assert "about:blank" in skipped

    async def test_inherited_frame_without_a_resolvable_parent_is_skipped(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        orphan = FakeFrame("about:blank", batches=[[raw_field(name="orphan_field")]])

        snapshot = await FormScanner().snapshot(FakePage(main, [orphan]))

        assert orphan.scan_calls == 0
        assert [field.name for field in snapshot.fields] == ["first_name"]
        assert "origin" in snapshot.skipped_frames[0].reason

    async def test_main_frame_is_always_scanned(self) -> None:
        main = FakeFrame("about:blank", batches=[[raw_field(name="main_field")]])
        child = FakeFrame(
            "https://ats.example.com/embed", batches=[[raw_field(name="child")]], parent=main
        )

        snapshot = await FormScanner().snapshot(FakePage(main, [child]))

        assert "main_field" in {field.name for field in snapshot.fields}
        assert child.scan_calls == 0


class TestTraversalScript:
    def test_scan_script_is_a_callable_javascript_expression(self) -> None:
        assert FIELD_SCAN_SCRIPT.strip().startswith("(")
        assert "=>" in FIELD_SCAN_SCRIPT

    def test_scan_script_walks_open_shadow_roots(self) -> None:
        assert "shadowRoot" in FIELD_SCAN_SCRIPT
        assert "shadowDepth" in FIELD_SCAN_SCRIPT
        assert "shadowPath" in FIELD_SCAN_SCRIPT

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

    def test_scan_script_normalizes_placeholder_selects(self) -> None:
        assert "selectedIndex" in FIELD_SCAN_SCRIPT
        assert "PLACEHOLDER" in FIELD_SCAN_SCRIPT.upper()

    def test_mutation_probe_observes_dom_and_value_events(self) -> None:
        assert "MutationObserver" in MUTATION_PROBE_SCRIPT
        assert "subtree" in MUTATION_PROBE_SCRIPT
        assert "characterData" in MUTATION_PROBE_SCRIPT
        assert "input" in MUTATION_PROBE_SCRIPT
        assert "change" in MUTATION_PROBE_SCRIPT

    def test_mutation_probe_installs_itself_once(self) -> None:
        assert "__jobrightScanState" in MUTATION_PROBE_SCRIPT


class TestControlFilledRule:
    @pytest.mark.parametrize(
        "selected_text",
        ["Select one", "  please choose  ", "-", "--", "N/A", "None", "Choose an option"],
    )
    def test_placeholder_selects_count_as_empty(self, selected_text: str) -> None:
        assert is_control_filled("select", "select-one", selected_text, selected_text) is False

    def test_real_selection_counts_as_filled(self) -> None:
        assert is_control_filled("select", "select-one", "yes", "Yes") is True

    def test_empty_select_value_counts_as_empty(self) -> None:
        assert is_control_filled("select", "select-one", "", "") is False

    def test_text_inputs_use_the_trimmed_value(self) -> None:
        assert is_control_filled("input", "text", "   ") is False
        assert is_control_filled("input", "text", " Ada ") is True


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

    async def test_placeholder_select_is_a_required_gap(self) -> None:
        snapshot = await snapshot_of(
            [
                raw_field(
                    tag="select",
                    type="select-one",
                    name="sponsorship",
                    id="sponsorship",
                    required=True,
                    value="Select one",
                    selectedText="Select one",
                ),
                raw_field(
                    tag="select",
                    type="select-one",
                    name="country",
                    id="country",
                    required=True,
                    value="us",
                    selectedText="United States",
                ),
            ]
        )

        assert [field.name for field in snapshot.required_gaps()] == ["sponsorship"]

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

    async def test_password_inputs_are_never_free_text_gaps(self) -> None:
        snapshot = await snapshot_of(
            [
                raw_field(
                    name="password", id="password", type="password", required=True, value=""
                )
            ]
        )

        assert snapshot.unanswered_free_text() == ()
        assert snapshot.fields[0].free_text is False
        assert [field.name for field in snapshot.required_gaps()] == ["password"]

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
    async def test_newly_filled_fields_are_attributed(self) -> None:
        diff = await diff_of(
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
        diff = await diff_of([raw_field(value="Ada")], [raw_field(value="Ada Lovelace")])

        assert len(diff.changed) == 1
        assert diff.newly_filled == ()
        assert diff.changed[0].before.value_digest != diff.changed[0].after.value_digest

    async def test_cleared_fields_are_reported(self) -> None:
        diff = await diff_of([raw_field(value="Ada")], [raw_field(value="")])

        assert [field.name for field in diff.cleared] == ["first_name"]
        assert diff.changed[0].became_empty is True

    async def test_unchanged_fields_are_absent_from_the_diff(self) -> None:
        diff = await diff_of([raw_field(value="Ada")], [raw_field(value="Ada")])

        assert diff.changed == ()
        assert diff.newly_filled == ()
        assert diff.has_changes is False

    async def test_added_and_removed_fields_are_reported(self) -> None:
        diff = await diff_of(
            [raw_field()], [raw_field(name="visa_status", id="visa", label="Visa status")]
        )

        assert [field.name for field in diff.added] == ["visa_status"]
        assert [field.name for field in diff.removed] == ["first_name"]

    async def test_still_empty_required_fields_come_from_the_after_snapshot(self) -> None:
        diff = await diff_of(
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
        record = raw_field(tag="textarea", type="textarea", name="cover", id="cover", value="")
        diff = await diff_of([record], [dict(record)])

        assert [field.name for field in diff.unanswered_free_text] == ["cover"]

    async def test_has_changes_is_true_when_anything_was_filled(self) -> None:
        diff = await diff_of([raw_field(value="")], [raw_field(value="Ada")])

        assert diff.has_changes is True


class TestCoverageDiagnostics:
    async def test_snapshot_reports_incomplete_coverage(self) -> None:
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        foreign = FakeFrame("https://tracker.example.net/pixel", parent=main)

        complete = await FormScanner().snapshot(FakePage(main))
        partial = await FormScanner().snapshot(FakePage(main, [foreign]))

        assert complete.coverage_complete is True
        assert partial.coverage_complete is False

    async def test_diff_propagates_skipped_frames_from_both_snapshots(self) -> None:
        scanner = FormScanner()
        main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        before = await scanner.snapshot(FakePage(main))

        after_main = FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]])
        foreign = FakeFrame("https://tracker.example.net/pixel", parent=after_main)
        after = await scanner.snapshot(FakePage(after_main, [foreign]))

        diff = before.diff(after)

        assert diff.coverage_complete is False
        assert [skip.frame_url for skip in diff.skipped_frames] == [
            "https://tracker.example.net/pixel"
        ]

    async def test_diff_reports_complete_coverage_when_nothing_was_skipped(self) -> None:
        diff = await diff_of([raw_field(value="")], [raw_field(value="Ada")])

        assert diff.coverage_complete is True
        assert diff.skipped_frames == ()

    async def test_skips_from_the_baseline_snapshot_are_not_lost(self) -> None:
        scanner = FormScanner()
        before_main = FakeFrame(MAIN_URL, batches=[[raw_field()]])
        detached = FakeFrame(
            "https://ats.example.com/gone",
            evaluate_error=RuntimeError("frame was detached"),
            parent=before_main,
        )
        before = await scanner.snapshot(FakePage(before_main, [detached]))
        after = await scanner.snapshot(FakePage(FakeFrame(MAIN_URL, batches=[[raw_field()]])))

        diff = before.diff(after)

        assert diff.coverage_complete is False
        assert "https://ats.example.com/gone" in {skip.frame_url for skip in diff.skipped_frames}


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
        assert isinstance(diff.skipped_frames, tuple)
        assert isinstance(snapshot.required_gaps(), tuple)


class TestSettleDetection:
    def _scanner(self, clock: FakeClock, **kwargs: Any) -> FormScanner:
        return FormScanner(
            clock=clock.monotonic, sleep=clock.sleep, poll_interval_ms=100, **kwargs
        )

    async def _baseline(
        self, scanner: FormScanner, fields: Sequence[dict[str, Any]], page: FakePage
    ) -> FormSnapshot:
        return await scanner.snapshot(FakePage(FakeFrame(page.url, batches=[fields])))

    async def test_waits_for_the_first_change_before_requiring_quiet(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        baseline_fields = [raw_field(value="")]
        page = FakePage(
            FakeFrame(
                MAIN_URL,
                batches=[baseline_fields, baseline_fields, [raw_field(value="Ada")]],
                mutations=[0, 0, 1],
            )
        )
        previous = await self._baseline(scanner, baseline_fields, page)

        result = await scanner.wait_for_settle(
            page,
            previous,
            quiet_ms=250,
            timeout_ms=30000,
            first_change_timeout_ms=5000,
        )

        assert result.observed_change is True
        assert result.settled is True
        assert result.diff.has_changes is True
        # First change is visible at t=200ms; quiet must then be observed on top.
        assert result.waited_ms >= 200 + 250

    async def test_a_frame_that_never_answers_does_not_hold_the_settle_wait(
        self,
    ) -> None:
        """The mutation probe runs on every poll, once per frame.

        A frame with no execution context never answers it, so an unbounded
        probe cannot even reach the settle timeout that exists to end this
        wait — the loop stops inside an await rather than at its deadline.
        The frame contributes no mutation count, which is what the docstring
        already promises for a frame that cannot be probed.
        """
        clock = FakeClock()
        scanner = self._scanner(clock, frame_timeout_ms=20)
        fields = [raw_field(value="Ada")]
        main = FakeFrame(MAIN_URL, batches=[fields])
        pending = FakeFrame(
            "https://ats.example.com/pending", never_answers=True, parent=main
        )
        page = FakePage(main, [pending])
        previous = await self._baseline(scanner, fields, page)

        result = await asyncio.wait_for(
            scanner.wait_for_settle(
                page, previous, quiet_ms=200, timeout_ms=30000, first_change_timeout_ms=1000
            ),
            timeout=5,
        )

        assert result.settled is True
        assert result.mutations == 0

    async def test_unchanged_page_settles_only_after_the_first_change_window(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        fields = [raw_field(value="Ada")]
        page = FakePage(FakeFrame(MAIN_URL, batches=[fields]))
        previous = await self._baseline(scanner, fields, page)

        result = await scanner.wait_for_settle(
            page, previous, quiet_ms=200, timeout_ms=30000, first_change_timeout_ms=1000
        )

        assert result.observed_change is False
        assert result.settled is True
        assert result.diff.has_changes is False
        assert result.waited_ms >= 1000

    async def test_late_quiet_is_still_required_after_a_change(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        baseline_fields = [raw_field(value="")]
        page = FakePage(
            FakeFrame(
                MAIN_URL,
                batches=[
                    baseline_fields,
                    [raw_field(value="A")],
                    [raw_field(value="Ad")],
                    [raw_field(value="Ada")],
                ],
                mutations=[0, 1, 2, 3],
            )
        )
        previous = await self._baseline(scanner, baseline_fields, page)

        result = await scanner.wait_for_settle(
            page, previous, quiet_ms=250, timeout_ms=30000, first_change_timeout_ms=5000
        )

        assert result.snapshot.fields[0].filled is True
        assert result.waited_ms >= 300 + 250

    async def test_dom_mutations_without_value_changes_delay_settling(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        baseline_fields = [raw_field(value="")]
        changed = [raw_field(value="Ada")]
        page = FakePage(
            FakeFrame(MAIN_URL, batches=[changed], mutations=[0, 1, 2, 3, 4, 4])
        )
        previous = await self._baseline(scanner, baseline_fields, page)

        result = await scanner.wait_for_settle(
            page, previous, quiet_ms=250, timeout_ms=30000, first_change_timeout_ms=5000
        )

        assert result.waited_ms >= 250 + 4 * 100

    async def test_waiting_uses_repeated_polls_not_one_fixed_sleep(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        fields = [raw_field(value="Ada")]
        page = FakePage(FakeFrame(MAIN_URL, batches=[fields]))
        previous = await self._baseline(scanner, fields, page)

        await scanner.wait_for_settle(
            page, previous, quiet_ms=200, timeout_ms=5000, first_change_timeout_ms=500
        )

        assert len(clock.sleeps) >= 2
        assert set(clock.sleeps) == {0.1}

    async def test_returns_the_diff_against_the_previous_snapshot(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        baseline_fields = [raw_field(value="")]
        page = FakePage(FakeFrame(MAIN_URL, batches=[[raw_field(value="Ada")]]))
        previous = await self._baseline(scanner, baseline_fields, page)

        result = await scanner.wait_for_settle(
            page, previous, quiet_ms=200, timeout_ms=5000, first_change_timeout_ms=2000
        )

        assert isinstance(result.diff, FormDiff)
        assert [field.name for field in result.diff.newly_filled] == ["first_name"]

    async def test_probes_mutations_in_every_same_origin_frame(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        fields = [raw_field(value="Ada")]
        main = FakeFrame(MAIN_URL, batches=[fields])
        child = FakeFrame("https://ats.example.com/embed", batches=[[]], parent=main)
        foreign = FakeFrame("https://tracker.example.net/pixel", parent=main)
        page = FakePage(main, [child, foreign])
        previous = await self._baseline(scanner, fields, page)

        await scanner.wait_for_settle(
            page, previous, quiet_ms=200, timeout_ms=5000, first_change_timeout_ms=300
        )

        assert main.probe_calls >= 1
        assert child.probe_calls >= 1
        assert foreign.probe_calls == 0

    async def test_raises_when_the_page_never_goes_quiet(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        baseline_fields = [raw_field(value="")]
        page = FakePage(
            FakeFrame(
                MAIN_URL,
                batches=[[raw_field(value=f"v{index}")] for index in range(200)],
                mutations=list(range(200)),
            )
        )
        previous = await self._baseline(scanner, baseline_fields, page)

        with pytest.raises(FormSettleTimeout) as excinfo:
            await scanner.wait_for_settle(
                page,
                previous,
                quiet_ms=300,
                timeout_ms=1000,
                first_change_timeout_ms=5000,
            )

        error = excinfo.value
        assert error.quiet_ms == 300
        assert error.timeout_ms == 1000
        assert isinstance(error.snapshot, FormSnapshot)
        assert "1000" in str(error)

    async def test_timeout_carries_an_unsettled_result_with_the_observed_diff(self) -> None:
        clock = FakeClock()
        scanner = self._scanner(clock)
        baseline_fields = [raw_field(value="")]
        page = FakePage(
            FakeFrame(
                MAIN_URL,
                batches=[[raw_field(value=f"v{index}")] for index in range(200)],
                mutations=list(range(200)),
            )
        )
        previous = await self._baseline(scanner, baseline_fields, page)

        with pytest.raises(FormSettleTimeout) as excinfo:
            await scanner.wait_for_settle(
                page, previous, quiet_ms=300, timeout_ms=1000, first_change_timeout_ms=5000
            )

        result = excinfo.value.result
        assert isinstance(result, SettleResult)
        assert result.settled is False
        assert result.observed_change is True
        assert excinfo.value.diff.has_changes is True

    async def test_first_change_window_has_a_documented_default(self) -> None:
        assert DEFAULT_FIRST_CHANGE_TIMEOUT_MS > 0
