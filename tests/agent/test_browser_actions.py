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
from typing import Any, Iterable, Mapping, Sequence

import pytest

from app.agent.browser_actions import (
    CONFIRMATION_TEXT,
    FINAL_SUBMIT_PHRASES,
    GUARD_SCRIPT,
    HUMAN_ONLY_FIELD_TYPES,
    SUBMIT_CANDIDATES_JS,
    SUBMIT_COUNT_SCRIPT,
    SUBMIT_RESOLVE_SCRIPT,
    SUBMIT_SIGNAL_SCRIPT,
    SUBMIT_TARGET_SCRIPT,
    SUPPORTED_FIELD_TYPES,
    WRITE_COUNT_SCRIPT,
    WRITE_SCRIPT,
    PlaywrightFieldWriter,
    PlaywrightPageGuard,
    PlaywrightSubmitter,
    is_confirmation_text,
    is_final_submit_name,
    normalize_control_name,
)
from app.agent.errors import (
    CaptchaEncountered,
    FieldNotUniquelyResolved,
    FieldProvenanceMismatch,
    FieldWriteNotVerified,
    FieldWriteRefused,
    FinalSubmitControlAmbiguous,
    FinalSubmitControlNotFound,
    LoginWallEncountered,
    SubmitNotAuthorized,
    UnsupportedFieldControl,
)
from app.agent.form_scanner import (
    FIELD_IDENTITY_JS,
    PAGE_TRAVERSAL_JS,
    FormField,
    FormScanner,
)
from app.agent.graph import SubmitAuthorization
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
    """A resolved control handle with a plausible bounding box."""

    def __init__(self, box: Mapping[str, float] | None = None) -> None:
        self.box = dict(box or {"x": 10.0, "y": 20.0, "width": 120.0, "height": 36.0})
        self.disposed = False
        self.scrolled = False

    def as_element(self) -> "FakeElement":
        return self

    async def bounding_box(self) -> dict[str, float]:
        return dict(self.box)

    async def scroll_into_view_if_needed(self) -> None:
        self.scrolled = True

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
    return FakeFrame(
        url,
        {
            WRITE_COUNT_SCRIPT: {"count": count},
            WRITE_SCRIPT: {
                "ok": True,
                "reason": "",
                "count": count,
                "matched": matched,
                "identity": dict(identity or resolved_identity(target)),
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
        """
        main = guarding_frame()
        silent = FakeFrame(
            "https://ats.example.com/pending",
            {GUARD_SCRIPT: _never_answers},
            parent=main,
        )

        await PlaywrightPageGuard(frame_timeout_ms=20).inspect(FakePage(main, [silent]))

    async def test_a_verdict_from_a_frame_that_did_answer_still_counts(self) -> None:
        """The timeout skips one frame, not the inspection."""
        main = FakeFrame(MAIN_URL, {GUARD_SCRIPT: _never_answers})
        answering = guarding_frame(
            captcha="div.g-recaptcha", url="https://ats.example.com/challenge", parent=main
        )

        with pytest.raises(CaptchaEncountered):
            await PlaywrightPageGuard(frame_timeout_ms=20).inspect(
                FakePage(main, [answering])
            )


async def _never_answers(argument: Any) -> Any:
    """Stands in for a frame whose execution context never arrives."""
    await asyncio.sleep(30)
    raise AssertionError("the guard waited for a frame that was never going to answer")


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
            "data-sitekey",
        ],
    )
    def test_it_knows_the_common_challenge_widgets(self, marker: str) -> None:
        assert marker in GUARD_SCRIPT

    def test_it_requires_a_challenge_to_be_visible(self) -> None:
        """An invisible reCAPTCHA v3 badge challenges nobody."""
        assert "isVisible(" in GUARD_SCRIPT

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


def submitting_frame(
    *,
    accepted: Sequence[str] = ("Submit application",),
    rejected: Sequence[Mapping[str, str]] = (),
    signals: Sequence[Mapping[str, Any]] = ({"confirmed": "role=status"},),
    url: str = MAIN_URL,
    parent: FakeFrame | None = None,
    element: FakeElement | None = None,
) -> FakeFrame:
    """A frame with a submit control and a scripted sequence of signals."""
    remaining = list(signals)

    def next_signal(_argument: Any) -> dict[str, Any]:
        current = remaining[0] if len(remaining) == 1 else remaining.pop(0)
        return {
            "navigated": "",
            "confirmed": "",
            "formGone": "",
            **current,
        }

    return FakeFrame(
        url,
        {
            SUBMIT_COUNT_SCRIPT: {
                "count": len(accepted),
                "accepted": list(accepted),
                "rejected": [dict(entry) for entry in rejected],
            },
            SUBMIT_RESOLVE_SCRIPT: element if element is not None else FakeElement(),
            SUBMIT_TARGET_SCRIPT: {"ok": True, "url": url, "marked": True},
            SUBMIT_SIGNAL_SCRIPT: next_signal,
        },
        parent=parent,
    )


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
            await submitter().submit(ForbiddenPage(), authorization)

    async def test_an_approved_decision_is_carried_out(self) -> None:
        frame = submitting_frame()

        outcome = await submitter().submit(FakePage(frame), APPROVED)

        assert outcome.submitted

    async def test_auto_submit_with_nothing_blocking_is_carried_out(self) -> None:
        frame = submitting_frame()
        authorization = dataclasses.replace(
            APPROVED, decision="", gate="auto_submit", blocking_reasons=()
        )

        assert (await submitter().submit(FakePage(frame), authorization)).submitted

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

        assert (await submitter().submit(page, APPROVED)).submitted
        assert page.mouse.presses == 1

    async def test_no_control_is_a_refusal_and_no_click(self) -> None:
        frame = submitting_frame(
            accepted=(),
            rejected=({"name": "next", "reason": "never a final submit"},),
        )
        page = FakePage(frame)

        with pytest.raises(FinalSubmitControlNotFound) as raised:
            await submitter().submit(page, APPROVED)

        assert page.mouse.presses == 0
        assert "next" in str(raised.value)

    async def test_two_controls_in_one_frame_are_a_refusal_and_no_click(self) -> None:
        frame = submitting_frame(accepted=("Submit application", "Submit"))
        page = FakePage(frame)

        with pytest.raises(FinalSubmitControlAmbiguous):
            await submitter().submit(page, APPROVED)

        assert page.mouse.presses == 0

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
            await submitter().submit(page, APPROVED)

        assert page.mouse.presses == 0

    async def test_a_cross_origin_frame_is_never_asked_for_a_submit_control(
        self,
    ) -> None:
        main = submitting_frame()
        foreign = FakeFrame("https://third-party.example/ad", {}, parent=main)

        await submitter().submit(FakePage(main, [foreign]), APPROVED)

        assert foreign.calls == []


class TestReportingASubmission:
    """`submitted` is a claim about the page, never about the click."""

    @pytest.mark.parametrize(
        "signal,expected",
        [
            ({"confirmed": "role=status"}, "confirmation"),
            ({"navigated": "https://ats.example.com/thanks"}, "navigat"),
            ({"formGone": "the application form was removed"}, "form"),
        ],
    )
    async def test_each_concrete_signal_is_enough(
        self, signal: dict[str, Any], expected: str
    ) -> None:
        frame = submitting_frame(signals=(signal,))

        outcome = await submitter().submit(FakePage(frame), APPROVED)

        assert outcome.submitted
        assert expected in outcome.reason

    async def test_a_signal_that_arrives_late_is_still_waited_for(self) -> None:
        frame = submitting_frame(
            signals=({}, {}, {"confirmed": "role=status"}),
        )

        assert (await submitter().submit(FakePage(frame), APPROVED)).submitted
        assert frame.times_run(SUBMIT_SIGNAL_SCRIPT) >= 3

    async def test_no_signal_at_all_is_not_a_submission(self) -> None:
        frame = submitting_frame(signals=({},))

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED
        )

        assert outcome.submitted is False
        assert "no" in outcome.reason.lower()

    async def test_an_unconfirmed_outcome_is_never_clicked_a_second_time(self) -> None:
        """The one thing worse than an unconfirmed submission is two."""
        frame = submitting_frame(signals=({},))
        page = FakePage(frame)

        await submitter(confirm_timeout_ms=400).submit(page, APPROVED)

        assert page.mouse.presses == 1

    async def test_the_unconfirmed_reason_says_what_was_looked_for(self) -> None:
        frame = submitting_frame(signals=({},))

        outcome = await submitter(confirm_timeout_ms=400).submit(
            FakePage(frame), APPROVED
        )

        assert "confirmation" in outcome.reason
        assert "navigation" in outcome.reason


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

        outcome = await submitter(screenshots=shots).submit(FakePage(frame), APPROVED)

        assert outcome.screenshot_path == "/artifacts/submitted.png"

    async def test_an_unconfirmed_submission_is_photographed_too(self) -> None:
        """This is the screenshot an operator most needs to look at."""
        shots = self.Shots()
        frame = submitting_frame(signals=({},))

        outcome = await submitter(confirm_timeout_ms=400, screenshots=shots).submit(
            FakePage(frame), APPROVED
        )

        assert outcome.screenshot_path == "/artifacts/unconfirmed.png"

    async def test_a_failed_screenshot_does_not_change_the_outcome(self) -> None:
        class Broken:
            async def capture(self, page: Any, name: str) -> str | None:
                raise RuntimeError("no disk")

        frame = submitting_frame()

        outcome = await submitter(screenshots=Broken()).submit(FakePage(frame), APPROVED)

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
        assert CONFIRMATION_TEXT.pattern in SUBMIT_SIGNAL_SCRIPT


class TestTheSubmitterScripts:
    @pytest.mark.parametrize(
        "script",
        [SUBMIT_COUNT_SCRIPT, SUBMIT_RESOLVE_SCRIPT, SUBMIT_SIGNAL_SCRIPT],
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

    def test_the_signal_script_looks_for_all_three_signals(self) -> None:
        for signal in ("navigated", "confirmed", "formGone"):
            assert signal in SUBMIT_SIGNAL_SCRIPT

    def test_the_target_script_marks_the_form_it_is_watching(self) -> None:
        """"That form disappeared" needs a way to say *which* form."""
        assert "setAttribute" in SUBMIT_TARGET_SCRIPT
