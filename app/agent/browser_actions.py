"""The three components that act on a live application form.

Everything else in this project reads a page. These three change one: they
type a decided answer into a control, refuse to keep working on a challenged
page, and perform the last click. That makes them the only place where
getting something wrong has a consequence somebody else has to live with, so
each one is built around a refusal rather than around an action.

**The writer types into exactly one control, or nothing.** It is given the
whole scanned `FormField`, not just its opaque key, because a digest cannot
be used to find a control again — the frame, the form, the id, the name, the
type, and the shadow path the key was derived from are what locate it. It
searches every same-origin frame and every open shadow root, requires
exactly one visible enabled match, dispatches the events a page framework
actually listens for, and then asks the page whether the control now holds
the value. When it shares the scanner that produced the field, it also
re-derives the stable key from the control it found and refuses to type into
one whose key is not the key the answer will be recorded against. Files,
passwords, checkboxes, radios, and multi-selects are refused outright: those
are the applicant's to operate.

**The guard reads structure, never prose.** "Sign in" is in the header of
every job board and "verify" is on half of all forms; a guard matching those
would abandon applications that were perfectly fillable, and the operator
would see `login_required` with no way to know it was wrong. So the markers
are the things that are only ever there when a challenge or a credential
prompt genuinely is: an *active*
reCAPTCHA/hCaptcha/Turnstile/Arkose challenge — the challenge frame, a
widget rendered at a size a person could use, or one in a modal dialog —
and a *visible* password field. The markup an invisible or v3 site key
leaves on a page that challenges nobody is not one of them.

**The submitter reports a submission only when the page says so.** It
refuses without an approval, requires exactly one visible control whose
accessible name is on an explicit allowlist, never clicks Next or Continue
or Save, and lets the driver verify the control is genuinely clickable
before anything is spent on this application. Afterwards it compares each
target with *its own* pre-click reading and accepts two things: a
confirmation the page was not already showing, or a destination only a
submission arrives at together with the form it clicked in being gone.
Navigation alone is not one of them. No signal means `submitted=False` and
no second click, because the only thing worse than an unconfirmed
submission is two of them.

Playwright is never imported here. Pages, frames, elements, and the mouse
are all duck-typed, so all of the above is unit-testable without a browser;
what the doubles cannot check is the JavaScript against a real DOM, which is
what `tests/integration/test_stub_extension.py` is for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

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
    SubmitNotAuthorized,
    UnsupportedFieldControl,
)
from app.agent.form_scanner import (
    FIELD_IDENTITY_JS,
    PAGE_TRAVERSAL_JS,
    TEXTUAL_INPUT_TYPES,
    FormField,
    FormScanner,
    frame_chain,
    same_origin_frames,
)
from app.agent.graph import SubmitAuthorization, SubmitOutcome, SubmitPermit
from app.agent.humanize import Humanizer

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class Screenshotter(Protocol):
    async def capture(self, page: Any, name: str) -> str | None: ...


class PageGuardLike(Protocol):
    async def inspect(self, page: Any) -> None: ...


# --------------------------------------------------------------------------
# Which controls may be typed into
# --------------------------------------------------------------------------

#: Control types this writer will type into: free-form text inputs, prose
#: areas, and single-choice selects. `password` is removed from the
#: scanner's textual set on purpose — a credential is not an answer to a
#: question, and typing one would move it out of a plaintext answers file
#: and into a page.
SUPPORTED_FIELD_TYPES: frozenset[str] = (
    TEXTUAL_INPUT_TYPES - {"password"}
) | frozenset({"textarea", "select-one"})

#: Control types the applicant operates themselves, named explicitly so the
#: refusal can say which kind it refused. The gap filler already routes
#: files, passwords, and checkboxes to a human; this list is the second,
#: independent refusal, because a writer that trusted its caller's
#: classification would only be as safe as that classification.
HUMAN_ONLY_FIELD_TYPES: frozenset[str] = frozenset(
    {
        "file",
        "password",
        "checkbox",
        "radio",
        "select-multiple",
        "contenteditable",
        "hidden",
        "date",
        "datetime-local",
        "month",
        "week",
        "time",
        "color",
        "range",
        "submit",
        "button",
        "reset",
        "image",
    }
)


# --------------------------------------------------------------------------
# Accessible-name rules, shared with the page
# --------------------------------------------------------------------------

_NAME_NOISE = re.compile(r"[^a-z0-9]+")

#: Accessible names that mean "this is the last click". An explicit list of
#: exact folded phrases rather than a pattern, because a pattern one word
#: too broad clicks "Save and continue" on a half-filled form. Spliced
#: verbatim into `SUBMIT_CANDIDATES_JS`, so the rule the page applies is
#: this one.
#:
#: "Apply" and "Apply now" are deliberately absent. They are also the words
#: that *start* an application on every board this project drives, and
#: guessing wrong in that direction opens somebody's application flow. A
#: form whose only final control says "Apply" is reported as having no
#: final-submit control and left filled for the applicant.
FINAL_SUBMIT_PHRASES: tuple[str, ...] = (
    "submit",
    "submit application",
    "submit my application",
    "submit the application",
    "submit this application",
    "submit job application",
    "submit your application",
    "send application",
    "send my application",
    "submit and apply",
    "complete application",
    "complete your application",
    "finish and submit",
)

#: Words that disqualify a control however it is otherwise worded. The
#: allowlist above is exact phrases, so this is belt and braces — but it is
#: cheap, and it is what stops a later addition to that list from quietly
#: accepting "Submit and continue".
NEVER_SUBMIT_NAME = re.compile(
    r"\b(next|continue|back|previous|prev|save|review|draft|cancel|close|"
    r"upload|attach|add|remove|delete|clear|search|filter|sign in|signin|"
    r"log in|login|register|create account|preview|edit|later|skip|help)\b"
)

#: What a confirmation reads like. Used only *after* a final-submit control
#: has been clicked and only inside a status, alert, or heading region, which
#: is why prose is acceptable here and not in the page guard: this decides
#: whether something already done succeeded, not whether to act at all.
#:
#: Matching this is necessary but never sufficient: the text also has to be
#: text the page was *not* already showing before the click. See
#: `submission_verdict`.
CONFIRMATION_TEXT = re.compile(
    r"application (was |has been |is )?(successfully )?(submitted|received|complete[d]?)"
    r"|your application (was |has been )?(submitted|received)"
    r"|thank you for (applying|your application|submitting)"
    r"|we (have )?received your application"
    r"|(submitted|sent) successfully"
    r"|successfully (submitted|sent|applied)",
    re.IGNORECASE,
)


def normalize_control_name(name: str) -> str:
    """Fold an accessible name to compare it with the allowlist.

    Lowercase, and every run of anything that is not a letter or a digit
    becomes one space, so "Submit application →", "SUBMIT   Application" and
    "Submit Application!" are all the same name. Mirrored in
    `SUBMIT_CANDIDATES_JS`.
    """
    return _NAME_NOISE.sub(" ", (name or "").casefold()).strip()


def is_final_submit_name(name: str) -> bool:
    """Whether this accessible name is one this build will click to submit."""
    folded = normalize_control_name(name)
    if not folded:
        return False
    if NEVER_SUBMIT_NAME.search(folded):
        return False
    return folded in FINAL_SUBMIT_PHRASES


def is_confirmation_text(text: str) -> bool:
    """Whether this text is a page saying the application went through."""
    if not text or not text.strip():
        return False
    if re.search(r"\b(could not|cannot|failed|unable|before submitting)\b", text, re.IGNORECASE):
        return False
    return CONFIRMATION_TEXT.search(text) is not None


# --------------------------------------------------------------------------
# Where a submitted application ends up, and where it does not
# --------------------------------------------------------------------------

#: Paths that only a completed submission leads to. Matched against the
#: path alone, and never on its own: a navigation is the weakest thing a
#: page can do in response to a click, so this has to be corroborated by the
#: form the control belonged to being gone.
SUCCESS_PATH = re.compile(
    r"(^|[/_.-])"
    r"(thank[-_]?you|thanks|confirmation|confirmed|submitted|success)"
    r"([/_.-]|$)",
    re.IGNORECASE,
)

#: A URL saying the opposite with the same words. `?submitted=false` is how
#: an ATS records a draft, and `/application/not-submitted` is how one
#: reports the state of it — both matched a bare word list, on the one path
#: where a vanished form is already half the evidence.
NEGATED_SUCCESS = re.compile(
    r"(^|[/_.-])(not|non|un|no)[-_]?(submitted|confirmed|success|complete[d]?)",
    re.IGNORECASE,
)

#: Query keys that can carry a claim about the submission, and the values
#: that count as making it. Nothing else does: a key whose value is missing,
#: empty, `false`, `0`, or anything not on this list is a page reporting
#: state, not a page reporting success.
SUCCESS_QUERY_KEY = re.compile(
    r"(^|[_.-])(submitted|confirmed|confirmation|success|complete[d]?)([_.-]|$)",
    re.IGNORECASE,
)
TRUTHY_QUERY_VALUES: frozenset[str] = frozenset(
    {"1", "t", "y", "true", "yes", "ok", "success", "submitted", "confirmed", "complete", "completed", "done"}
)

#: The values that make the opposite claim, which overrides even a path only
#: a submission is supposed to reach. Anything on neither list — a
#: confirmation id, a reference number — is not a claim about the state of
#: the application, and is left out of the decision entirely.
FALSEY_QUERY_VALUES: frozenset[str] = frozenset(
    {
        "",
        "0",
        "f",
        "n",
        "no",
        "off",
        "false",
        "none",
        "null",
        "pending",
        "draft",
        "incomplete",
        "unsubmitted",
        "unconfirmed",
    }
)

#: URLs a submission is never at the end of. A board that bounces an expired
#: session to a sign-in page, a validation round trip that reloads with an
#: error banner, and an enforcement challenge all navigate exactly like a
#: success does, and the first of those is the common case on an application
#: that sat at the approval gate overnight.
REFUSED_DESTINATION = re.compile(
    r"(^|[/?&=_.-])"
    r"(log[-_]?in|sign[-_]?in|signin|signon|auth|authenticate|authwall|sso|"
    r"register|captcha|challenge|verify|verification|error|errors|failed|"
    r"failure|denied|forbidden|unauthori[sz]ed|expired|session[-_]?expired)"
    r"([/?&=_.-]|$)",
    re.IGNORECASE,
)

#: Regions a page puts a rejection in. Deliberately wide, because a false
#: match here costs an honest "not confirmed" rather than a wrong
#: "submitted" — and because the text still has to read like a rejection,
#: and the whole marker still has to be one the page was not already
#: showing before the click.
VALIDATION_REGION_SELECTORS: tuple[str, ...] = (
    '[role="alert"]',
    '[class*="error" i]',
    '[id*="error" i]',
    '[class*="invalid" i]',
    '[class*="validation" i]',
)

#: What a rejection reads like.
VALIDATION_TEXT = re.compile(
    r"\b(is required|are required|required field|this field is|"
    r"please (enter|fill|select|choose|provide|correct|complete|review|try)|"
    r"cannot be (blank|empty)|must be|is not valid|is invalid|invalid|"
    r"something went wrong|an error occurred|there (was|were) (an |a )?error|"
    r"could not be (submitted|saved|processed)|failed to submit|try again)\b",
    re.IGNORECASE,
)

#: Structural evidence, as `(selector, what it means)`, that the page did
#: not take the submission. A password field appearing where there was none
#: is a session that expired between the approval and the click.
SUBMISSION_BLOCKER_SELECTORS: tuple[tuple[str, str], ...] = (
    ('input[type="password"]', "a sign-in prompt"),
    ('[aria-invalid="true"]', "a control marked invalid"),
)


def is_success_destination(url: str) -> bool:
    """Whether this URL is one only a submitted application arrives at.

    The query outranks the path, in both directions. A board that renders
    `/thank-you` for every state of an application and reports which state
    in a flag is saying "not this one" when the flag reads `submitted=false`,
    and a path word list that overrode it would read the page's own denial
    as a success.
    """
    parts = urlsplit(url)
    claim = _query_claim(parts.query)
    if claim is False:
        return False
    if NEGATED_SUCCESS.search(parts.path):
        return False
    if claim is True:
        return True
    return bool(SUCCESS_PATH.search(parts.path))


def _query_claim(query: str) -> bool | None:
    """What this query says about the submission, if it says anything.

    `True` for an affirmative claim, `False` for a denial, and `None` for a
    query that says nothing about the submission — a job id, a campaign tag,
    or a `confirmation_id` whose value is a reference rather than a state,
    all of which leave the path to speak for itself. A contradiction counts
    as a denial, because half a page saying no is a page to check by hand.
    """
    claimed = False
    for key, value in parse_qsl(query, keep_blank_values=True):
        folded = _CAMEL_BOUNDARY.sub("_", key).casefold()
        if NEGATED_SUCCESS.search(folded):
            return False
        if not SUCCESS_QUERY_KEY.search(folded):
            continue
        spoken = value.strip().casefold()
        if spoken in FALSEY_QUERY_VALUES:
            return False
        if spoken in TRUTHY_QUERY_VALUES:
            claimed = True
    return True if claimed else None


#: Where one word ends and the next begins in `applicationSubmitted`, so a
#: camel-cased query key folds to the same shape as `application_submitted`.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def is_refused_destination(url: str) -> bool:
    """Whether this URL says the click went somewhere other than through."""
    parts = urlsplit(url)
    return bool(REFUSED_DESTINATION.search(f"{parts.path}?{parts.query}"))


@dataclass(frozen=True)
class ConfirmationRegion:
    """One region of one target that reads like a confirmation.

    `identity` is what makes a region *the same region* across two
    readings, and it deliberately does not involve the words in it: a
    standing thank-you panel that counts applications, cycles messages, or
    animates its wording is one region whose text changes, and comparing
    text alone made every one of its changes look like a confirmation
    arriving. See `SUBMIT_STATE_SCRIPT` for how a region is addressed.
    """

    identity: str
    text: str


@dataclass(frozen=True)
class PageState:
    """What one target looked like at one moment.

    Read before the click and again while waiting, by the same script
    against the same target, so that every comparison is like with like.
    The bug this type exists to make impossible was comparing a child
    frame's "before" with the top page's "after": the URLs differ by
    definition and the form is in neither document, so the first poll
    reported a navigation and a vanished form on a page where nothing had
    happened at all.
    """

    #: `location.href` of this target.
    url: str = ""
    #: Every region of this target that reads like a confirmation, each
    #: addressed by an identity that survives its own text changing.
    confirmations: tuple[ConfirmationRegion, ...] = ()
    #: Every reason to think the page refused the submission.
    blockers: tuple[str, ...] = ()
    #: Whether the form the submit control belonged to is in *this* target,
    #: and visible. False in a baseline means "not mine to watch".
    marked: bool = False

    @classmethod
    def from_report(cls, report: Mapping[str, Any]) -> "PageState":
        """Build one reading, dropping anything that cannot be compared.

        A region that arrives without both an identity and a text is not
        read as a confirmation at all: an unjudgeable reading costs an
        honest "not confirmed", and guessing at one costs an application.
        """
        regions: list[ConfirmationRegion] = []
        for entry in report.get("confirmations") or ():
            detail = _mapping(entry)
            identity = _text(detail.get("identity"))
            body = _text(detail.get("text"))
            if identity and body:
                regions.append(ConfirmationRegion(identity, body))
        return cls(
            url=_text(report.get("url")),
            confirmations=tuple(regions),
            blockers=tuple(_text(entry) for entry in report.get("blockers") or ()),
            marked=bool(report.get("marked")),
        )


@dataclass(frozen=True)
class SubmitVerdict:
    """What one target's before-and-after says about the submission.

    Three outcomes, not two. `signal` is a concrete reason to believe the
    application went through, `refusal` is a concrete reason to believe it
    did not, and neither means "nothing has happened yet, keep waiting".
    """

    signal: str = ""
    refusal: str = ""

    @property
    def decided(self) -> bool:
        return bool(self.signal or self.refusal)


def _fresh_confirmations(
    before: PageState, after: PageState
) -> tuple[ConfirmationRegion, ...]:
    """The regions that became confirmations because of this click.

    A region already reading like a confirmation before the click is not
    one, however its wording changes afterwards, and a region whose wording
    the target was already showing elsewhere is not one either however
    newly it appeared. What is left is a region that was not shaped like a
    confirmation and now is, saying something nobody was saying.
    """
    known_regions = {region.identity for region in before.confirmations}
    known_words = {region.text for region in before.confirmations}
    return tuple(
        region
        for region in after.confirmations
        if region.identity not in known_regions and region.text not in known_words
    )


def submission_verdict(before: PageState, after: PageState) -> SubmitVerdict:
    """Judge one target against its own pre-click state.

    Nothing is accepted on words alone. Every submission needs one of the
    two *structural* facts — the form the control belonged to gone from this
    target or no longer visible in it, or a navigation to a destination only
    a submission arrives at — and then either a new confirmation beside it,
    or both facts together.

    Text is the weakest evidence a page can offer, and every rule about
    which region said it is still a rule about text: a page that has
    genuinely taken an application does not go on showing the form it took.
    So a standing panel that repaints itself next to a form still sitting
    there cannot confirm anything, whatever it says and however it changes.

    A confirmation is new when a *region* that was not already shaped like
    one now is, and it says something the target was not already saying.
    Neither half is enough by itself: a counting panel is one region whose
    text keeps changing, and a rebuilt banner is a new region carrying the
    text it was already carrying.

    Navigation alone is not one of them, and neither is the form
    disappearing alone. Both were, and both are wrong in the same
    direction: a board that bounces an expired session to a sign-in page
    navigates, a validation round trip navigates, and a single-page ATS
    swapping in step two of three removes the form. Each of those was
    recorded as a submitted application, which is the worst outcome this
    project has — an approval spent, a queue row completed, and nothing
    sent.
    """
    fresh_blockers = tuple(
        blocker for blocker in after.blockers if blocker not in before.blockers
    )
    if fresh_blockers:
        return SubmitVerdict(
            refusal=(
                f"the page answered the click with {fresh_blockers[0]}, so the "
                "application was not accepted"
            )
        )

    moved = _normalized(after.url) != _normalized(before.url)
    if moved and is_refused_destination(after.url):
        return SubmitVerdict(
            refusal=(
                f"the click led to {after.url}, which is not a destination a "
                "submitted application arrives at"
            )
        )

    # The two structural facts. Either one corroborates a confirmation, and
    # together they are a submission on their own.
    form_gone = before.marked and not after.marked
    arrived = moved and is_success_destination(after.url)

    fresh = _fresh_confirmations(before, after)
    if fresh and (form_gone or arrived):
        return SubmitVerdict(
            signal=(
                "the page showed a confirmation it was not showing before the "
                f"click: {fresh[0].text!r}, and "
                + (
                    "the form the submit control belonged to is gone"
                    if form_gone
                    else f"it is at {after.url}, which only a submission arrives at"
                )
            )
        )

    if form_gone and arrived:
        return SubmitVerdict(
            signal=(
                f"the page navigated to {after.url}, which only a submission "
                "arrives at, and the form the submit control belonged to did "
                "not come with it"
            )
        )
    return SubmitVerdict()


# --------------------------------------------------------------------------
# In-page scripts
# --------------------------------------------------------------------------

#: Resolves the controls in one frame that match a scanned field's identity,
#: using the scanner's own notion of a control's type, id, form, label, and
#: shadow path. Spliced into both writer scripts so counting and writing can
#: never disagree about what a match is.
_WRITE_CANDIDATES_JS = """
  const SUPPORTED_TYPES = new Set(__SUPPORTED_TYPES__);

  const matchesDescriptor = (el, want) => {
    if (el.tagName.toLowerCase() !== String(want.tag || '').toLowerCase()) {
      return false;
    }
    const type = controlType(el);
    if (!SUPPORTED_TYPES.has(type)) {
      return false;
    }
    if (type !== String(want.fieldType || '')) {
      return false;
    }
    if (text(el.getAttribute('name')) !== String(want.name || '')) {
      return false;
    }
    if (controlIdentity(el) !== String(want.controlId || '')) {
      return false;
    }
    if (formIdentity(el) !== String(want.form || '')) {
      return false;
    }
    if (!isVisible(el) || isDisabled(el)) {
      return false;
    }
    return true;
  };

  const identityOf = (el, entry) => ({
    shadowPath: entry.path,
    shadowDepth: entry.depth,
    form: formIdentity(el),
    id: controlIdentity(el),
    name: text(el.getAttribute('name')),
    type: controlType(el),
    label: labelText(el),
  });

  const findCandidates = (want) => {
    const found = [];
    for (const entry of collectRoots(document, 0, '', [])) {
      if (entry.path !== String(want.shadowPath || '')
          || entry.depth !== Number(want.shadowDepth || 0)) {
        continue;
      }
      let controls = [];
      try {
        controls = entry.root.querySelectorAll(CONTROL_SELECTOR);
      } catch (error) {
        continue;
      }
      for (const el of controls) {
        if (matchesDescriptor(el, want)) {
          found.push({ el, entry });
        }
      }
    }
    return found;
  };
