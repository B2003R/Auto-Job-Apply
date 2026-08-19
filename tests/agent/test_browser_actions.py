"""Tests for the three browser-facing components that act on a real page.

Every test here runs against page and frame doubles: no browser, no
display, and no Playwright import. What the doubles emulate is the *script
contract* — each in-page script is answered by name, and a double raises if
it is asked to run a script the component under test should not have
reached for — so a writer that typed before resolving, a guard that matched
prose, or a submitter that clicked before checking its authorisation fails
loudly rather than silently passing.

What the doubles cannot prove is that the JavaScript is correct against a
real DOM. That is what `tests/integration/test_stub_extension.py` is for.
Everything the scripts decide *and* Python has to agree with — which
accessible names count as a final submit, what a confirmation reads like —
is a shared Python constant here rather than a string duplicated into
JavaScript, so those rules are unit-tested rather than asserted about.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import pytest

from app.agent.browser_actions import (
    ACTIVE_CAPTCHA_JS,
    CAPTCHA_CHALLENGE_SELECTORS,
    CAPTCHA_WIDGET_SELECTORS,
    CONFIRMATION_TEXT,
    FINAL_SUBMIT_PHRASES,
    NEVER_SUBMIT_NAME,
    GUARD_SCRIPT,
    PASSIVE_CAPTCHA_SELECTORS,
    HUMAN_ONLY_FIELD_TYPES,
    SUBMIT_CANDIDATES_JS,
    SUBMIT_COUNT_SCRIPT,
    SUBMIT_RESOLVE_SCRIPT,
    SUBMIT_STATE_SCRIPT,
    SUBMIT_TARGET_SCRIPT,
    SUPPORTED_FIELD_TYPES,
    VALIDATION_REGION_SELECTORS,
    VALIDATION_TEXT,
    WRITE_COUNT_SCRIPT,
    WRITE_SCRIPT,
    ConfirmationRegion,
    PageState,
    PlaywrightFieldWriter,
    PlaywrightPageGuard,
    PlaywrightSubmitter,
    is_confirmation_text,
    is_final_submit_name,
    normalize_control_name,
    submission_verdict,
)
from app.agent.errors import (
    CaptchaEncountered,
    FieldNotUniquelyResolved,
    FieldProvenanceMismatch,
    FieldWriteNotVerified,
    FieldWriteRefused,
    FinalSubmitControlAmbiguous,
    FinalSubmitControlNotActionable,
    FinalSubmitControlNotFound,
    LoginWallEncountered,
    SubmitAlreadyAttempted,
    SubmitNotAuthorized,
    UnsupportedFieldControl,
)
from app.agent.form_scanner import (
    FIELD_IDENTITY_JS,
    PAGE_TRAVERSAL_JS,
    FormField,
    FormScanner,
)
from app.agent.graph import SubmitAuthorization, SubmitPermit
from app.storage.models import ApprovalDecision

MAIN_URL = "https://ats.example.com/apply"
ANSWER = "Rivera"


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


async def _resolved(answer: Any) -> Any:
    """Allow a response to be a coroutine, for the frames that never answer."""
    if inspect.isawaitable(answer):
        return await answer
    return answer


class FakeFrame:
    """A frame that answers exactly the scripts it was given, and no others."""

    def __init__(
        self,
        url: str = MAIN_URL,
        responses: Mapping[str, Any] | None = None,
        *,
        name: str = "",
        parent: "FakeFrame | None" = None,
        evaluate_error: BaseException | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self.parent_frame = parent
        self.child_frames: list[FakeFrame] = []
        if parent is not None:
            parent.child_frames.append(self)
        self._responses = dict(responses or {})
        self._error = evaluate_error
        self.calls: list[tuple[str, Any]] = []

    def _answer(self, script: str, argument: Any) -> Any:
        if self._error is not None:
            raise self._error
        if script not in self._responses:
            raise AssertionError(
                f"frame {self.url} was asked to run a script it should not have "
                f"been asked for: {script[:60]!r}"
            )
        self.calls.append((script, argument))
        handler = self._responses[script]
        return handler(argument) if callable(handler) else handler

    async def evaluate(self, script: str, argument: Any = None) -> Any:
        return await _resolved(self._answer(script, argument))

    async def evaluate_handle(self, script: str, argument: Any = None) -> Any:
        return await _resolved(self._answer(script, argument))

    def arguments_for(self, script: str) -> list[Any]:
        return [argument for name, argument in self.calls if name == script]

    def times_run(self, script: str) -> int:
        return len(self.arguments_for(script))


class FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.presses = 0

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y))

    async def down(self) -> None:
        self.presses += 1

    async def up(self) -> None:
        pass


class FakeElement:
    """A resolved control handle with a plausible bounding box.

    `click` is the driver's own trusted click, which is where actionability
    and hit-target verification happen; `trial=True` is that check with the
    press withheld. Both are counted separately so a test can say the check
    happened *and* that nothing was pressed.
    """

    def __init__(
        self,
        box: Mapping[str, float] | None = None,
        *,
        boxless: bool = False,
        trial_error: BaseException | None = None,
        click_error: BaseException | None = None,
    ) -> None:
        self.box = dict(box or {"x": 10.0, "y": 20.0, "width": 120.0, "height": 36.0})
        self.boxless = boxless
        self.disposed = False
        self.scrolled = False
        self.trials = 0
        self.presses = 0
        self.order: list[str] = []
        self._trial_error = trial_error
        self._click_error = click_error

    def as_element(self) -> "FakeElement":
        return self

    async def bounding_box(self) -> dict[str, float] | None:
        return None if self.boxless else dict(self.box)

    async def scroll_into_view_if_needed(self) -> None:
        self.scrolled = True

    async def click(self, *, trial: bool = False, **_kwargs: Any) -> None:
        if trial:
            self.trials += 1
            self.order.append("trial")
            if self._trial_error is not None:
                raise self._trial_error
            return
        self.presses += 1
        self.order.append("click")
        if self._click_error is not None:
            raise self._click_error

    async def dispose(self) -> None:
        self.disposed = True


class FakePage:
    def __init__(
        self,
        main: FakeFrame | None = None,
        others: Iterable[FakeFrame] = (),
        *,
        viewport: Mapping[str, float] | None = None,
    ) -> None:
        self.main_frame = main if main is not None else FakeFrame()
        self.frames = [self.main_frame, *others]
        self.url = self.main_frame.url
        self.mouse = FakeMouse()
        self.viewport_size = dict(viewport or {"width": 1280.0, "height": 900.0})

    async def evaluate(self, script: str, argument: Any = None) -> Any:
        return await self.main_frame.evaluate(script, argument)

    async def evaluate_handle(self, script: str, argument: Any = None) -> Any:
        return await self.main_frame.evaluate_handle(script, argument)


class ForbiddenPage:
    """A page that fails the test if anything at all is asked of it."""

    url = MAIN_URL
    frames: list[Any] = []

    async def evaluate(self, script: str, argument: Any = None) -> Any:
        raise AssertionError("the page was touched when it should not have been")

    evaluate_handle = evaluate


async def no_sleep(_seconds: float) -> None:
    return None


class StepClock:
    """A monotonic clock that only moves when a sleep is awaited."""

    def __init__(self, step: float = 0.05) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds or self.step


# --------------------------------------------------------------------------
# Field descriptors
# --------------------------------------------------------------------------


def field(
    *,
    key: str = "field-key",
    field_type: str = "text",
    tag: str = "input",
    frame_url: str = MAIN_URL,
    frame_chain: str = "",
    shadow_path: str = "",
    shadow_depth: int = 0,
    form: str = "application-form",
    control_id: str = "last-name",
    name: str = "last_name",
    label: str = "Last name *",
) -> FormField:
    return FormField(
        key=key,
        frame_url=frame_url,
        form=form,
        control_id=control_id,
        name=name,
        field_type=field_type,
        label=label,
        tag=tag,
        required=True,
        disabled=False,
        visible=True,
        filled=False,
        free_text=True,
        value_digest="digest",
        frame_chain=frame_chain,
        shadow_path=shadow_path,
        shadow_depth=shadow_depth,
    )


def resolved_identity(target: FormField) -> dict[str, Any]:
    """What the write script reports about the control it resolved."""
    return {
        "shadowPath": target.shadow_path,
        "shadowDepth": target.shadow_depth,
        "form": target.form,
        "id": target.control_id,
        "name": target.name,
        "type": target.field_type,
        "label": target.label,
    }


def writing_frame(
    target: FormField,
    *,
    url: str = MAIN_URL,
    count: int = 1,
    matched: bool = True,
    identity: Mapping[str, Any] | None = None,
    parent: FakeFrame | None = None,
    name: str = "",
) -> FakeFrame:
    """A frame that resolves exactly `count` controls and writes into one."""
    resolved = dict(identity or resolved_identity(target))
    return FakeFrame(
        url,
        {
            WRITE_COUNT_SCRIPT: {"count": count, "identity": resolved},
            WRITE_SCRIPT: {
                "ok": True,
                "reason": "",
                "count": count,
                "matched": matched,
                "identity": resolved,
            },
        },
        parent=parent,
        name=name,
    )


def keyed_field(scanner: FormScanner, **overrides: Any) -> FormField:
    """A field whose key is the one this scanner would really have given it."""
    target = field(**overrides)
    return dataclasses.replace(
        target,
        key=scanner.stable_key(
            frame_chain=target.frame_chain,
            frame_url=target.frame_url,
            shadow_path=target.shadow_path,
            shadow_depth=target.shadow_depth,
            form=target.form,
            control_id=target.control_id,
            name=target.name,
            field_type=target.field_type,
            label=target.label,
        ),
    )


# --------------------------------------------------------------------------
# The field writer
# --------------------------------------------------------------------------


class TestWhichControlsMayBeTyped:
    """An allowlist, checked before the page is touched at all.

    A file input cannot be satisfied by text, a password field would take a
    credential out of a plaintext answers file and put it into a page, and a
    checkbox or radio is a consent the applicant gives. The gap filler
    already routes all three to a human; this is the second, independent
    refusal, because a writer that trusted its caller's classification would
    only be as safe as that classification.
    """

    @pytest.mark.parametrize(
        "field_type",
        ["file", "password", "checkbox", "radio", "select-multiple", "contenteditable"],
    )
    async def test_a_human_only_control_is_refused_without_touching_the_page(
        self, field_type: str
    ) -> None:
        writer = PlaywrightFieldWriter()

        assert (
            await writer.write(ForbiddenPage(), field(field_type=field_type), ANSWER)
            is False
        )

    @pytest.mark.parametrize("field_type", ["file", "password", "checkbox"])
    def test_the_refusal_is_typed_and_names_the_control_kind(
        self, field_type: str
    ) -> None:
        assert field_type in HUMAN_ONLY_FIELD_TYPES
        assert field_type not in SUPPORTED_FIELD_TYPES

    @pytest.mark.parametrize(
        "field_type,tag",
        [
            ("text", "input"),
            ("email", "input"),
            ("tel", "input"),
            ("url", "input"),
            ("search", "input"),
            ("number", "input"),
            ("textarea", "textarea"),
            ("select-one", "select"),
        ],
    )
    async def test_a_supported_control_reaches_the_page(
        self, field_type: str, tag: str
    ) -> None:
        target = field(field_type=field_type, tag=tag)
        frame = writing_frame(target)

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER)

    async def test_the_two_sets_never_overlap(self) -> None:
        assert not (HUMAN_ONLY_FIELD_TYPES & SUPPORTED_FIELD_TYPES)


class TestResolvingExactlyOneControl:
    """One match, or nothing is typed.

    "Type the answer into the control this key names" is only a meaningful
    instruction if exactly one control on the page answers to that
    description. Two matches means the writer would be guessing which of
    someone's answers goes where, and zero means the form is not the one
    that was scanned.
    """

    async def test_one_match_is_written(self) -> None:
        target = field()
        frame = writing_frame(target)

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER)
        assert frame.times_run(WRITE_SCRIPT) == 1

    async def test_no_match_is_refused_and_nothing_is_typed(self) -> None:
        target = field()
        frame = FakeFrame(MAIN_URL, {WRITE_COUNT_SCRIPT: {"count": 0}})

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER) is False
        assert frame.times_run(WRITE_SCRIPT) == 0

    async def test_two_matches_in_one_frame_are_refused(self) -> None:
        target = field()
        frame = FakeFrame(MAIN_URL, {WRITE_COUNT_SCRIPT: {"count": 2}})

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER) is False
        assert frame.times_run(WRITE_SCRIPT) == 0

    async def test_one_match_in_each_of_two_candidate_frames_is_refused(self) -> None:
        """Two identical widgets are exactly when a key stops being unique."""
        target = field(frame_url="https://ats.example.com/widget")
        main = FakeFrame(MAIN_URL, {})
        first = FakeFrame(
            target.frame_url, {WRITE_COUNT_SCRIPT: {"count": 1}}, parent=main
        )
        second = FakeFrame(
            target.frame_url, {WRITE_COUNT_SCRIPT: {"count": 1}}, parent=main
        )

        written = await PlaywrightFieldWriter().write(
            FakePage(main, [first, second]), target, ANSWER
        )

        assert written is False
        assert first.times_run(WRITE_SCRIPT) == 0
        assert second.times_run(WRITE_SCRIPT) == 0

    async def test_only_the_frame_the_field_was_scanned_in_is_asked(self) -> None:
        target = field(frame_url="https://ats.example.com/widget", frame_chain="0")
        main = FakeFrame(MAIN_URL, {})
        wanted = writing_frame(target, url=target.frame_url, parent=main)
        # No responses at all: being asked anything raises.
        other = FakeFrame("https://ats.example.com/other", {}, parent=main)

        assert await PlaywrightFieldWriter().write(
            FakePage(main, [wanted, other]), target, ANSWER
        )
        assert other.calls == []

    async def test_a_field_whose_frame_is_gone_is_refused(self) -> None:
        target = field(frame_url="https://ats.example.com/widget", frame_chain="0")
        main = FakeFrame(MAIN_URL, {})

        assert (
            await PlaywrightFieldWriter().write(FakePage(main), target, ANSWER) is False
        )

    async def test_a_cross_origin_frame_is_never_asked(self) -> None:
        target = field(frame_url="https://third-party.example/widget")
        main = FakeFrame(MAIN_URL, {})
        foreign = FakeFrame(target.frame_url, {}, parent=main)

        assert (
            await PlaywrightFieldWriter().write(
                FakePage(main, [foreign]), target, ANSWER
            )
            is False
        )
        assert foreign.calls == []

    async def test_a_url_fragment_does_not_stop_a_frame_matching(self) -> None:
        target = field(frame_url=f"{MAIN_URL}#step-2")
        frame = writing_frame(target, url=MAIN_URL)

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER)


class TestVerifyingTheValueLanded:
    """A dispatched value is not a written one until the control holds it."""

    async def test_a_value_that_did_not_land_is_reported_as_unwritten(self) -> None:
        target = field()
        frame = writing_frame(target, matched=False)

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER) is False

    async def test_an_unverified_write_is_never_retried(self) -> None:
        """A second attempt would be a second set of input events."""
        target = field()
        frame = writing_frame(target, matched=False)

        await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER)

        assert frame.times_run(WRITE_SCRIPT) == 1

    async def test_a_page_that_refuses_the_write_is_reported_as_unwritten(self) -> None:
        target = field()
        frame = FakeFrame(
            MAIN_URL,
            {
                WRITE_COUNT_SCRIPT: {"count": 1},
                WRITE_SCRIPT: {
                    "ok": False,
                    "reason": "the control became disabled",
                    "count": 1,
                    "matched": False,
                    "identity": {},
                },
            },
        )

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER) is False


class TestProvenanceIsChecked:
    """The control that was typed into is the one the key names.

    Every audit row this project writes says "this answer went into this
    stable key". The writer resolves a control from metadata, which is a
    different question from "is this the control that key was derived
    from" — so when it shares the scanner that derived the key, it re-derives
    the key from what it actually found and refuses a mismatch.
    """

    async def test_a_matching_derived_key_is_written(self) -> None:
        scanner = FormScanner()
        target = keyed_field(scanner)
        frame = writing_frame(target)

        assert await PlaywrightFieldWriter(scanner=scanner).write(
            FakePage(frame), target, ANSWER
        )

    async def test_a_control_whose_derived_key_differs_is_refused(self) -> None:
        scanner = FormScanner()
        target = keyed_field(scanner)
        frame = writing_frame(
            target,
            identity={**resolved_identity(target), "name": "first_name"},
        )

        written = await PlaywrightFieldWriter(scanner=scanner).write(
            FakePage(frame), target, ANSWER
        )

        assert written is False

    async def test_a_relabelled_control_still_matches(self) -> None:
        """The derivation folds the label, so a repaint is not a mismatch."""
        scanner = FormScanner()
        target = keyed_field(scanner, label="Last name *")
        frame = writing_frame(
            target, identity={**resolved_identity(target), "label": "LAST NAME"}
        )

        assert await PlaywrightFieldWriter(scanner=scanner).write(
            FakePage(frame), target, ANSWER
        )

    async def test_a_mismatch_is_caught_before_anything_is_typed(self) -> None:
        """The refusal has to arrive before the value does.

        Checking afterwards would still report the write as failed, but the
        answer would already be sitting in the wrong control — and the run
        that reads "not typed" would leave a form somebody's salary was put
        into the wrong box on. So the identity is read during the counting
        pass, and a mismatch means the writing pass never runs.
        """
        scanner = FormScanner()
        target = keyed_field(scanner)
        frame = writing_frame(
            target,
            identity={**resolved_identity(target), "name": "first_name"},
        )

        written = await PlaywrightFieldWriter(scanner=scanner).write(
            FakePage(frame), target, ANSWER
        )

        assert written is False
        assert frame.arguments_for(WRITE_SCRIPT) == []

    async def test_without_a_shared_scanner_the_check_is_skipped(self) -> None:
        """A writer with no scanner cannot derive keys, and says so by not.

        It still requires exactly one metadata match, which is the check
        that stops a wrong control being typed into.
        """
        target = field(key="a-key-no-scanner-derived")
        frame = writing_frame(target)

        assert await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER)


class TestTheAnswerStaysOutOfEverythingElse:
    """A decided answer is somebody's salary, address, or cover letter."""

    async def test_the_counting_pass_is_never_given_the_value(self) -> None:
        target = field()
        frame = writing_frame(target)

        await PlaywrightFieldWriter().write(FakePage(frame), target, ANSWER)

        for argument in frame.arguments_for(WRITE_COUNT_SCRIPT):
            assert ANSWER not in repr(argument)

    async def test_a_refusal_never_carries_the_value(self) -> None:
        target = field()
        frame = writing_frame(target, matched=False)
        writer = PlaywrightFieldWriter()

        with pytest.raises(FieldWriteNotVerified) as raised:
            await writer.write_or_raise(FakePage(frame), target, ANSWER)

        assert ANSWER not in str(raised.value)

    @pytest.mark.parametrize(
        "error,frame_responses",
        [
            (UnsupportedFieldControl, None),
            (FieldNotUniquelyResolved, {WRITE_COUNT_SCRIPT: {"count": 0}}),
        ],
    )
    async def test_every_refusal_is_a_field_write_refusal(
        self, error: type[Exception], frame_responses: dict[str, Any] | None
    ) -> None:
        """One base class, so a caller can contain them all in one branch."""
        assert issubclass(error, FieldWriteRefused)
        target = field() if frame_responses else field(field_type="file")
        page = FakePage(FakeFrame(MAIN_URL, frame_responses or {}))

        with pytest.raises(error):
            await PlaywrightFieldWriter().write_or_raise(page, target, ANSWER)

    async def test_provenance_mismatch_is_a_field_write_refusal(self) -> None:
        assert issubclass(FieldProvenanceMismatch, FieldWriteRefused)


