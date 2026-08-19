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
prompt genuinely is: a *visible* reCAPTCHA/hCaptcha/Turnstile/Arkose widget,
and a *visible* password field.

**The submitter reports a submission only when the page says so.** It
refuses without an approval, requires exactly one visible control whose
accessible name is on an explicit allowlist, never clicks Next or Continue
or Save, and after clicking waits for one of three concrete signals —
navigation, a confirmation region, or the form it clicked in disappearing.
No signal means `submitted=False` and no second click, because the only
thing worse than an unconfirmed submission is two of them.

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
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

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
    TEXTUAL_INPUT_TYPES,
    FormField,
    FormScanner,
    frame_chain,
    same_origin_frames,
)
from app.agent.graph import SubmitAuthorization, SubmitOutcome
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


#: Counts the controls in this frame that match a field's identity. Never
#: given the value: a frame that turns out to hold two matches, or none, has
#: no business receiving somebody's answer.
WRITE_COUNT_SCRIPT = (
    """
((want) => {
"""
    + FIELD_IDENTITY_JS
    + WRITE_CANDIDATES_JS
    + """
  return { count: findCandidates(want || {}).length };
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

  const identity = {
    shadowPath: entry.path,
    shadowDepth: entry.depth,
    form: formIdentity(el),
    id: controlIdentity(el),
    name: text(el.getAttribute('name')),
    type: controlType(el),
    label: labelText(el),
  };

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


#: Reports whether this frame is presenting a human-verification challenge
#: or asking for credentials, as the selector that matched.
#:
#: Structural only, and visible only. An invisible reCAPTCHA v3 badge sits
#: on an enormous number of ordinary pages and challenges nobody; a hidden
#: `g-recaptcha-response` textarea is present whether or not a challenge was
#: ever shown. Neither is a reason to abandon somebody's application, so
#: neither is a marker here.
GUARD_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + """
  const CAPTCHA_SELECTORS = [
    'iframe[src*="recaptcha/"]',
    'iframe[src*="hcaptcha.com"]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="arkoselabs.com"]',
    'iframe[src*="funcaptcha"]',
    'iframe[src*="geo.captcha-delivery.com"]',
    'div.g-recaptcha',
    'div.h-captcha',
    'div.cf-turnstile',
    'div[data-sitekey]',
    '#px-captcha',
    '#captcha-challenge',
  ];

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
    captcha = captcha || firstVisible(entry.root, CAPTCHA_SELECTORS);
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


#: Records what the page looked like before the click and marks the form the
#: submit control belongs to, so "that form disappeared" can name *which*
#: form rather than "some form is missing".
SUBMIT_TARGET_SCRIPT = (
    """
(() => {
"""
    + PAGE_TRAVERSAL_JS
    + SUBMIT_CANDIDATES_JS
    + """
  const { accepted } = collectSubmitCandidates();
  if (accepted.length !== 1) {
    return { ok: false, url: String(location.href), marked: false };
  }
  const el = accepted[0].el;
  const form = el.form || (el.closest ? el.closest('form') : null);
  if (form) {
    form.setAttribute('data-jobright-submit-target', '1');
  }
  return { ok: true, url: String(location.href), marked: Boolean(form) };
})
"""
).strip()


#: Looks for the three concrete things a submitted application does to a
#: page. Each is reported as the detail that was observed, or an empty
#: string; nothing is inferred from the passage of time.
SUBMIT_SIGNAL_SCRIPT = (
    """
((before) => {
"""
    + PAGE_TRAVERSAL_JS
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

  const stripFragment = (href) => String(href || '').split('#')[0];

  const previous = (before && before.url) || '';
  const navigated = stripFragment(location.href) !== stripFragment(previous)
    ? String(location.href)
    : '';

  let confirmed = '';
  let formGone = '';
  const roots = collectRoots(document, 0, '', []);

  for (const entry of roots) {
    if (confirmed) {
      break;
    }
    let found = [];
    try {
      found = entry.root.querySelectorAll(CONFIRMATION_SELECTOR);
    } catch (error) {
      continue;
    }
    for (const el of found) {
      const body = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!body || !isVisible(el)) {
        continue;
      }
      if (NOT_A_CONFIRMATION.test(body)) {
        continue;
      }
      if (CONFIRMATION_TEXT.test(body)) {
        confirmed = body.slice(0, 160);
        break;
      }
    }
  }

  if (before && before.marked) {
    let target = null;
    for (const entry of roots) {
      try {
        target = entry.root.querySelector('form[data-jobright-submit-target]');
      } catch (error) {
        target = null;
      }
      if (target) {
        break;
      }
    }
    if (!target) {
      formGone = 'the form the submit control belonged to is no longer in the page';
    } else if (!isVisible(target)) {
      formGone = 'the form the submit control belonged to is no longer visible';
    }
  }

  return { navigated, confirmed, formGone };
})
"""
).replace("__CONFIRMATION_TEXT__", json.dumps(CONFIRMATION_TEXT.pattern)).strip()


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

    def __init__(self, *, scanner: FormScanner | None = None) -> None:
        self._scanner = scanner

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
        matches: list[tuple[Any, int]] = []
        total = 0
        for frame in frames:
            found = int(
                _mapping(await _evaluate(frame, WRITE_COUNT_SCRIPT, descriptor)).get(
                    "count", 0
                )
            )
            total += found
            if found:
                matches.append((frame, found))
        if total != 1:
            raise FieldNotUniquelyResolved(
                field.key,
                total,
                f"across {len(frames)} same-origin frame(s)",
            )

        frame = matches[0][0]
        result = _mapping(
            await _evaluate(
                frame, WRITE_SCRIPT, {"field": descriptor, "value": value}
            )
        )
        if not result.get("ok"):
            reported = int(result.get("count", 0))
            reason = _text(result.get("reason")) or "the page refused the write"
            if reported != 1:
                raise FieldNotUniquelyResolved(field.key, reported, reason)
            raise FieldWriteNotVerified(field.key, reason)

        self._require_provenance(field, frame, _mapping(result.get("identity")))
        if not result.get("matched"):
            raise FieldWriteNotVerified(field.key)
        return True

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


class PlaywrightSubmitter:
    """Performs the last click, and only says it worked when the page does."""

    def __init__(
        self,
        *,
        screenshots: Screenshotter | None = None,
        confirm_timeout_ms: int = DEFAULT_CONFIRM_TIMEOUT_MS,
        poll_interval_ms: int = DEFAULT_CONFIRM_POLL_MS,
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
        self, page: Any, authorization: SubmitAuthorization
    ) -> SubmitOutcome:
        if not authorization.approved:
            raise SubmitNotAuthorized(
                authorization.application_id, authorization.refusal()
            )

        frame = await self._sole_submit_frame(page)
        before = _mapping(await _evaluate(frame, SUBMIT_TARGET_SCRIPT))
        if not before.get("ok"):
            # The page changed between counting and marking. Refusing is the
            # only safe answer: whatever is there now was never counted.
            raise FinalSubmitControlNotFound(
                (("", "the submit control disappeared before it could be clicked"),)
            )

        await self._click_the_only_submit(page, frame)
        return await self._await_confirmation(page, frame, before)

    async def _sole_submit_frame(self, page: Any) -> Any:
        """The one same-origin frame holding the one final-submit control."""
        frames, _skipped = same_origin_frames(page)
        holders: list[Any] = []
        accepted: list[str] = []
        rejected: list[tuple[str, str]] = []
        for frame in frames:
            report = _mapping(await _evaluate(frame, SUBMIT_COUNT_SCRIPT))
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
        return holders[0]

    async def _click_the_only_submit(self, page: Any, frame: Any) -> None:
        handle = await frame.evaluate_handle(SUBMIT_RESOLVE_SCRIPT)
        as_element = getattr(handle, "as_element", None)
        element = as_element() if as_element is not None else handle
        if element is None:
            await _dispose_quietly(handle)
            raise FinalSubmitControlNotFound(
                (("", "the counted submit control could not be resolved again"),)
            )
        try:
            await self._press(page, element)
        finally:
            await _dispose_quietly(element)

    async def _press(self, page: Any, element: Any) -> None:
        scroll = getattr(element, "scroll_into_view_if_needed", None)
        if scroll is not None:
            try:
                await scroll()
            except Exception:  # noqa: BLE001 - scrolling is best effort
                pass

        box = await element.bounding_box()
        if not box:
            raise FinalSubmitControlNotFound(
                (("", "the submit control has no bounding box, so it is not on screen"),)
            )
        self._reject_oversized(page, box)

        mouse = getattr(page, "mouse", None)
        if mouse is None:  # pragma: no cover - every real page has one
            raise FinalSubmitControlNotFound(
                (("", "the page exposes no mouse, so nothing can be clicked"),)
            )
        await self._humanizer.move_and_click(mouse, box)

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
        self, page: Any, frame: Any, before: Mapping[str, Any]
    ) -> SubmitOutcome:
        """Wait for the page to say the application went through.

        Polls until one of three concrete things is true, or until the
        deadline. A deadline reached is *not* a submission and is *not*
        another click: an application that may or may not have been filed is
        recorded as unconfirmed, with a screenshot, for a human to check.
        """
        recorded = {"url": _text(before.get("url")), "marked": bool(before.get("marked"))}
        deadline = self._clock() + self._confirm_timeout_ms / 1000
        poll_s = self._poll_interval_ms / 1000

        while True:
            for target in _distinct((frame, page)):
                signal = _describe_signal(
                    _mapping(await _evaluate(target, SUBMIT_SIGNAL_SCRIPT, recorded))
                )
                if signal:
                    return SubmitOutcome(
                        submitted=True,
                        reason=signal,
                        screenshot_path=await self._capture(page, "submitted"),
                    )
            if self._clock() >= deadline:
                return SubmitOutcome(
                    submitted=False,
                    reason=(
                        "the final submit control was clicked once, and none of the "
                        "three signals that would confirm a submission appeared "
                        f"within {self._confirm_timeout_ms}ms: no navigation, no "
                        "confirmation region, and the form is still on the page. "
                        "It is not clicked again; check the screenshot and the "
                        "application by hand."
                    ),
                    screenshot_path=await self._capture(page, "unconfirmed"),
                )
            await self._sleep(poll_s)

    async def _capture(self, page: Any, name: str) -> str | None:
        if self._screenshots is None:
            return None
        try:
            return await self._screenshots.capture(page, name)
        except Exception:  # noqa: BLE001 - a diagnostic never changes an outcome
            return None


def _distinct(targets: Sequence[Any]) -> list[Any]:
    """The given objects, without asking the same one twice.

    A single-frame page's main frame *is* the page for evaluation purposes
    in some drivers and a separate object in others; either way the signal
    script should run once per distinct target.
    """
    seen: list[Any] = []
    for target in targets:
        if target is None or any(target is existing for existing in seen):
            continue
        seen.append(target)
    return seen


def _describe_signal(report: Mapping[str, Any]) -> str:
    """Which concrete success signal the page showed, if any."""
    confirmed = _text(report.get("confirmed"))
    if confirmed:
        return f"the page showed a confirmation: {confirmed!r}"
    navigated = _text(report.get("navigated"))
    if navigated:
        return f"the page navigated to {navigated} after the submit click"
    gone = _text(report.get("formGone"))
    if gone:
        return gone
    return ""