"""


def _with_supported_types(source: str) -> str:
    return source.replace(
        "__SUPPORTED_TYPES__", json.dumps(sorted(SUPPORTED_FIELD_TYPES))
    )


WRITE_CANDIDATES_JS = _with_supported_types(_WRITE_CANDIDATES_JS)


#: Counts the controls in this frame that match a field's identity, and
#: describes the one it found. Never given the value: a frame that turns out
#: to hold two matches, or none, has no business receiving somebody's
#: answer — and neither does one whose single match is the wrong control,
#: which is what the identity is returned here to establish.
WRITE_COUNT_SCRIPT = (
    """
((want) => {
"""
    + FIELD_IDENTITY_JS
    + WRITE_CANDIDATES_JS
    + """
  const found = findCandidates(want || {});
  if (found.length !== 1) {
    return { count: found.length, identity: {} };
  }
  return { count: 1, identity: identityOf(found[0].el, found[0].entry) };
})
"""
).strip()


#: Types one value into the single matching control and reports whether the
#: control holds it afterwards. The comparison happens here rather than in
#: Python, so the value crosses the wire once (going in) and never comes
#: back out. The identity of the control that was written is returned so the
#: caller can re-derive its stable key.
WRITE_SCRIPT = (
    """
((request) => {
  const want = (request && request.field) || {};
  const value = String((request && request.value) || '');
"""
    + FIELD_IDENTITY_JS
    + WRITE_CANDIDATES_JS
    + """
  const refuse = (reason, count) => ({
    ok: false, reason, count, matched: false, identity: {},
  });

  const candidates = findCandidates(want);
  if (candidates.length !== 1) {
    return refuse('exactly one matching control is required', candidates.length);
  }
  const { el, entry } = candidates[0];

  // A framework-controlled input ignores `el.value = x`: React installs its
  // own value property on the element, so the assignment never reaches the
  // prototype setter the framework's onChange is watching for.
  const nativeSetter = (element) => {
    for (const ctor of [
      globalThis.HTMLInputElement,
      globalThis.HTMLTextAreaElement,
      globalThis.HTMLSelectElement,
    ]) {
      if (ctor && element instanceof ctor) {
        const descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, 'value');
        if (descriptor && typeof descriptor.set === 'function') {
          return descriptor.set;
        }
      }
    }
    return null;
  };

  const assign = (element, next) => {
    const setter = nativeSetter(element);
    if (setter) {
      setter.call(element, next);
    } else {
      element.value = next;
    }
  };

  const fire = (element) => {
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const identity = identityOf(el, entry);

  try {
    el.focus({ preventScroll: true });
  } catch (error) {
    // A control that cannot take focus can still take a value.
  }

  if (el.tagName.toLowerCase() === 'select') {
    const wanted = value.trim().toLowerCase();
    const options = Array.from(el.options || []);
    const hits = options.filter((option) => (
      String(option.value || '').trim().toLowerCase() === wanted
      || text(option.textContent).toLowerCase() === wanted
    ));
    if (hits.length !== 1) {
      return refuse(
        hits.length === 0
          ? 'no option of this select holds the decided answer'
          : 'several options of this select hold the decided answer',
        1,
      );
    }
    if (hits[0].disabled) {
      return refuse('the only matching option is disabled', 1);
    }
    el.selectedIndex = options.indexOf(hits[0]);
    fire(el);
    return {
      ok: true,
      reason: '',
      count: 1,
      matched: isFilled(el) && controlValue(el) === String(hits[0].value),
      identity,
    };
  }

  assign(el, value);
  fire(el);
  try {
    el.blur();
  } catch (error) {
    // Blurring is how some forms validate; not being able to is not fatal.
  }

  return {
    ok: true,
    reason: '',
    count: 1,
    matched: controlValue(el) === value,
    identity,
  };
})
"""
).strip()


# --------------------------------------------------------------------------
# What counts as a challenge, shared between the guard and the submitter
# --------------------------------------------------------------------------

#: The markup a challenge *frame* or an enforcement interstitial puts on a
#: page. Each of these only exists while somebody is being asked to prove
#: they are human: `bframe` is reCAPTCHA's challenge popup (as opposed to
#: `anchor`, which is the checkbox or the invisible hook), and the rest are
#: products that take over the page rather than sit in a corner of it.
CAPTCHA_CHALLENGE_SELECTORS: tuple[str, ...] = (
    'iframe[src*="recaptcha/api2/bframe"]',
    'iframe[src*="recaptcha/enterprise/bframe"]',
    'iframe[title*="recaptcha challenge" i]',
    'iframe[src*="hcaptcha.com"][src*="challenge"]',
    "#hcaptcha-challenge",
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="arkoselabs.com"]',
    'iframe[src*="funcaptcha"]',
    "#arkose-enforcement",
    'iframe[src*="geo.captcha-delivery.com"]',
    "#px-captcha",
    "#captcha-challenge",
)

#: Widget containers, which count only once the page has actually rendered
#: one at a size a person could use. The same container is what an invisible
#: or score-based widget is mounted in, and that one renders at 0x0.
CAPTCHA_WIDGET_SELECTORS: tuple[str, ...] = (
    "div.g-recaptcha",
    "div.h-captcha",
    "div.cf-turnstile",
)

#: Markup that is present whether or not anybody was ever challenged, and
#: is therefore never evidence of one. `.grecaptcha-badge` is the v3 corner
#: badge, `data-size="invisible"` is the widget declaring that it will not
#: show itself, and `anchor` is the checkbox/hook iframe — reCAPTCHA v3
#: creates one on every page it scores.
PASSIVE_CAPTCHA_SELECTORS: tuple[str, ...] = (
    ".grecaptcha-badge",
    '[data-size="invisible"]',
    'iframe[src*="recaptcha/api2/anchor"]',
    'iframe[src*="recaptcha/enterprise/anchor"]',
)

#: Answers "is somebody being asked to prove they are human *right now*",
#: as the selector that says so, or an empty string. Spliced into the guard
#: and into the submitter's post-click signal script, so a challenge that
#: appears in response to the click is read by the same rule.
#:
#: Not a callable expression: a sequence of `const` declarations meant to be
#: pasted inside a script body that has already spliced `PAGE_TRAVERSAL_JS`.
_ACTIVE_CAPTCHA_JS = """
  const CAPTCHA_CHALLENGE_SELECTORS = __CAPTCHA_CHALLENGE_SELECTORS__;
  const CAPTCHA_WIDGET_SELECTORS = __CAPTCHA_WIDGET_SELECTORS__;
  const PASSIVE_CAPTCHA_SELECTOR = __PASSIVE_CAPTCHA_SELECTORS__.join(', ');
  const CAPTCHA_DIALOG_SELECTOR = '[role="dialog"], [aria-modal="true"], dialog[open]';
  const CAPTCHA_HINT_SELECTOR = [
    'iframe[src*="captcha" i]',
    'div.g-recaptcha',
    'div.h-captcha',
    'div.cf-turnstile',
  ].join(', ');

  // A rendered reCAPTCHA checkbox is 304x78 and an hCaptcha one is 300x74.
  // An invisible widget, and a v3 one, render at 0x0 in a container that is
  // otherwise identical markup, which is why size is part of the rule.
  const MIN_CAPTCHA_WIDGET_WIDTH = 100;
  const MIN_CAPTCHA_WIDGET_HEIGHT = 40;

  const queryAll = (root, selector) => {
    try {
      return Array.from(root.querySelectorAll(selector));
    } catch (error) {
      return [];
    }
  };

  const isPassiveCaptcha = (el) => {
    try {
      if (el.matches && el.matches(PASSIVE_CAPTCHA_SELECTOR)) {
        return true;
      }
      return Boolean(el.closest && el.closest('.grecaptcha-badge'));
    } catch (error) {
      return false;
    }
  };

  const isInteractiveSize = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width >= MIN_CAPTCHA_WIDGET_WIDTH
      && rect.height >= MIN_CAPTCHA_WIDGET_HEIGHT;
  };

  const activeCaptcha = (root) => {
    for (const selector of CAPTCHA_CHALLENGE_SELECTORS) {
      for (const el of queryAll(root, selector)) {
        if (isVisible(el) && !isDisabled(el) && !isPassiveCaptcha(el)) {
          return selector;
        }
      }
    }
    for (const selector of CAPTCHA_WIDGET_SELECTORS) {
      for (const el of queryAll(root, selector)) {
        if (isVisible(el) && !isPassiveCaptcha(el) && isInteractiveSize(el)) {
          return selector;
        }
      }
    }
    for (const dialog of queryAll(root, CAPTCHA_DIALOG_SELECTOR)) {
      if (!isVisible(dialog)) {
        continue;
      }
      for (const el of queryAll(dialog, CAPTCHA_HINT_SELECTOR)) {
        if (isVisible(el) && !isPassiveCaptcha(el)) {
          return 'a captcha inside ' + CAPTCHA_DIALOG_SELECTOR;
        }
      }
    }
    return '';
  };