class TestTheWriterScripts:
    """The page-side half, checked structurally.

    Behaviour against a real DOM is proven by the headed integration test;
    what is worth pinning here is that these scripts ask the same questions
    about a control as the scanner does, and that they write the way a page
    framework will actually notice.
    """

    @pytest.mark.parametrize("script", [WRITE_COUNT_SCRIPT, WRITE_SCRIPT])
    def test_each_script_is_built_from_the_shared_identity_helper(
        self, script: str
    ) -> None:
        assert FIELD_IDENTITY_JS in script

    @pytest.mark.parametrize("script", [WRITE_COUNT_SCRIPT, WRITE_SCRIPT])
    def test_each_script_is_a_callable_javascript_expression(self, script: str) -> None:
        assert script.startswith("(")
        assert "=>" in script

    def test_the_write_script_dispatches_the_events_a_page_listens_for(self) -> None:
        assert "new Event('input'" in WRITE_SCRIPT
        assert "new Event('change'" in WRITE_SCRIPT
        assert "bubbles: true" in WRITE_SCRIPT

    def test_the_write_script_uses_the_native_value_setter(self) -> None:
        """A plain `el.value =` is invisible to a React-controlled input."""
        assert "getOwnPropertyDescriptor" in WRITE_SCRIPT
        assert "HTMLInputElement" in WRITE_SCRIPT
        assert "HTMLTextAreaElement" in WRITE_SCRIPT

    def test_the_write_script_reads_the_control_back(self) -> None:
        assert "matched" in WRITE_SCRIPT
        assert "controlValue" in WRITE_SCRIPT

    def test_the_write_script_returns_no_value_of_its_own(self) -> None:
        """It compares in the page and reports a boolean, not the text."""
        assert "value: controlValue" not in WRITE_SCRIPT
        assert "readBack" not in WRITE_SCRIPT

    @pytest.mark.parametrize("field_type", sorted(HUMAN_ONLY_FIELD_TYPES))
    def test_the_scripts_also_refuse_human_only_controls(self, field_type: str) -> None:
        """The allowlist is enforced in the page too, not only in Python."""
        assert field_type in WRITE_SCRIPT or "SUPPORTED_TYPES" in WRITE_SCRIPT

    def test_the_scripts_only_consider_visible_enabled_controls(self) -> None:
        for script in (WRITE_COUNT_SCRIPT, WRITE_SCRIPT):
            assert "isVisible(" in script
            assert "isDisabled(" in script


# --------------------------------------------------------------------------
# The page guard
# --------------------------------------------------------------------------


def guarding_frame(
    *,
    captcha: str = "",
    login: str = "",
    url: str = MAIN_URL,
    parent: FakeFrame | None = None,
) -> FakeFrame:
    return FakeFrame(
        url,
        {GUARD_SCRIPT: {"captcha": captcha, "login": login}},
        parent=parent,
    )


class TestTheGuardRefusesAChallengedPage:
    async def test_a_visible_challenge_widget_abandons_the_application(self) -> None:
        page = FakePage(guarding_frame(captcha='iframe[src*="recaptcha/"]'))

        with pytest.raises(CaptchaEncountered) as raised:
            await PlaywrightPageGuard().inspect(page)

        assert raised.value.marker == 'iframe[src*="recaptcha/"]'

    async def test_a_challenge_in_a_nested_frame_is_found(self) -> None:
        main = FakeFrame(MAIN_URL, {GUARD_SCRIPT: {"captcha": "", "login": ""}})
        nested = guarding_frame(
            captcha="div.cf-turnstile", url="https://ats.example.com/challenge", parent=main
        )

        with pytest.raises(CaptchaEncountered):
            await PlaywrightPageGuard().inspect(FakePage(main, [nested]))

    async def test_a_challenge_outranks_a_sign_in_form_on_the_same_page(self) -> None:
        """Both present means the challenge is what stops a human too."""
        page = FakePage(guarding_frame(captcha="div.g-recaptcha", login='input[type="password"]'))

        with pytest.raises(CaptchaEncountered):
            await PlaywrightPageGuard().inspect(page)


class TestTheGuardRefusesALoginWall:
    async def test_a_visible_password_field_abandons_the_application(self) -> None:
        page = FakePage(guarding_frame(login='input[type="password"]'))

        with pytest.raises(LoginWallEncountered) as raised:
            await PlaywrightPageGuard().inspect(page)

        assert raised.value.marker == 'input[type="password"]'


class TestTheGuardLetsAnOrdinaryPageThrough:
    async def test_nothing_reported_means_nothing_raised(self) -> None:
        await PlaywrightPageGuard().inspect(FakePage(guarding_frame()))

    async def test_a_frame_that_cannot_be_probed_does_not_fail_the_page(self) -> None:
        """A detached frame is routine; the scanner reports coverage gaps."""
        main = guarding_frame()
        broken = FakeFrame(
            "https://ats.example.com/gone",
            {},
            parent=main,
            evaluate_error=RuntimeError("frame detached"),
        )

        await PlaywrightPageGuard().inspect(FakePage(main, [broken]))

    async def test_a_cross_origin_frame_is_never_probed(self) -> None:
        main = guarding_frame()
        foreign = FakeFrame("https://third-party.example/ad", {}, parent=main)

        await PlaywrightPageGuard().inspect(FakePage(main, [foreign]))

        assert foreign.calls == []

    async def test_a_frame_that_never_answers_does_not_stall_the_run(self) -> None:
        """A frame with no execution context can otherwise wait forever.

        An `about:blank` iframe that is still notionally navigating is the
        real-world case: the driver waits for a context that never arrives.
        Left unbounded, the guard would hold a worker's tab, its lease, and
        the queue behind it, on a page nobody is looking at.

        The deadline is enforced by this test rather than merely by the fake
        never returning: a fake that eventually gives up would let a guard
        with no timeout at all pass, just slowly.
        """
        main = guarding_frame()
        silent = FakeFrame(
            "https://ats.example.com/pending",
            {GUARD_SCRIPT: _never_answers},
            parent=main,
        )

        await asyncio.wait_for(
            PlaywrightPageGuard(frame_timeout_ms=20).inspect(FakePage(main, [silent])),
            timeout=2,
        )

    async def test_a_verdict_from_a_frame_that_did_answer_still_counts(self) -> None:
        """The timeout skips one frame, not the inspection."""
        main = FakeFrame(MAIN_URL, {GUARD_SCRIPT: _never_answers})
        answering = guarding_frame(
            captcha="div.g-recaptcha", url="https://ats.example.com/challenge", parent=main
        )

        with pytest.raises(CaptchaEncountered):
            await asyncio.wait_for(
                PlaywrightPageGuard(frame_timeout_ms=20).inspect(
                    FakePage(main, [answering])
                ),
                timeout=2,
            )


async def _never_answers(argument: Any) -> Any:
    """Stands in for a frame whose execution context never arrives.

    Never returns and never raises. A fake that gave up after a while would
    make an unbounded guard look merely slow instead of broken.
    """
    await asyncio.Event().wait()
    raise AssertionError("unreachable: this frame never answers")