"""


ACTIVE_CAPTCHA_JS = (
    _ACTIVE_CAPTCHA_JS.replace(
        "__CAPTCHA_CHALLENGE_SELECTORS__", json.dumps(list(CAPTCHA_CHALLENGE_SELECTORS))
    )
    .replace("__CAPTCHA_WIDGET_SELECTORS__", json.dumps(list(CAPTCHA_WIDGET_SELECTORS)))
    .replace(
        "__PASSIVE_CAPTCHA_SELECTORS__", json.dumps(list(PASSIVE_CAPTCHA_SELECTORS))
    )
)


#: Reports whether this frame is presenting a human-verification challenge
#: or asking for credentials, as the selector that matched.
#:
#: Structural only, and active only: see `ACTIVE_CAPTCHA_JS` for why a
#: reCAPTCHA badge or anchor widget is not a reason to abandon somebody's
#: application.
GUARD_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + ACTIVE_CAPTCHA_JS
    + """
  // A visible password field is the whole rule. A sign-in link in a header,
  // a form whose action merely contains "login", or a footer "Log in" is on
  // pages whose application form is perfectly fillable, and abandoning
  // those would be a silent, unattributable loss.
  const LOGIN_SELECTORS = [
    'input[type="password"]',
    'div.authwall',
    'section.authwall',
  ];

  const firstVisible = (root, selectors) => {
    for (const selector of selectors) {
      let found = [];
      try {
        found = root.querySelectorAll(selector);
      } catch (error) {
        continue;
      }
      for (const el of found) {
        if (isVisible(el) && !isDisabled(el)) {
          return selector;
        }
      }
    }
    return '';
  };

  let captcha = '';
  let login = '';
  for (const entry of collectRoots(document, 0, '', [])) {
    captcha = captcha || activeCaptcha(entry.root);
    login = login || firstVisible(entry.root, LOGIN_SELECTORS);
  }
  return { captcha, login };
})
"""
).strip()


#: Collects the controls in one frame that are recognisably a *final*
#: submit, using the Python allowlist and denylist verbatim. Spliced into
#: both the counting and the resolving script.
_SUBMIT_CANDIDATES_JS = """
  const FINAL_SUBMIT_PHRASES = new Set(__FINAL_SUBMIT_PHRASES__);
  const NEVER_SUBMIT_NAME = new RegExp(__NEVER_SUBMIT_NAME__, 'i');
  const SUBMIT_SELECTOR = [
    'button[type="submit"]',
    'input[type="submit"]',
    'form button:not([type])',
  ].join(', ');

  const foldName = (raw) => String(raw == null ? '' : raw)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();

  const submitName = (el) => {
    const explicit = text(
      el.getAttribute('aria-label') || el.getAttribute('value') || el.getAttribute('title'),
    );
    return foldName(explicit || el.textContent);
  };

  const judge = (el) => {
    const name = submitName(el);
    if (!name) {
      return { name, reason: 'the control has no accessible name' };
    }
    if (NEVER_SUBMIT_NAME.test(name)) {
      return { name, reason: 'the name contains a word that is never a final submit' };
    }
    if (!FINAL_SUBMIT_PHRASES.has(name)) {
      return { name, reason: 'the name is not one of the accepted final-submit phrases' };
    }
    if (!isVisible(el) || isDisabled(el)) {
      return { name, reason: 'the control is not visible and enabled' };
    }
    return { name, reason: '' };
  };

  const collectSubmitCandidates = () => {
    const accepted = [];
    const rejected = [];
    for (const entry of collectRoots(document, 0, '', [])) {
      let found = [];
      try {
        found = entry.root.querySelectorAll(SUBMIT_SELECTOR);
      } catch (error) {
        continue;
      }
      for (const el of found) {
        const verdict = judge(el);
        if (verdict.reason) {
          if (verdict.name) {
            rejected.push(verdict);
          }
        } else {
          accepted.push({ el, name: verdict.name });
        }
      }
    }
    return { accepted, rejected };
  };