class TestTheGuardScript:
    """Structural markers only. Prose would abandon working applications.

    "Sign in" appears in the header of most job boards, and "verify" appears
    on plenty of ordinary forms. A guard matching those would skip
    applications that were perfectly fillable, which is a quiet failure: the
    operator sees `login_required` and has no way to know it was wrong.
    """

    def test_it_is_a_callable_javascript_expression(self) -> None:
        assert GUARD_SCRIPT.startswith("(")
        assert "=>" in GUARD_SCRIPT

    def test_it_never_reads_the_pages_prose(self) -> None:
        for prose in ("innerText", "textContent", "innerHTML"):
            assert prose not in GUARD_SCRIPT

    def test_it_is_not_even_given_the_label_reader(self) -> None:
        """It walks the page with the traversal helper and nothing more."""
        assert PAGE_TRAVERSAL_JS in GUARD_SCRIPT
        assert "labelText" not in GUARD_SCRIPT

    @pytest.mark.parametrize(
        "marker",
        [
            "recaptcha",
            "hcaptcha",
            "challenges.cloudflare.com",
            "arkoselabs",
            "g-recaptcha",
            "cf-turnstile",
        ],
    )
    def test_it_knows_the_common_challenge_widgets(self, marker: str) -> None:
        assert marker in GUARD_SCRIPT

    def test_it_requires_a_challenge_to_be_visible(self) -> None:
        """An invisible reCAPTCHA v3 badge challenges nobody."""
        assert "isVisible(" in GUARD_SCRIPT


class TestWhichCaptchaMarkupIsActuallyAChallenge:
    """A widget on the page is not the same thing as a challenge on it.

    reCAPTCHA v3 runs on an enormous number of ordinary pages, scoring
    every visitor and challenging almost none of them. It leaves behind a
    badge in the corner and an anchor iframe, both of which are present
    whether or not anybody was ever asked anything. Treating either as a
    challenge abandons applications that were perfectly fillable, and the
    operator sees `captcha_required` with no way to know it was wrong.

    The direction of the trade is deliberate. A missed challenge costs a
    click that ends unconfirmed and an application somebody checks by hand;
    a false one costs an application nobody ever finds out was fillable.
    """

    @pytest.mark.parametrize(
        "passive",
        [
            ".grecaptcha-badge",
            '[data-size="invisible"]',
            'iframe[src*="recaptcha/api2/anchor"]',
            'iframe[src*="recaptcha/enterprise/anchor"]',
        ],
    )
    def test_the_passive_markup_v3_leaves_behind_is_named_as_passive(
        self, passive: str
    ) -> None:
        assert passive in PASSIVE_CAPTCHA_SELECTORS

    @pytest.mark.parametrize(
        "selectors",
        [
            CAPTCHA_CHALLENGE_SELECTORS,
            CAPTCHA_WIDGET_SELECTORS,
            PASSIVE_CAPTCHA_SELECTORS,
        ],
    )
    def test_each_list_is_the_one_the_page_applies(
        self, selectors: tuple[str, ...]
    ) -> None:
        """Spliced in verbatim, so these tests are about the browser's rule."""
        assert json.dumps(list(selectors)) in ACTIVE_CAPTCHA_JS

    @pytest.mark.parametrize(
        "passive", [".grecaptcha-badge", 'iframe[src*="recaptcha/api2/anchor"]']
    )
    def test_no_passive_marker_is_also_an_active_one(self, passive: str) -> None:
        assert passive not in CAPTCHA_CHALLENGE_SELECTORS
        assert passive not in CAPTCHA_WIDGET_SELECTORS

    @pytest.mark.parametrize(
        "active",
        [
            'iframe[src*="recaptcha/api2/bframe"]',
            'iframe[src*="recaptcha/enterprise/bframe"]',
            "#hcaptcha-challenge",
            "#px-captcha",
        ],
    )
    def test_the_challenge_frame_is_what_it_looks_for(self, active: str) -> None:
        assert active in CAPTCHA_CHALLENGE_SELECTORS

    def test_a_bare_site_key_is_no_longer_a_challenge_on_its_own(self) -> None:
        """`data-sitekey` is the mount point, not the challenge.

        It is on the anchor container of every invisible and v3 widget, so
        matching it made the guard fire on pages that challenged nobody.
        """
        assert "div[data-sitekey]" not in ACTIVE_CAPTCHA_JS
        assert "div[data-sitekey]" not in GUARD_SCRIPT

    def test_a_widget_only_counts_when_a_person_could_use_it(self) -> None:
        """An invisible widget renders at 0x0; a real checkbox is 304x78."""
        assert "getBoundingClientRect" in ACTIVE_CAPTCHA_JS
        assert "MIN_CAPTCHA_WIDGET_WIDTH" in ACTIVE_CAPTCHA_JS
        assert "MIN_CAPTCHA_WIDGET_HEIGHT" in ACTIVE_CAPTCHA_JS

    def test_a_challenge_held_in_a_modal_dialog_counts(self) -> None:
        assert '[role="dialog"]' in ACTIVE_CAPTCHA_JS
        assert '[aria-modal="true"]' in ACTIVE_CAPTCHA_JS

    def test_the_guard_asks_this_one_question(self) -> None:
        """One rule, spliced in, rather than two that could drift apart.

        The submitter needs the same answer after its click — a challenge
        that appears then is a submission that did not happen — so the rule
        is a shared constant rather than a second selector list.
        """
        assert ACTIVE_CAPTCHA_JS in GUARD_SCRIPT
        assert "activeCaptcha(" in GUARD_SCRIPT

    def test_a_password_field_is_the_login_marker(self) -> None:
        assert 'input[type="password"]' in GUARD_SCRIPT

    @pytest.mark.parametrize(
        "over_broad",
        ['a[href*="login"]', 'a[href*="signin"]', "form[action*=\"/login\"]"],
    )
    def test_a_sign_in_link_is_deliberately_not_a_marker(
        self, over_broad: str
    ) -> None:
        assert over_broad not in GUARD_SCRIPT

    def test_it_descends_through_open_shadow_roots(self) -> None:
        assert "shadowRoot" in GUARD_SCRIPT


# --------------------------------------------------------------------------
# The submitter
# --------------------------------------------------------------------------


APPROVED = SubmitAuthorization(
    application_id=7,
    thread_id="application-1",
    decision=ApprovalDecision.APPROVED.value,
    decided_at="2026-08-19T12:00:00+00:00",
    gate="approval",
    blocking_reasons=(),
)


CONFIRMED = "Your application was submitted. Thank you for applying."


def region(text: str, identity: str = "") -> dict[str, str]:
    """One confirmation-shaped region, as the state script reports one.

    `identity` is what makes two readings the *same* region; when a test
    does not care, the text stands in for it, which is the case where a
    region's text never changes and identity and text agree.
    """
    return {"identity": identity or f"#{text}", "text": text}


def state(
    *,
    url: str = MAIN_URL,
    confirmations: Sequence[str | Mapping[str, str]] = (),
    blockers: Sequence[str] = (),
    marked: bool = True,
) -> dict[str, Any]:
    """One reading of one target: what a state script reports."""
    return {
        "url": url,
        "confirmations": [
            dict(entry) if isinstance(entry, Mapping) else region(entry)
            for entry in confirmations
        ],
        "blockers": list(blockers),
        "marked": marked,
    }


def state_reader(
    baseline: Mapping[str, Any], after: Sequence[Mapping[str, Any]]
) -> Any:
    """Answers the state script with the baseline first, then each reading.

    The last reading repeats, so a test that means "and then nothing else
    ever changed" does not have to say how many times it will be asked.
    """
    readings = [dict(baseline), *(dict(entry) for entry in after)]

    def next_reading(_argument: Any) -> dict[str, Any]:
        return readings[0] if len(readings) == 1 else readings.pop(0)

    return next_reading


def submitting_frame(
    *,
    accepted: Sequence[str] = ("Submit application",),
    rejected: Sequence[Mapping[str, str]] = (),
    baseline: Mapping[str, Any] | None = None,
    after: Sequence[Mapping[str, Any]] | None = None,
    url: str = MAIN_URL,
    parent: FakeFrame | None = None,
    element: FakeElement | None = None,
) -> FakeFrame:
    """A frame with a submit control, a pre-click state, and what follows."""
    return FakeFrame(
        url,
        {
            SUBMIT_COUNT_SCRIPT: {
                "count": len(accepted),
                "accepted": list(accepted),
                "rejected": [dict(entry) for entry in rejected],
            },
            SUBMIT_RESOLVE_SCRIPT: element if element is not None else FakeElement(),
            SUBMIT_TARGET_SCRIPT: {"ok": True, "marked": True},
            SUBMIT_STATE_SCRIPT: state_reader(
                baseline if baseline is not None else state(url=url),
                after
                if after is not None
                else (state(url=url, confirmations=[CONFIRMED]),),
            ),
        },
        parent=parent,
    )


def submit_control(frame: FakeFrame) -> FakeElement:
    """The control this frame would resolve, so a test can count its clicks."""
    element = frame._responses.get(SUBMIT_RESOLVE_SCRIPT)
    assert isinstance(element, FakeElement)
    return element


def presses(frame: FakeFrame) -> int:
    """How many trusted clicks landed on this frame's submit control."""
    return submit_control(frame).presses


class RecordedClaim:
    """A permit that remembers when it was claimed, relative to the click.

    The real one is `SubmitPermit` and it is what these tests construct: the
    substitute is only the durable claim behind it, which here appends to a
    list instead of writing a row. What matters to every test below is the
    order of that list against the control's own record of being checked and
    pressed.
    """

    def __init__(
        self, *, error: BaseException | None = None, log: list[str] | None = None
    ) -> None:
        self.error = error
        self.log = log if log is not None else []
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self.log.append("claim")
        if self.error is not None:
            raise self.error


def permit(claim: RecordedClaim | None = None) -> SubmitPermit:
    return SubmitPermit(APPROVED.application_id, claim or RecordedClaim())