"""


SUBMIT_CANDIDATES_JS = _SUBMIT_CANDIDATES_JS.replace(
    "__FINAL_SUBMIT_PHRASES__", json.dumps(list(FINAL_SUBMIT_PHRASES))
).replace("__NEVER_SUBMIT_NAME__", json.dumps(NEVER_SUBMIT_NAME.pattern))


SUBMIT_COUNT_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + SUBMIT_CANDIDATES_JS
    + """
  const { accepted, rejected } = collectSubmitCandidates();
  return {
    count: accepted.length,
    accepted: accepted.map((entry) => entry.name),
    rejected: rejected.map((entry) => ({ name: entry.name, reason: entry.reason })),
  };
})
"""
).strip()


SUBMIT_RESOLVE_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + SUBMIT_CANDIDATES_JS
    + """
  const { accepted } = collectSubmitCandidates();
  return accepted.length === 1 ? accepted[0].el : null;
})
"""
).strip()


#: Marks the form the submit control belongs to, so "that form disappeared"
#: can name *which* form rather than "some form is missing".
#:
#: The search crosses shadow boundaries outwards. A control inside a shadow
#: root is not form-associated with the form around its *host*, and
#: `closest` stops at the boundary — so a component-framework ATS, whose
#: submit button is exactly that, had no form to watch at all. Since the
#: form going is what corroborates a confirmation, that left the whole of
#: that shape unconfirmable.
SUBMIT_TARGET_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + SUBMIT_CANDIDATES_JS
    + """
  const MAX_HOST_HOPS = 8;

  const enclosingForm = (el) => {
    let node = el;
    for (let hop = 0; hop < MAX_HOST_HOPS; hop += 1) {
      const found = node.form || (node.closest ? node.closest('form') : null);
      if (found) {
        return found;
      }
      const root = node.getRootNode ? node.getRootNode() : null;
      const host = root && root.host ? root.host : null;
      if (!host) {
        return null;
      }
      node = host;
    }
    return null;
  };

  const { accepted } = collectSubmitCandidates();
  if (accepted.length !== 1) {
    return { ok: false, marked: false };
  }
  const form = enclosingForm(accepted[0].el);
  if (form) {
    form.setAttribute('data-jobright-submit-target', '1');
  }
  return { ok: true, marked: Boolean(form) };
})
"""
).strip()


#: Reads one target: where it is, what it is saying, and whether the marked
#: form is still in it. Run once before the click and repeatedly after,
#: against the same target each time, so the comparison in
#: `submission_verdict` is always like with like.
#:
#: Reports only readings. Whether a reading amounts to a submission is
#: decided in Python, where it can be tested directly rather than through a
#: substring of a script.
#:
#: Each confirmation-shaped region is reported with an identity as well as
#: its text: where the region is, derived from the shadow root it lives in
#: and its position among its ancestors, and stamped on the node so a region
#: that *moves* is still recognised. That is what stops a panel which counts,
#: ticks, cycles its wording, or is thrown away and built again from reading
#: as a confirmation arriving on every poll: `_fresh_confirmations` asks
#: which *regions* are new, and a counter is one region all along.
SUBMIT_STATE_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + ACTIVE_CAPTCHA_JS
    + """
  const CONFIRMATION_SELECTOR = [
    '[role="status"]',
    '[role="alert"]',
    '[aria-live]',
    '[id*="confirm" i]',
    '[class*="confirm" i]',
    '[data-application-submitted]',
    'h1',
    'h2',
  ].join(', ');
  const CONFIRMATION_TEXT = new RegExp(__CONFIRMATION_TEXT__, 'i');
  const NOT_A_CONFIRMATION = /\\b(could not|cannot|failed|unable|before submitting)\\b/i;
  const BLOCKER_SELECTORS = __BLOCKER_SELECTORS__;
  const VALIDATION_SELECTOR = __VALIDATION_REGION_SELECTORS__.join(', ');
  const VALIDATION_TEXT = new RegExp(__VALIDATION_TEXT__, 'i');
  const MAX_TEXT = 160;
  const MAX_PATH_STEPS = 12;
  const REGION_MARK = 'data-jobright-confirmation-region';

  const bodyText = (el) => String(el.textContent || '')
    .replace(/\\s+/g, ' ')
    .trim()
    .slice(0, MAX_TEXT);

  const add = (list, entry) => {
    if (entry && list.indexOf(entry) === -1) {
      list.push(entry);
    }
  };

  // Where a region is, expressed so that a second reading of the same place
  // arrives at the same answer. An id ends the walk, because an id is the
  // page's own name for the node; otherwise each step is the tag and its
  // position among same-tag siblings, up to the root of this tree.
  const structuralPath = (el) => {
    const steps = [];
    let node = el;
    while (node && node.nodeType === 1 && steps.length < MAX_PATH_STEPS) {
      let own = '';
      try {
        own = text(node.getAttribute('id'));
      } catch (error) {
        own = '';
      }
      if (own) {
        steps.push('#' + own);
        break;
      }
      const tag = node.tagName.toLowerCase();
      const parent = node.parentNode;
      if (!parent || !parent.children) {
        steps.push(tag);
        break;
      }
      let index = 1;
      for (const sibling of parent.children) {
        if (sibling === node) {
          break;
        }
        if (sibling.tagName === node.tagName) {
          index += 1;
        }
      }
      steps.push(tag + ':' + index);
      node = parent.nodeType === 1 ? parent : null;
    }
    return steps.reverse().join('>');
  };

  // How a region is addressed across readings, in the order the addresses
  // survive a repaint: one this reader has already stamped on the node, and
  // failing that where the region is. The stamp carries a region that moves;
  // the path finds one whose node was thrown away and built again, which is
  // what a framework does to its own subtree and which nothing on the node
  // itself can survive. Neither is invented: a token minted per node would
  // be a new identity on every reading, and a panel that rewrites itself
  // would read as a stream of confirmations arriving.
  const regionIdentity = (el, path) => {
    let stamped = '';
    try {
      stamped = text(el.getAttribute(REGION_MARK));
    } catch (error) {
      stamped = '';
    }
    if (stamped) {
      return stamped;
    }
    const derived = (path ? path + '>>' : '') + structuralPath(el);
    try {
      el.setAttribute(REGION_MARK, derived);
    } catch (error) {
      // A node that cannot be stamped is addressed by its path every
      // reading, which is the same answer the stamp would have given.
    }
    return derived;
  };

  const confirmations = [];
  const identities = [];
  const blockers = [];
  let marked = false;

  for (const entry of collectRoots(document, 0, '', [])) {
    for (const el of queryAll(entry.root, CONFIRMATION_SELECTOR)) {
      const body = bodyText(el);
      if (!body || !isVisible(el) || NOT_A_CONFIRMATION.test(body)) {
        continue;
      }
      if (CONFIRMATION_TEXT.test(body)) {
        const identity = regionIdentity(el, entry.path);
        if (identities.indexOf(identity) === -1) {
          identities.push(identity);
          confirmations.push({ identity, text: body });
        }
      }
    }

    for (const pair of BLOCKER_SELECTORS) {
      for (const el of queryAll(entry.root, pair[0])) {
        if (isVisible(el)) {
          add(blockers, pair[1] + ' (' + pair[0] + ')');
          break;
        }
      }
    }

    for (const el of queryAll(entry.root, VALIDATION_SELECTOR)) {
      const body = bodyText(el);
      if (body && isVisible(el) && VALIDATION_TEXT.test(body)) {
        add(blockers, 'a validation message: ' + JSON.stringify(body));
      }
    }

    const captcha = activeCaptcha(entry.root);
    if (captcha) {
      add(blockers, 'a human-verification challenge (' + captcha + ')');
    }

    if (!marked) {
      const target = queryAll(entry.root, 'form[data-jobright-submit-target]')[0];
      marked = Boolean(target) && isVisible(target);
    }
  }

  return { url: String(location.href), confirmations, blockers, marked };
})
"""
)
SUBMIT_STATE_SCRIPT = (
    SUBMIT_STATE_SCRIPT.replace(
        "__CONFIRMATION_TEXT__", json.dumps(CONFIRMATION_TEXT.pattern)
    )
    .replace(
        "__BLOCKER_SELECTORS__",
        json.dumps([list(pair) for pair in SUBMISSION_BLOCKER_SELECTORS]),
    )
    .replace(
        "__VALIDATION_REGION_SELECTORS__", json.dumps(list(VALIDATION_REGION_SELECTORS))
    )
    .replace("__VALIDATION_TEXT__", json.dumps(VALIDATION_TEXT.pattern))
    .strip()
)


# --------------------------------------------------------------------------
# Shared frame helpers
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is None else str(value)


async def _evaluate(frame: Any, script: str, argument: Any = None) -> Any:
    evaluate = getattr(frame, "evaluate", None)
    if evaluate is None:
        return None
    if argument is None:
        return await evaluate(script)
    return await evaluate(script, argument)


async def _dispose_quietly(handle: Any) -> None:
    dispose = getattr(handle, "dispose", None)
    if dispose is None:
        return
    try:
        await dispose()
    except Exception:  # noqa: BLE001 - handle cleanup is best effort
        pass


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------
# The field writer
# --------------------------------------------------------------------------


#: How long one frame is given to answer the writer. Same defect as the
#: guard's: an `about:blank` iframe still notionally navigating never
#: answers at all, and a worker stuck here is holding a half-filled form and
#: a lease nobody is renewing.
DEFAULT_WRITE_FRAME_TIMEOUT_MS = 3_000


class PlaywrightFieldWriter:
    """Types one decided answer into the one control a field names.

    `scanner` is the `FormScanner` the field came from, and sharing it is
    what turns "a control matching this metadata" into "the control this
    key was derived from": the key is re-derived from whatever was found
    and a mismatch refuses the write. A writer built without one still
    requires exactly one metadata match, which is the check that stops the
    wrong control being typed into; it simply cannot make the stronger
    statement.
    """

    def __init__(
        self,
        *,
        scanner: FormScanner | None = None,
        frame_timeout_ms: int = DEFAULT_WRITE_FRAME_TIMEOUT_MS,
    ) -> None:
        self._scanner = scanner
        self._frame_timeout_s = max(0.001, frame_timeout_ms / 1000)

    @property
    def scanner(self) -> FormScanner | None:
        """The scanner whose key derivation this writer checks against.

        Readable so the wiring can be asserted on: a writer holding a
        different scanner from the one that produced the fields would refuse
        every write, and that is a property of `build_dependencies` worth a
        test rather than a comment.
        """
        return self._scanner

    async def write(self, page: Any, field: FormField, value: str) -> bool:
        """Type `value` into `field`, reporting whether it landed.

        Every refusal is contained here and reported as `False`, because a
        gap that could not be typed is not a broken application: the graph
        records the gap as unfilled, which keeps the approval gate in play
        and leaves the other answers on the form. Anything that is *not* a
        refusal — a detached frame, a closed page — propagates, because that
        is a failure of the run rather than a judgement about one field.
        """
        try:
            return await self.write_or_raise(page, field, value)
        except FieldWriteRefused as refusal:
            # Never `refusal.key`'s value: this reaches a log file.
            logger.warning("%s", refusal)
            return False

    async def write_or_raise(self, page: Any, field: FormField, value: str) -> bool:
        """`write`, with the refusal raised instead of reported."""
        self._require_supported(field)
        frames = self._candidate_frames(page, field)
        if not frames:
            raise FieldNotUniquelyResolved(
                field.key,
                0,
                f"no same-origin frame is still showing {field.frame_url!r}",
            )

        descriptor = self._descriptor(field)
        matches: list[tuple[Any, Mapping[str, Any]]] = []
        total = 0
        for frame in frames:
            try:
                counted = _mapping(await self._ask(frame, WRITE_COUNT_SCRIPT, descriptor))
            except (Exception, asyncio.TimeoutError) as error:  # noqa: BLE001
                # Not skipped: this frame could be the one holding a second
                # match, and "exactly one control answers to this key" is
                # the check that stops an answer going into the wrong box.
                raise FieldNotUniquelyResolved(
                    field.key,
                    total,
                    f"a same-origin frame stopped answering while its controls "
                    f"were being counted ({error!r})",
                ) from error
            found = int(counted.get("count", 0))
            total += found
            if found:
                matches.append((frame, _mapping(counted.get("identity"))))
        if total != 1:
            raise FieldNotUniquelyResolved(
                field.key,
                total,
                f"across {len(frames)} same-origin frame(s)",
            )

        frame, counted_identity = matches[0]
        # Before the value goes anywhere: a refusal after the write would be
        # honestly reported and still leave somebody's answer in the wrong
        # control.
        self._require_provenance(field, frame, counted_identity)
        try:
            result = _mapping(
                await self._ask(
                    frame, WRITE_SCRIPT, {"field": descriptor, "value": value}
                )
            )
        except (Exception, asyncio.TimeoutError) as error:  # noqa: BLE001
            # The value may already be in the control: the frame stopped
            # answering, which says nothing about what it did first. Reported
            # as dispatched-but-unverified, which is never retried.
            raise FieldWriteNotVerified(
                field.key,
                f"the frame stopped answering before it confirmed the value "
                f"landed ({error!r})",
            ) from error
        if not result.get("ok"):
            reported = int(result.get("count", 0))
            reason = _text(result.get("reason")) or "the page refused the write"
            if reported != 1:
                raise FieldNotUniquelyResolved(field.key, reported, reason)
            raise FieldWriteNotVerified(field.key, reason)

        # Again, on what was actually written: the two passes resolve the
        # control separately, and a page that repainted between them could
        # have moved it.
        self._require_provenance(field, frame, _mapping(result.get("identity")))
        if not result.get("matched"):
            raise FieldWriteNotVerified(field.key)
        return True

    async def _ask(self, frame: Any, script: str, argument: Any) -> Any:
        """One question to one frame, under this writer's per-frame deadline."""
        return await asyncio.wait_for(
            _evaluate(frame, script, argument), self._frame_timeout_s
        )

    def _require_supported(self, field: FormField) -> None:
        field_type = field.field_type.strip().lower()
        if field_type in HUMAN_ONLY_FIELD_TYPES or field_type not in SUPPORTED_FIELD_TYPES:
            raise UnsupportedFieldControl(field.key, field.tag, field.field_type)

    def _descriptor(self, field: FormField) -> dict[str, Any]:
        """What the page needs in order to find this control again.

        Deliberately not the value: this same descriptor is sent to every
        candidate frame while they are being counted, and a frame that turns
        out to hold two matches has no business receiving somebody's answer.
        """
        return {
            "tag": field.tag,
            "fieldType": field.field_type,
            "name": field.name,
            "controlId": field.control_id,
            "form": field.form,
            "shadowPath": field.shadow_path,
            "shadowDepth": field.shadow_depth,
        }

    def _candidate_frames(self, page: Any, field: FormField) -> list[Any]:
        """Every same-origin frame that could still be this field's frame.

        Narrowed by the frame's URL and its position in the frame tree, both
        of which are part of the key: two iframes with the same `src` are a
        very common ATS pattern, and a field scanned in the first of them
        must not be written into the second.
        """
        frames, _skipped = same_origin_frames(page)
        wanted_url = _normalized(field.frame_url)
        candidates: list[Any] = []
        for frame in frames:
            if wanted_url and _normalized(_text(getattr(frame, "url", ""))) != wanted_url:
                continue
            if frame_chain(frame) != field.frame_chain:
                continue
            candidates.append(frame)
        return candidates

    def _require_provenance(
        self, field: FormField, frame: Any, identity: Mapping[str, Any]
    ) -> None:
        """Refuse a control whose derived key is not the provenance key."""
        if self._scanner is None or not identity:
            return
        derived = self._scanner.stable_key(
            frame_chain=frame_chain(frame),
            frame_url=_text(getattr(frame, "url", "")) or field.frame_url,
            shadow_path=_text(identity.get("shadowPath")),
            shadow_depth=int(identity.get("shadowDepth") or 0),
            form=_text(identity.get("form")),
            control_id=_text(identity.get("id")),
            name=_text(identity.get("name")),
            field_type=_text(identity.get("type")),
            label=_text(identity.get("label")),
        )
        if derived != field.key:
            raise FieldProvenanceMismatch(field.key, derived)