def submitter(**kwargs: Any) -> PlaywrightSubmitter:
    clock = StepClock()
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("sleep", clock.sleep)
    kwargs.setdefault("seed", 7)
    return PlaywrightSubmitter(**kwargs)


class TestNothingIsSubmittedWithoutAnApproval:
    """The graph routes here only after a decision. This checks it anyway.

    The submit node is reachable from exactly two places, and both of them
    mean "a human said yes" or "an operator turned auto-submit on for a form
    with nothing blocking it". Restating that as a precondition the
    submitter itself enforces costs one comparison and means a future
    caller — a retry path, a debugging script, a refactor that adds an
    edge — cannot submit by wiring the graph wrongly.
    """

    @pytest.mark.parametrize(
        "authorization",
        [
            dataclasses.replace(APPROVED, decision=ApprovalDecision.REJECTED.value),
            dataclasses.replace(APPROVED, decision=""),
            dataclasses.replace(APPROVED, decision="approved_maybe"),
            dataclasses.replace(
                APPROVED,
                decision="",
                gate="auto_submit",
                blocking_reasons=("unanswered_gap",),
            ),
            dataclasses.replace(APPROVED, decision="", gate="approval"),
        ],
    )
    async def test_an_unapproved_submission_never_touches_the_page(
        self, authorization: SubmitAuthorization
    ) -> None:
        with pytest.raises(SubmitNotAuthorized):
            await submitter().submit(ForbiddenPage(), authorization, permit())

    async def test_an_approved_decision_is_carried_out(self) -> None:
        frame = submitting_frame()

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted

    async def test_auto_submit_with_nothing_blocking_is_carried_out(self) -> None:
        frame = submitting_frame()
        authorization = dataclasses.replace(
            APPROVED, decision="", gate="auto_submit", blocking_reasons=()
        )

        assert (
            await submitter().submit(FakePage(frame), authorization, permit())
        ).submitted

    def test_the_authorization_says_why_it_permits_a_submission(self) -> None:
        assert APPROVED.approved
        assert not dataclasses.replace(APPROVED, decision="").approved
        assert dataclasses.replace(
            APPROVED, decision="", gate="auto_submit"
        ).approved
        assert not dataclasses.replace(
            APPROVED,
            decision="",
            gate="auto_submit",
            blocking_reasons=("protected_question",),
        ).approved


class TestFindingExactlyOneFinalSubmitControl:
    async def test_one_control_is_clicked_once(self) -> None:
        frame = submitting_frame()
        page = FakePage(frame)

        assert (await submitter().submit(page, APPROVED, permit())).submitted
        assert presses(frame) == 1

    async def test_no_control_is_a_refusal_and_no_click(self) -> None:
        frame = submitting_frame(
            accepted=(),
            rejected=({"name": "next", "reason": "never a final submit"},),
        )
        page = FakePage(frame)

        with pytest.raises(FinalSubmitControlNotFound) as raised:
            await submitter().submit(page, APPROVED, permit())

        assert presses(frame) == 0
        assert "next" in str(raised.value)

    async def test_two_controls_in_one_frame_are_a_refusal_and_no_click(self) -> None:
        frame = submitting_frame(accepted=("Submit application", "Submit"))
        page = FakePage(frame)

        with pytest.raises(FinalSubmitControlAmbiguous):
            await submitter().submit(page, APPROVED, permit())

        assert presses(frame) == 0

    async def test_one_control_in_each_of_two_frames_is_a_refusal(self) -> None:
        main = FakeFrame(
            MAIN_URL,
            {SUBMIT_COUNT_SCRIPT: {"count": 1, "accepted": ["Submit"], "rejected": []}},
        )
        nested = FakeFrame(
            "https://ats.example.com/second",
            {
                SUBMIT_COUNT_SCRIPT: {
                    "count": 1,
                    "accepted": ["Submit application"],
                    "rejected": [],
                }
            },
            parent=main,
        )
        page = FakePage(main, [nested])

        with pytest.raises(FinalSubmitControlAmbiguous):
            await submitter().submit(page, APPROVED, permit())

        assert main.times_run(SUBMIT_RESOLVE_SCRIPT) == 0

    async def test_a_cross_origin_frame_is_never_asked_for_a_submit_control(
        self,
    ) -> None:
        main = submitting_frame()
        foreign = FakeFrame("https://third-party.example/ad", {}, parent=main)

        await submitter().submit(FakePage(main, [foreign]), APPROVED, permit())

        assert foreign.calls == []


class TestThePressIsAVerifiedClickRatherThanACoordinate:
    """A pointer press at a remembered centre lands on whatever is there.

    Between resolving a control and pressing it, a cookie banner can
    animate in over it, a sticky footer can cover it, a chat widget can
    take the corner, or the node can detach entirely. A blind
    `mouse.down()` at the box's centre lands on whichever of those is
    actually under the pointer, and this is the press that sends somebody's
    application somewhere.

    So the press is the driver's own trusted click, which will not fire
    until the element is visible, stable, enabled, and genuinely the thing
    that receives an event at that point.
    """

    async def test_the_control_is_checked_before_it_is_pressed(self) -> None:
        frame = submitting_frame()

        await submitter().submit(FakePage(frame), APPROVED, permit())

        assert submit_control(frame).order == ["trial", "click"]

    async def test_the_press_goes_through_the_driver_not_the_bare_pointer(
        self,
    ) -> None:
        frame = submitting_frame()
        page = FakePage(frame)

        await submitter().submit(page, APPROVED, permit())

        assert presses(frame) == 1
        assert page.mouse.presses == 0

    async def test_the_pointer_still_travels_there_first(self) -> None:
        """The movement is what an extension's own listeners see."""
        frame = submitting_frame()
        page = FakePage(frame)

        await submitter().submit(page, APPROVED, permit())

        assert len(page.mouse.moves) > 1

    @pytest.mark.parametrize(
        "failure",
        [
            TimeoutError("element is not receiving pointer events"),
            RuntimeError("element is outside of the viewport"),
            RuntimeError("Element is not attached to the DOM"),
        ],
    )
    async def test_a_control_that_cannot_be_reached_is_refused_unpressed(
        self, failure: BaseException
    ) -> None:
        frame = submitting_frame(element=FakeElement(trial_error=failure))
        page = FakePage(frame)

        with pytest.raises(FinalSubmitControlNotActionable) as raised:
            await submitter().submit(page, APPROVED, permit())

        assert presses(frame) == 0
        assert page.mouse.presses == 0
        assert str(failure) in str(raised.value)

    async def test_a_press_that_fails_after_the_check_is_never_a_submission(
        self,
    ) -> None:
        """Half a click is not a submission, and is not two clicks either."""
        frame = submitting_frame(
            element=FakeElement(click_error=TimeoutError("the page went away"))
        )

        with pytest.raises(FinalSubmitControlNotActionable):
            await submitter().submit(FakePage(frame), APPROVED, permit())

        assert presses(frame) == 1

    async def test_a_control_the_size_of_the_page_is_refused(self) -> None:
        """Hit-target verification is happy to click a full-page container.

        It only asks whether the element receives the event, and a wrapper
        that covers the viewport does. What is underneath it could be any
        link on the page.
        """
        frame = submitting_frame(
            element=FakeElement({"x": 0.0, "y": 0.0, "width": 1280.0, "height": 900.0})
        )

        with pytest.raises(FinalSubmitControlNotFound) as raised:
            await submitter().submit(FakePage(frame), APPROVED, permit())

        assert presses(frame) == 0
        assert "container" in str(raised.value)

    async def test_a_control_with_no_box_at_all_is_refused(self) -> None:
        frame = submitting_frame(element=FakeElement(boxless=True))

        with pytest.raises(FinalSubmitControlNotFound):
            await submitter().submit(FakePage(frame), APPROVED, permit())

        assert presses(frame) == 0


class TestWhenTheOnePressIsClaimed:
    """Last thing before the click, and nothing between the two.

    The claim is durable and never expires, so it is the application's one
    attempt. Taken any earlier, every refusal that follows it — a control
    worded so the accessible-name rules reject it, two controls, a cookie
    banner that animated in over the one control — spends an attempt on a
    page nothing was clicked on, and the operator who fixes that page finds
    an application that can never be sent.

    Taken any later, a worker killed between the claim and the press leaves
    a thread that looks exactly like one that never pressed, and the replay
    presses a second time. So the order is: resolve, check the rules, let
    the driver confirm the control is really clickable, claim, click.
    """

    async def test_it_is_claimed_after_the_check_and_before_the_click(self) -> None:
        claim = RecordedClaim()
        element = FakeElement()
        element.order = claim.log
        frame = submitting_frame(element=element)

        await submitter().submit(FakePage(frame), APPROVED, permit(claim))

        assert claim.log == ["trial", "claim", "click"]

    async def test_a_control_the_rules_reject_never_claims_it(self) -> None:
        claim = RecordedClaim()
        frame = submitting_frame(
            accepted=(), rejected=({"name": "Next", "reason": "not a final submit"},)
        )

        with pytest.raises(FinalSubmitControlNotFound):
            await submitter().submit(FakePage(frame), APPROVED, permit(claim))

        assert claim.calls == 0

    async def test_a_control_something_is_covering_never_claims_it(self) -> None:
        claim = RecordedClaim()
        frame = submitting_frame(
            element=FakeElement(
                trial_error=TimeoutError("element is not receiving pointer events")
            )
        )

        with pytest.raises(FinalSubmitControlNotActionable):
            await submitter().submit(FakePage(frame), APPROVED, permit(claim))

        assert claim.calls == 0

    async def test_an_unapproved_submission_never_claims_it(self) -> None:
        claim = RecordedClaim()
        frame = submitting_frame()

        with pytest.raises(SubmitNotAuthorized):
            await submitter().submit(
                FakePage(frame),
                dataclasses.replace(APPROVED, decision=""),
                permit(claim),
            )

        assert claim.calls == 0

    async def test_a_press_nothing_confirmed_still_claimed_it(self) -> None:
        """The click landed. Whether the page said so is a separate question."""
        claim = RecordedClaim()
        frame = submitting_frame(after=(state(),))

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED, permit(claim)
        )

        assert not outcome.submitted
        assert claim.calls == 1

    async def test_a_claim_the_record_refuses_stops_the_press(self) -> None:
        """The replay case: another attempt already pressed this one.

        The submitter gets no further than the claim, so the control is
        never pressed a second time — and the refusal that comes back names
        the earlier attempt rather than being reported as a fresh failure.
        """
        already = SubmitAlreadyAttempted(
            APPROVED.application_id,
            "worker-1",
            datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
        )
        claim = RecordedClaim(error=already)
        frame = submitting_frame()

        with pytest.raises(SubmitAlreadyAttempted):
            await submitter().submit(FakePage(frame), APPROVED, permit(claim))

        assert presses(frame) == 0

    async def test_the_permit_it_was_given_is_the_one_it_claims(self) -> None:
        """Not a claim of its own: the graph checks this exact object."""
        offered = permit()
        frame = submitting_frame()

        await submitter().submit(FakePage(frame), APPROVED, offered)

        assert offered.claimed


class TestReportingASubmission:
    """`submitted` is a claim about the page, never about the click."""

    async def test_a_confirmation_that_appears_after_the_click_is_enough(self) -> None:
        frame = submitting_frame(after=(state(confirmations=[CONFIRMED]),))

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted
        assert "confirmation" in outcome.reason

    async def test_a_success_destination_the_form_did_not_survive_is_enough(
        self,
    ) -> None:
        frame = submitting_frame(
            after=(state(url="https://ats.example.com/thank-you", marked=False),)
        )

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted
        assert "thank-you" in outcome.reason

    async def test_a_signal_that_arrives_late_is_still_waited_for(self) -> None:
        frame = submitting_frame(
            after=(state(), state(), state(confirmations=[CONFIRMED])),
        )

        assert (await submitter().submit(FakePage(frame), APPROVED, permit())).submitted
        assert frame.times_run(SUBMIT_STATE_SCRIPT) >= 4

    async def test_no_signal_at_all_is_not_a_submission(self) -> None:
        frame = submitting_frame(after=(state(),))

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED, permit()
        )

        assert outcome.submitted is False
        assert "no" in outcome.reason.lower()

    async def test_an_unconfirmed_outcome_is_never_clicked_a_second_time(self) -> None:
        """The one thing worse than an unconfirmed submission is two."""
        frame = submitting_frame(after=(state(),))
        page = FakePage(frame)

        await submitter(confirm_timeout_ms=400).submit(page, APPROVED, permit())

        assert presses(frame) == 1

    async def test_the_unconfirmed_reason_says_what_was_looked_for(self) -> None:
        frame = submitting_frame(after=(state(),))

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED, permit()
        )

        assert "confirmation" in outcome.reason
        assert "navigation" in outcome.reason


class TestEachTargetIsJudgedAgainstItsOwnBaseline:
    """A form in a child frame is where a shared baseline goes wrong.

    The submitting frame's URL is not the top page's URL, and the form the
    control belongs to is not in the top page's document at all. Comparing
    one target's "before" against another target's "after" therefore reports
    a navigation and a vanished form on the very first poll, on a page where
    nothing whatsoever has happened — which is a submission recorded, an
    approval consumed, and a queue row marked completed for a form that is
    still sitting there filled in.
    """

    def _framed_page(
        self,
        *,
        after: Sequence[Mapping[str, Any]],
        top_baseline: Mapping[str, Any] | None = None,
        top_after: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[FakePage, FakeFrame]:
        """A top page with no form, and a child frame that holds one."""
        top = state(url=MAIN_URL, marked=False)
        main = FakeFrame(
            MAIN_URL,
            {
                SUBMIT_COUNT_SCRIPT: {"count": 0, "accepted": [], "rejected": []},
                SUBMIT_STATE_SCRIPT: state_reader(
                    top_baseline if top_baseline is not None else top,
                    top_after if top_after is not None else (top,),
                ),
            },
        )
        inner = submitting_frame(
            url="https://ats.example.com/embedded-form",
            baseline=state(url="https://ats.example.com/embedded-form"),
            after=after,
            parent=main,
        )
        return FakePage(main, [inner]), inner

    async def test_a_top_page_that_never_held_the_form_cannot_report_it_gone(
        self,
    ) -> None:
        page, _inner = self._framed_page(after=(state(url="https://ats.example.com/embedded-form"),))

        outcome = await submitter(confirm_timeout_ms=400).submit(page, APPROVED, permit())

        assert outcome.submitted is False

    async def test_a_top_page_at_its_own_url_is_not_a_navigation(self) -> None:
        """The child frame's URL differs from the top page's by definition."""
        page, _inner = self._framed_page(after=(state(url="https://ats.example.com/embedded-form"),))

        outcome = await submitter(confirm_timeout_ms=400).submit(page, APPROVED, permit())

        assert outcome.submitted is False
        assert "navigation" in outcome.reason

    async def test_a_confirmation_the_top_page_was_already_showing_cannot_succeed(
        self,
    ) -> None:
        """The wrapper page saying "thank you for applying" about last time.

        A careers site that keeps a standing thank-you panel, or a page
        that was reached from a previous application, already reads like a
        confirmation before anything is clicked.
        """
        standing = state(url=MAIN_URL, marked=False, confirmations=[CONFIRMED])
        page, _inner = self._framed_page(
            after=(state(url="https://ats.example.com/embedded-form"),),
            top_baseline=standing,
            top_after=(standing,),
        )

        outcome = await submitter(confirm_timeout_ms=400).submit(page, APPROVED, permit())

        assert outcome.submitted is False

    async def test_a_confirmation_the_frame_was_already_showing_cannot_succeed(
        self,
    ) -> None:
        standing = state(confirmations=[CONFIRMED])
        frame = submitting_frame(baseline=standing, after=(standing,))

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED, permit()
        )

        assert outcome.submitted is False

    async def test_a_second_confirmation_alongside_the_standing_one_does_succeed(
        self,
    ) -> None:
        """Freshness, not absence: a page may confirm twice over."""
        standing = state(confirmations=["Thank you for applying to our team"])
        frame = submitting_frame(
            baseline=standing,
            after=(
                state(confirmations=["Thank you for applying to our team", CONFIRMED]),
            ),
        )

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted
        assert CONFIRMED in outcome.reason

    async def test_the_frame_holding_the_form_is_asked_before_the_click(self) -> None:
        frame = submitting_frame()
        page = FakePage(frame)

        await submitter().submit(page, APPROVED, permit())

        assert frame.times_run(SUBMIT_STATE_SCRIPT) >= 2


TICKING = "Thank you for applying — {count} applications this month"