def _normalized(url: str) -> str:
    """A frame URL without its fragment, matching the scanner's folding."""
    return url.split("#", 1)[0]


# --------------------------------------------------------------------------
# The page guard
# --------------------------------------------------------------------------


#: How long one frame is given to answer the guard. A frame that has no
#: execution context — an `about:blank` iframe still notionally navigating
#: is the common case — never answers at all, and the driver will wait for
#: that context far longer than anybody watching a queue would like.
DEFAULT_GUARD_FRAME_TIMEOUT_MS = 3_000


class PlaywrightPageGuard:
    """Refuses to keep working on a challenged or credential-gated page.

    A frame that cannot be evaluated contributes nothing rather than
    aborting the inspection: a navigating or detaching frame is routine, and
    the scanner already reports unreadable frames as a coverage gap, which
    is itself a blocking reason at the approval gate. This guard is an extra
    refusal, not the only one.

    That tolerance is bounded in time as well as in kind. A frame that
    simply never answers is skipped once its own deadline passes, so a page
    with one stuck frame costs a few seconds rather than holding a tab, a
    lease, and the whole queue behind it.
    """

    def __init__(
        self, *, frame_timeout_ms: int = DEFAULT_GUARD_FRAME_TIMEOUT_MS
    ) -> None:
        self._frame_timeout_s = max(0.001, frame_timeout_ms / 1000)

    async def inspect(self, page: Any) -> None:
        captcha = ""
        login = ""
        frames, _skipped = same_origin_frames(page)
        for frame in frames:
            try:
                report = _mapping(
                    await asyncio.wait_for(
                        _evaluate(frame, GUARD_SCRIPT), self._frame_timeout_s
                    )
                )
            except Exception:  # noqa: BLE001
                # One unreadable — or unresponsive, which arrives here as a
                # timeout — frame is not a verdict.
                continue
            captcha = captcha or _text(report.get("captcha"))
            login = login or _text(report.get("login"))

        # A challenge outranks a sign-in prompt: when both are on the page,
        # the challenge is what stops a human getting through too, and it is
        # the more specific thing to tell an operator.
        if captcha:
            raise CaptchaEncountered(captcha)
        if login:
            raise LoginWallEncountered(login)