class TestWhatMakesAConfirmationANewOne:
    """A region is the same region when its text changes, not a new one.

    Comparing the *text* of confirmation-shaped regions was the whole
    freshness rule, and a careers page with a standing thank-you panel that
    counts, ticks, cycles, or animates its wording produces a text nobody
    was showing before the click on the very first poll — every time,
    within a fifth of a second, whatever the click did. That is a submitted
    application recorded against a form that never went anywhere.

    So freshness is a property of the region, addressed by an identity that
    survives its text changing. The regions already reading like a
    confirmation are the baseline; a confirmation is new when a region that
    was not one of them is now shaped like one.
    """

    def test_a_region_whose_wording_ticks_is_still_the_same_region(self) -> None:
        verdict = submission_verdict(
            PageState(
                url=MAIN_URL,
                marked=True,
                confirmations=(
                    ConfirmationRegion("#applied-count", TICKING.format(count=11)),
                ),
            ),
            PageState(
                url=MAIN_URL,
                marked=True,
                confirmations=(
                    ConfirmationRegion("#applied-count", TICKING.format(count=12)),
                ),
            ),
        )

        assert not verdict.signal

    async def test_a_counting_panel_never_confirms_however_long_it_counts(
        self,
    ) -> None:
        standing = state(
            confirmations=[region(TICKING.format(count=3), "#applied-count")]
        )
        frame = submitting_frame(
            baseline=standing,
            after=tuple(
                state(confirmations=[region(TICKING.format(count=n), "#applied-count")])
                for n in range(4, 12)
            ),
        )

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED, permit()
        )

        assert outcome.submitted is False
        assert presses(frame) == 1

    async def test_a_neutral_status_region_that_becomes_a_confirmation_confirms(
        self,
    ) -> None:
        """The common case, and the one an identity rule must not break.

        Almost every ATS has one empty `role="status"` region that the click
        fills in. It is not in the confirmation baseline — nothing that is
        not already shaped like a confirmation ever is — so the moment it
        reads like one, it is a new confirmation.
        """
        frame = submitting_frame(
            baseline=state(confirmations=[]),
            after=(state(confirmations=[region(CONFIRMED, "#form-status")]),),
        )

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted
        assert CONFIRMED in outcome.reason

    async def test_a_confirmation_beside_a_counting_panel_still_confirms(self) -> None:
        """The counter keeps ticking; the real banner still arrives."""
        frame = submitting_frame(
            baseline=state(
                confirmations=[region(TICKING.format(count=3), "#applied-count")]
            ),
            after=(
                state(
                    confirmations=[
                        region(TICKING.format(count=4), "#applied-count"),
                        region(CONFIRMED, "#form-status"),
                    ]
                ),
            ),
        )

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted
        assert CONFIRMED in outcome.reason

    def test_the_standing_wording_reappearing_in_a_new_node_is_not_a_second_one(
        self,
    ) -> None:
        """The other half of the rule, and the other way a page repaints.

        A framework that rebuilds its banner rather than editing its text
        leaves a region with no history, and identity alone would read that
        as a confirmation arriving. The wording it arrived with is the
        wording the page was already showing, so it is not one.
        """
        verdict = submission_verdict(
            PageState(
                url=MAIN_URL,
                marked=True,
                confirmations=(ConfirmationRegion("#banner", CONFIRMED),),
            ),
            PageState(
                url=MAIN_URL,
                marked=True,
                confirmations=(ConfirmationRegion("#banner-rebuilt", CONFIRMED),),
            ),
        )

        assert not verdict.signal

    def test_a_region_that_cannot_be_identified_confirms_nothing(self) -> None:
        """Silence from the reading, not a submission on an unjudgeable one."""
        state_with_anonymous_region = PageState.from_report(
            {
                "url": MAIN_URL,
                "marked": True,
                "confirmations": [{"text": CONFIRMED}, CONFIRMED, {"identity": "#a"}],
            }
        )

        assert state_with_anonymous_region.confirmations == ()
        assert not submission_verdict(
            PageState(url=MAIN_URL, marked=True), state_with_anonymous_region
        ).signal


class TestNavigationAloneNeverConfirms:
    """The weakest of the old signals, and the one most often wrong.

    An ATS that redirects an unauthenticated poster to a sign-in page, a
    validation round trip that reloads with errors, and a submission that
    genuinely succeeded all navigate. Accepting the navigation itself meant
    the first two were recorded as submitted applications.
    """

    def test_a_bare_navigation_confirms_nothing(self) -> None:
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(url="https://ats.example.com/apply/step-2", marked=True),
        )

        assert not verdict.signal
        assert not verdict.refusal

    def test_a_success_destination_the_form_survived_confirms_nothing(self) -> None:
        """A wizard step called "complete" is still a form to fill in."""
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(url="https://ats.example.com/apply/completed", marked=True),
        )

        assert not verdict.signal

    def test_the_form_going_without_a_navigation_confirms_nothing(self) -> None:
        """A single-page ATS swapping in step two removes the form too."""
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(url=MAIN_URL, marked=False),
        )

        assert not verdict.signal

    @pytest.mark.parametrize(
        "destination",
        [
            "https://ats.example.com/thank-you",
            "https://ats.example.com/thanks?job=42",
            "https://ats.example.com/application/submitted",
            "https://ats.example.com/confirmation",
            "https://ats.example.com/apply?submitted=true",
        ],
    )
    def test_a_success_destination_and_a_vanished_form_together_confirm(
        self, destination: str
    ) -> None:
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(url=destination, marked=False),
        )

        assert verdict.signal
        assert destination in verdict.signal

    @pytest.mark.parametrize(
        "destination",
        [
            "https://ats.example.com/login?next=/apply",
            "https://ats.example.com/users/sign_in",
            "https://ats.example.com/auth/callback",
            "https://ats.example.com/error",
            "https://ats.example.com/apply/failed",
            "https://ats.example.com/session-expired",
            "https://ats.example.com/challenge",
        ],
    )
    def test_a_destination_no_submission_ends_at_is_a_refusal(
        self, destination: str
    ) -> None:
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(url=destination, marked=False),
        )

        assert not verdict.signal
        assert verdict.refusal

    def test_a_refused_destination_outranks_a_confirmation_on_it(self) -> None:
        """A sign-in page is not made trustworthy by the words on it."""
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(
                url="https://ats.example.com/login",
                marked=False,
                confirmations=(ConfirmationRegion("#banner", CONFIRMED),),
            ),
        )

        assert not verdict.signal
        assert verdict.refusal

    def test_a_url_fragment_is_not_a_navigation(self) -> None:
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(url=f"{MAIN_URL}#thank-you", marked=False),
        )

        assert not verdict.signal


class TestThePageSayingItRefusedTheSubmission:
    """A refusal ends the wait, and never with a second click.

    Waiting out the full confirmation timeout on a page that has already
    said "Last name is required" wastes the only minutes an operator has,
    and reporting it as merely unconfirmed sends them to check an ATS that
    has nothing in it.
    """

    async def test_a_validation_message_that_appears_ends_the_wait(self) -> None:
        frame = submitting_frame(
            after=(state(blockers=['a validation message: "Last name is required"']),)
        )
        page = FakePage(frame)

        outcome = await submitter().submit(page, APPROVED, permit())

        assert outcome.submitted is False
        assert "Last name is required" in outcome.reason
        assert presses(frame) == 1

    async def test_a_blocker_that_was_already_there_is_not_a_refusal(self) -> None:
        """A form with a standing "required field" hint is an ordinary form."""
        blockers = ['a validation message: "All fields are required"']
        frame = submitting_frame(
            baseline=state(blockers=blockers),
            after=(state(blockers=blockers, confirmations=[CONFIRMED]),),
        )

        assert (await submitter().submit(FakePage(frame), APPROVED, permit())).submitted

    async def test_a_challenge_that_appears_after_the_click_is_not_a_submission(
        self,
    ) -> None:
        frame = submitting_frame(
            after=(
                state(blockers=["a human-verification challenge (div.g-recaptcha)"]),
            )
        )

        outcome = await submitter().submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted is False
        assert "challenge" in outcome.reason

    def test_a_fresh_blocker_outranks_a_success_destination(self) -> None:
        verdict = submission_verdict(
            PageState(url=MAIN_URL, marked=True),
            PageState(
                url="https://ats.example.com/thank-you",
                marked=False,
                blockers=("a sign-in prompt",),
            ),
        )

        assert not verdict.signal
        assert verdict.refusal


class TestNoFrameCanHoldTheSubmitter:
    """A frame with no execution context must cost a timeout, not a worker.

    This is the same defect the guard and the scanner already had: an
    `about:blank` iframe that is still notionally navigating never answers
    an `evaluate` at all, and the driver waits for a context that is not
    coming. Here it would hold a tab whose form has just been filled, in
    the one node where a stuck worker also means a lease nobody renews and
    an approval nobody can act on.
    """

    async def test_a_frame_that_never_answers_before_the_press_is_skipped(
        self,
    ) -> None:
        main = submitting_frame()
        silent = FakeFrame(
            "https://ats.example.com/pending",
            {SUBMIT_COUNT_SCRIPT: _never_answers},
            parent=main,
        )

        outcome = await asyncio.wait_for(
            submitter(frame_timeout_ms=20).submit(FakePage(main, [silent]), APPROVED, permit()),
            timeout=5,
        )

        assert outcome.submitted

    async def test_a_target_that_never_answers_after_the_press_is_not_a_signal(
        self,
    ) -> None:
        """A stuck target is silence, and silence is not a submission."""
        main = FakeFrame(
            MAIN_URL,
            {
                SUBMIT_COUNT_SCRIPT: {"count": 0, "accepted": [], "rejected": []},
                SUBMIT_STATE_SCRIPT: _never_answers,
            },
        )
        inner = submitting_frame(
            url="https://ats.example.com/embedded-form",
            after=(state(url="https://ats.example.com/embedded-form"),),
            parent=main,
        )
        page = FakePage(main, [inner])

        outcome = await asyncio.wait_for(
            submitter(frame_timeout_ms=20, confirm_timeout_ms=400).submit(
                page, APPROVED, permit()
            ),
            timeout=5,
        )

        assert outcome.submitted is False
        assert presses(inner) == 1

    async def test_the_deadline_still_ends_a_wait_in_which_nothing_answers(
        self,
    ) -> None:
        """Every per-frame timeout together must not outlive the deadline."""
        readings = [state()]

        def baseline_then_silence(argument: Any) -> Any:
            return readings.pop() if readings else _never_answers(argument)

        main = submitting_frame()
        main._responses[SUBMIT_STATE_SCRIPT] = baseline_then_silence

        outcome = await asyncio.wait_for(
            submitter(frame_timeout_ms=20, confirm_timeout_ms=400).submit(
                FakePage(main), APPROVED, permit()
            ),
            timeout=5,
        )

        assert outcome.submitted is False

    async def test_a_frame_that_never_marks_the_form_is_refused_before_the_click(
        self,
    ) -> None:
        """No pre-click baseline means nothing to compare a signal against."""
        main = submitting_frame()
        main._responses[SUBMIT_TARGET_SCRIPT] = _never_answers
        page = FakePage(main)

        with pytest.raises(FinalSubmitControlNotFound):
            await asyncio.wait_for(
                submitter(frame_timeout_ms=20).submit(page, APPROVED, permit()), timeout=5
            )

        assert presses(main) == 0