# --------------------------------------------------------------------------
# The submitter
# --------------------------------------------------------------------------

#: How long a page is given to show that a submission went through. Generous
#: on purpose: the cost of waiting too long is a slow run, and the cost of
#: waiting too little is an application recorded as unconfirmed when it
#: actually succeeded — which an operator then has to check by hand.
DEFAULT_CONFIRM_TIMEOUT_MS = 20_000
DEFAULT_CONFIRM_POLL_MS = 200

#: How long any one frame is given to answer any one question, before or
#: after the press. Same defect as the guard's and the scanner's: a frame
#: with no execution context never answers, and here the stuck worker is
#: also holding a filled form, a lease nobody renews, and an approval
#: nobody can act on.
DEFAULT_SUBMIT_FRAME_TIMEOUT_MS = 3_000

#: How long the driver is given to satisfy itself that the control can be
#: clicked, and then to click it. Actionability is retried internally until
#: this elapses, which is what lets a banner finish animating out of the way
#: rather than turning a transient overlay into a refusal.
DEFAULT_CLICK_TIMEOUT_MS = 5_000


class PlaywrightSubmitter:
    """Performs the last click, and only says it worked when the page does."""

    def __init__(
        self,
        *,
        screenshots: Screenshotter | None = None,
        confirm_timeout_ms: int = DEFAULT_CONFIRM_TIMEOUT_MS,
        poll_interval_ms: int = DEFAULT_CONFIRM_POLL_MS,
        frame_timeout_ms: int = DEFAULT_SUBMIT_FRAME_TIMEOUT_MS,
        click_timeout_ms: int = DEFAULT_CLICK_TIMEOUT_MS,
        sleep: Sleeper = asyncio.sleep,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
        seed: int | None = None,
        max_area_fraction: float = 0.35,
        min_container_area: float = 40_000.0,
    ) -> None:
        self._screenshots = screenshots
        self._confirm_timeout_ms = confirm_timeout_ms
        self._poll_interval_ms = max(1, poll_interval_ms)
        self._frame_timeout_s = max(0.001, frame_timeout_ms / 1000)
        self._click_timeout_ms = click_timeout_ms
        self._sleep = sleep
        self._clock = clock
        self._rng = rng if rng is not None else random.Random(seed)
        self._humanizer = Humanizer(sleep=sleep, rng=self._rng)
        self._max_area_fraction = max_area_fraction
        self._min_container_area = min_container_area

    @property
    def screenshots(self) -> Screenshotter | None:
        """Where an unconfirmed submission's evidence goes, if anywhere.

        An outcome nobody can check is the worst one this class produces, so
        whether it can save an image is part of the wiring under test.
        """
        return self._screenshots

    async def submit(
        self, page: Any, authorization: SubmitAuthorization, permit: SubmitPermit
    ) -> SubmitOutcome:
        """Resolve, check, claim, click — and only ever in that order.

        Every reason not to press this page's control is found before the
        permit is claimed, because the claim is durable and spending it on a
        page nobody clicked leaves an application that can never be sent.
        Once the driver has confirmed the control is genuinely clickable
        there is nothing left between the claim and the pointer going down.
        """
        if not authorization.approved:
            raise SubmitNotAuthorized(
                authorization.application_id, authorization.refusal()
            )

        frame, name = await self._sole_submit_frame(page)
        try:
            before = _mapping(await self._ask(frame, SUBMIT_TARGET_SCRIPT))
        except Exception:  # noqa: BLE001 - a timeout arrives here too
            before = {}
        if not before.get("ok"):
            # The page changed between counting and marking, or stopped
            # answering. Refusing is the only safe answer: whatever is there
            # now was never counted, and a press with no pre-click baseline
            # has nothing to compare a signal against.
            raise FinalSubmitControlNotFound(
                (("", "the submit control disappeared before it could be clicked"),)
            )

        # Every target that will be watched afterwards is read *now*, and
        # each is only ever compared with its own reading. See `PageState`.
        baselines = await self._baselines(page, frame)
        await self._click_the_only_submit(page, frame, name, permit)
        return await self._await_confirmation(page, baselines, authorization)

    async def _baselines(self, page: Any, frame: Any) -> list[tuple[Any, PageState]]:
        """Read every target that will be asked about the outcome.

        The submitting frame is required: with no pre-click reading of it
        there is nothing a later reading could be compared against, and a
        press whose outcome cannot be judged is not one to make. Any other
        target that cannot be read is simply not watched.
        """
        captured: list[tuple[Any, PageState]] = []
        for target in _confirmation_targets(page, frame):
            try:
                state = PageState.from_report(
                    _mapping(await self._ask(target, SUBMIT_STATE_SCRIPT))
                )
            except Exception as error:  # noqa: BLE001 - a timeout arrives here too
                if target is frame:
                    raise FinalSubmitControlNotFound(
                        (
                            (
                                "",
                                "the frame holding the submit control could not "
                                f"be read before the click ({error!r}), so the "
                                "outcome of pressing it could not be judged",
                            ),
                        )
                    ) from error
                continue
            captured.append((target, state))
        return captured

    async def _ask(
        self, target: Any, script: str, argument: Any = None, *, budget: float | None = None
    ) -> Any:
        """Run one script against one target, under a deadline.

        Every question this class asks a page goes through here, because
        every one of them is asked of a frame that may have no execution
        context. `budget` narrows the per-frame allowance when a caller has
        an overall deadline of its own, so the sum of the per-frame waits
        can never outlive it.
        """
        limit = self._frame_timeout_s
        if budget is not None:
            limit = max(0.001, min(limit, budget))
        return await asyncio.wait_for(_evaluate(target, script, argument), limit)

    async def _sole_submit_frame(self, page: Any) -> tuple[Any, str]:
        """The one same-origin frame holding the one final-submit control.

        A frame that cannot be read contributes nothing rather than
        refusing the submission outright, for the same reason the guard
        tolerates one: a pending `about:blank` child is routine on ATS
        pages, and the scanner already travels a skipped frame as a
        coverage gap — which is a blocking reason at the approval gate, so
        the human who released this application saw it.
        """
        frames, _skipped = same_origin_frames(page)
        holders: list[Any] = []
        accepted: list[str] = []
        rejected: list[tuple[str, str]] = []
        for frame in frames:
            try:
                report = _mapping(await self._ask(frame, SUBMIT_COUNT_SCRIPT))
            except Exception:  # noqa: BLE001 - a timeout arrives here too
                continue
            names = [_text(name) for name in report.get("accepted") or ()]
            accepted.extend(names)
            for entry in report.get("rejected") or ():
                detail = _mapping(entry)
                rejected.append((_text(detail.get("name")), _text(detail.get("reason"))))
            if names:
                holders.append(frame)

        if len(accepted) > 1:
            raise FinalSubmitControlAmbiguous(accepted)
        if not accepted:
            raise FinalSubmitControlNotFound(rejected)
        return holders[0], accepted[0]

    async def _click_the_only_submit(
        self, page: Any, frame: Any, name: str, permit: SubmitPermit
    ) -> None:
        try:
            handle = await asyncio.wait_for(
                frame.evaluate_handle(SUBMIT_RESOLVE_SCRIPT), self._frame_timeout_s
            )
        except Exception as error:  # noqa: BLE001 - a timeout arrives here too
            raise FinalSubmitControlNotFound(
                (
                    (
                        "",
                        "the frame holding the counted submit control stopped "
                        f"answering before it could be resolved ({error!r})",
                    ),
                )
            ) from error
        as_element = getattr(handle, "as_element", None)
        element = as_element() if as_element is not None else handle
        if element is None:
            await _dispose_quietly(handle)
            raise FinalSubmitControlNotFound(
                (("", "the counted submit control could not be resolved again"),)
            )
        try:
            await self._press(page, element, name, permit)
        finally:
            await _dispose_quietly(element)

    async def _press(
        self, page: Any, element: Any, name: str, permit: SubmitPermit
    ) -> None:
        """Verify the control can be clicked, then let the driver click it.

        The press itself is the driver's own trusted click rather than a
        pointer event at the box's remembered centre, because between
        resolving a control and pressing it a cookie banner can animate in
        over it, a sticky footer can cover it, or the node can detach — and
        a coordinate press lands on whatever is actually there. The driver
        will not fire until the element is visible, stable, enabled, and the
        thing that genuinely receives an event at that point.

        The check runs first with the press withheld (`trial=True`), so
        every reason not to click is found before anything is spent on this
        application — and only then is the permit claimed. The pointer
        travels there before the claim, because moving a pointer submits
        nothing and the movement is what a page's own listeners see.
        """
        scroll = getattr(element, "scroll_into_view_if_needed", None)
        if scroll is not None:
            try:
                await self._bounded(scroll())
            except (Exception, asyncio.TimeoutError):  # noqa: BLE001
                # Best effort, and bounded: a page whose smooth scroll never
                # settles would otherwise hold a worker one line before the
                # press. The driver's own click scrolls again anyway.
                pass

        box = await self._bounded(element.bounding_box())
        if not box:
            raise FinalSubmitControlNotFound(
                (("", "the submit control has no bounding box, so it is not on screen"),)
            )
        self._reject_oversized(page, box)

        await self._verify_actionable(element, name)

        mouse = getattr(page, "mouse", None)
        if mouse is not None:
            try:
                await self._humanizer.move_to(mouse, box)
            except Exception:  # noqa: BLE001 - the travel is realism, not the click
                pass

        # The last thing before the click, and after every check that could
        # still have refused. A crash between these two lines leaves the
        # attempt recorded, which is what stops a replay pressing again.
        permit.claim()
        try:
            await element.click(timeout=self._click_timeout_ms)
        except Exception as error:  # noqa: BLE001
            # After the press was made, so it may have landed. Never a
            # second attempt.
            raise FinalSubmitControlNotActionable(
                name, f"the click did not complete ({error})", pressed=True
            ) from error

    async def _verify_actionable(self, element: Any, name: str) -> None:
        """Run the driver's actionability and hit-target checks, unclicked."""
        try:
            await element.click(trial=True, timeout=self._click_timeout_ms)
        except Exception as error:  # noqa: BLE001
            raise FinalSubmitControlNotActionable(name, str(error)) from error

    async def _bounded(self, awaitable: Awaitable[Any]) -> Any:
        """One driver call, under the per-frame deadline."""
        return await asyncio.wait_for(awaitable, self._frame_timeout_s)

    def _reject_oversized(self, page: Any, box: Mapping[str, float]) -> None:
        """Refuse to press the centre of something page-sized.

        The candidate rules already reject a control that is not a real
        button, but this is the last check before a pointer press lands: the
        centre of a full-page container could be any link underneath it, and
        this particular press is the one that sends an application.
        """
        size = getattr(page, "viewport_size", None)
        if not isinstance(size, Mapping) or not size.get("width") or not size.get("height"):
            return
        area = float(box.get("width", 0.0)) * float(box.get("height", 0.0))
        if area <= self._min_container_area:
            return
        fraction = area / max(1.0, float(size["width"]) * float(size["height"]))
        if fraction <= self._max_area_fraction:
            return
        raise FinalSubmitControlNotFound(
            (
                (
                    "",
                    f"the matched submit control covers {fraction:.0%} of the "
                    "viewport, which is a page container rather than a button",
                ),
            )
        )

    async def _await_confirmation(
        self,
        page: Any,
        baselines: Sequence[tuple[Any, PageState]],
        authorization: SubmitAuthorization,
    ) -> SubmitOutcome:
        """Wait for the page to say what became of the application.

        Polls each target against *its own* pre-click reading until one of
        them is decided, or until the deadline. A deadline reached is not a
        submission and is not another click: an application that may or may
        not have been filed is recorded as unconfirmed, with a screenshot,
        for a human to check.
        """
        deadline = self._clock() + self._confirm_timeout_ms / 1000
        poll_s = self._poll_interval_ms / 1000

        while True:
            for target, before in baselines:
                try:
                    after = PageState.from_report(
                        _mapping(
                            await self._ask(
                                target,
                                SUBMIT_STATE_SCRIPT,
                                budget=deadline - self._clock(),
                            )
                        )
                    )
                except Exception:  # noqa: BLE001 - a timeout arrives here too
                    # A target that stops answering is silence, and silence
                    # is not a submission.
                    continue
                verdict = submission_verdict(before, after)
                if verdict.signal:
                    return SubmitOutcome(
                        submitted=True,
                        reason=verdict.signal,
                        screenshot_path=await self._capture(page, authorization, "submitted"),
                    )
                if verdict.refusal:
                    return SubmitOutcome(
                        submitted=False,
                        reason=(
                            f"the final submit control was clicked once and "
                            f"{verdict.refusal}. It is not clicked again; check "
                            "the screenshot and the application by hand."
                        ),
                        screenshot_path=await self._capture(page, authorization, "refused"),
                    )
            if self._clock() >= deadline:
                return SubmitOutcome(
                    submitted=False,
                    reason=(
                        "the final submit control was clicked once, and nothing "
                        "that would confirm a submission appeared within "
                        f"{self._confirm_timeout_ms}ms: no confirmation the page "
                        "was not already showing, and no navigation to a "
                        "destination only a submission arrives at. It is not "
                        "clicked again; check the screenshot and the application "
                        "by hand."
                    ),
                    screenshot_path=await self._capture(page, authorization, "unconfirmed"),
                )
            await self._sleep(poll_s)

    async def _capture(
        self, page: Any, authorization: SubmitAuthorization, name: str
    ) -> str | None:
        """Photograph this outcome, named after the application it is of.

        An image called `unconfirmed.png` is an image of somebody's
        application, and which somebody is the only thing an operator needs
        from it. Naming it after the thread matches every other artifact
        this run writes.
        """
        if self._screenshots is None:
            return None
        try:
            return await self._screenshots.capture(
                page, f"{authorization.thread_id}-{name}"
            )
        except Exception:  # noqa: BLE001 - a diagnostic never changes an outcome
            return None


def _confirmation_targets(page: Any, frame: Any) -> list[Any]:
    """The frames whose before-and-after decide this submission.

    The frame that was clicked in, and the top document, which is where a
    board that answers a submit with a whole-page redirect puts the answer.
    Both are frames rather than one frame and one page, so that "these are
    the same target" is an identity comparison: a page and its own main
    frame evaluate against one document, and asking twice would mean
    holding two baselines for one thing.
    """
    main = getattr(page, "main_frame", None)
    seen: list[Any] = []
    for target in (frame, main if main is not None else page):
        if target is None or any(target is existing for existing in seen):
            continue
        seen.append(target)
    return seen