class TestSubmissionScreenshots:
    class Shots:
        def __init__(self) -> None:
            self.names: list[str] = []

        async def capture(self, page: Any, name: str) -> str | None:
            self.names.append(name)
            return f"/artifacts/{name}.png"

    async def test_a_confirmed_submission_is_photographed(self) -> None:
        shots = self.Shots()
        frame = submitting_frame()

        outcome = await submitter(screenshots=shots).submit(FakePage(frame), APPROVED, permit())

        assert outcome.screenshot_path == "/artifacts/submitted.png"

    async def test_an_unconfirmed_submission_is_photographed_too(self) -> None:
        """This is the screenshot an operator most needs to look at."""
        shots = self.Shots()
        frame = submitting_frame(after=(state(),))

        outcome = await submitter(confirm_timeout_ms=400, screenshots=shots).submit(
            FakePage(frame), APPROVED, permit()
        )

        assert outcome.screenshot_path == "/artifacts/unconfirmed.png"

    async def test_a_failed_screenshot_does_not_change_the_outcome(self) -> None:
        class Broken:
            async def capture(self, page: Any, name: str) -> str | None:
                raise RuntimeError("no disk")

        frame = submitting_frame()

        outcome = await submitter(screenshots=Broken()).submit(FakePage(frame), APPROVED, permit())

        assert outcome.submitted
        assert outcome.screenshot_path is None


class TestWhichAccessibleNamesCountAsAFinalSubmit:
    """The rule, in Python, shared verbatim with the page.

    This is the single most dangerous decision in the project: a name rule
    one word too broad clicks "Save and continue" on a half-filled form, and
    one word too narrow means an approved application never goes anywhere.
    Both the list and the folding live here and are spliced into the page
    script, so this test is testing the rule the browser applies.
    """

    @pytest.mark.parametrize("phrase", sorted(FINAL_SUBMIT_PHRASES))
    def test_every_accepted_phrase_is_accepted(self, phrase: str) -> None:
        assert is_final_submit_name(phrase)

    @pytest.mark.parametrize(
        "name",
        [
            "Submit application",
            "SUBMIT APPLICATION",
            "  Submit   application  ",
            "Submit application →",
            "Submit Application!",
            "Submit my application",
        ],
    )
    def test_a_real_final_submit_button_is_recognised(self, name: str) -> None:
        assert is_final_submit_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Next",
            "Next step",
            "Continue",
            "Continue to review",
            "Save and continue",
            "Save draft",
            "Save",
            "Review application",
            "Review and submit",
            "Back",
            "Previous",
            "Upload resume",
            "Attach a file",
            "Add another employer",
            "Sign in",
            "Log in",
            "Create account",
            "Cancel",
            "Preview application",
            "Edit application",
            "Submit and continue",
        ],
    )
    def test_anything_that_is_not_the_last_click_is_refused(self, name: str) -> None:
        assert not is_final_submit_name(name)

    @pytest.mark.parametrize("name", ["Apply", "Apply now", "Easy Apply", "Apply →"])
    def test_apply_is_deliberately_not_accepted(self, name: str) -> None:
        """It is also the word that *starts* an application on every board.

        A form whose only final control says "Apply" is not submitted by
        this build; it is reported as having no final-submit control and
        left for the applicant. Guessing wrong in the other direction means
        clicking the button that opens somebody's application flow.
        """
        assert not is_final_submit_name(name)

    @pytest.mark.parametrize("name", ["", "   ", "→", "?"])
    def test_a_nameless_control_is_never_the_one(self, name: str) -> None:
        assert not is_final_submit_name(name)

    def test_a_denied_word_wins_over_an_accepted_phrase(self) -> None:
        """Belt and braces: the allowlist is exact phrases already."""
        assert not is_final_submit_name("submit application and continue")

    @pytest.mark.parametrize(
        "word",
        [
            "next",
            "continue",
            "save",
            "review",
            "back",
            "previous",
            "draft",
            "upload",
            "attach",
            "cancel",
            "sign in",
            "log in",
            "register",
            "preview",
            "skip",
        ],
    )
    def test_the_denylist_still_names_the_words_it_promises_to(self, word: str) -> None:
        """The list itself, not its effect — because it has none yet.

        No denied word can currently reach the allowlist: that is exact
        phrases, so "Next" is refused by not being on it. Deleting the
        denylist changes no outcome today, which is precisely why it needs
        pinning here — it becomes load-bearing the moment somebody adds a
        broader accepted phrase, and the README already promises these words
        are never clicked.
        """
        assert NEVER_SUBMIT_NAME.search(word)

    @pytest.mark.parametrize(
        "raw,folded",
        [
            ("Submit application →", "submit application"),
            ("  SUBMIT   Application  ", "submit application"),
            ("Save & continue", "save continue"),
            ("Apply now!", "apply now"),
        ],
    )
    def test_the_folding_is_the_one_the_page_uses(self, raw: str, folded: str) -> None:
        assert normalize_control_name(raw) == folded

    def test_no_accepted_phrase_is_itself_denied(self) -> None:
        """A contradictory rule set would accept nothing and say nothing."""
        assert all(is_final_submit_name(phrase) for phrase in FINAL_SUBMIT_PHRASES)


class TestWhatCountsAsAConfirmation:
    @pytest.mark.parametrize(
        "text",
        [
            "Your application has been submitted.",
            "Application submitted",
            "Application received",
            "Thank you for applying!",
            "Thank you for your application",
            "We have received your application.",
            "Submitted successfully",
        ],
    )
    def test_a_real_confirmation_is_recognised(self, text: str) -> None:
        assert is_confirmation_text(text)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Submit application",
            "Please complete the required fields",
            "Step 2 of 3",
            "Your application could not be submitted",
            "Review your application before submitting",
        ],
    )
    def test_anything_short_of_one_is_refused(self, text: str) -> None:
        assert not is_confirmation_text(text)

    def test_the_pattern_is_shared_with_the_page(self) -> None:
        assert CONFIRMATION_TEXT.pattern in SUBMIT_STATE_SCRIPT


class TestTheSubmitterScripts:
    @pytest.mark.parametrize(
        "script",
        [SUBMIT_COUNT_SCRIPT, SUBMIT_RESOLVE_SCRIPT, SUBMIT_STATE_SCRIPT],
    )
    def test_each_script_is_a_callable_javascript_expression(self, script: str) -> None:
        assert script.startswith("(")
        assert "=>" in script

    @pytest.mark.parametrize("script", [SUBMIT_COUNT_SCRIPT, SUBMIT_RESOLVE_SCRIPT])
    def test_counting_and_resolving_share_one_notion_of_a_candidate(
        self, script: str
    ) -> None:
        assert SUBMIT_CANDIDATES_JS in script

    def test_the_candidate_rules_are_the_python_ones(self) -> None:
        for phrase in FINAL_SUBMIT_PHRASES:
            assert phrase in SUBMIT_CANDIDATES_JS

    def test_only_real_submit_controls_are_candidates(self) -> None:
        for selector in ('button[type="submit"]', 'input[type="submit"]'):
            assert selector in SUBMIT_CANDIDATES_JS

    def test_a_candidate_must_be_visible_and_enabled(self) -> None:
        assert "isVisible(" in SUBMIT_CANDIDATES_JS
        assert "isDisabled(" in SUBMIT_CANDIDATES_JS

    def test_candidates_are_searched_through_open_shadow_roots(self) -> None:
        assert "collectRoots(" in SUBMIT_CANDIDATES_JS

    def test_the_state_script_reports_facts_rather_than_verdicts(self) -> None:
        """Only readings cross the wire; the rules are applied in Python.

        Which readings count as a submission is the most consequential
        decision this project makes, so it lives where it can be tested
        directly rather than asserted about through a substring.
        """
        for reading in ("url", "confirmations", "blockers", "marked"):
            assert reading in SUBMIT_STATE_SCRIPT
        for verdict in ("navigated", "formGone"):
            assert verdict not in SUBMIT_STATE_SCRIPT

    def test_the_state_script_reads_the_same_page_before_and_after(self) -> None:
        """One script, so a baseline and a reading cannot be different questions."""
        assert SUBMIT_STATE_SCRIPT.count("confirmations") >= 1
        assert "location.href" in SUBMIT_STATE_SCRIPT

    def test_the_state_script_uses_the_shared_challenge_rule(self) -> None:
        assert ACTIVE_CAPTCHA_JS in SUBMIT_STATE_SCRIPT

    def test_the_blocker_rules_are_the_python_ones(self) -> None:
        assert json.dumps(list(VALIDATION_REGION_SELECTORS)) in SUBMIT_STATE_SCRIPT
        assert json.dumps(VALIDATION_TEXT.pattern) in SUBMIT_STATE_SCRIPT

    def test_the_target_script_marks_the_form_it_is_watching(self) -> None:
        """"That form disappeared" needs a way to say *which* form."""
        assert "setAttribute" in SUBMIT_TARGET_SCRIPT
