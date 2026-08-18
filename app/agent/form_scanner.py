"""Deep form snapshotting, diffing, and mutation/value quiescence detection.

Playwright is never imported here. Everything operates on duck-typed page and
frame objects (`.url`, `.frames`, `.evaluate(script)`), so the whole module is
unit-testable without a browser, a display, or Playwright installed.

Three guarantees shape the design:

* **Plaintext values never leave the page by default.** The in-page script
  computes a keyed digest of each value with a per-scanner random HMAC key
  and returns only that digest plus a `filled` flag; the raw value is sent
  over CDP only when a caller explicitly opts in via `capture_values` (which
  the application wires to `LOG_FIELD_VALUES`). Because the key is random per
  `FormScanner` instance, digests are only comparable within one instance —
  snapshot the baseline and the result with the *same* scanner.
* **Settling is observed in two phases, never assumed.** `wait_for_settle`
  first waits for a real diff against the baseline (bounded by
  `first_change_timeout_ms`) and only then requires a DOM-mutation and
  field-digest quiet period. A page that never changes returns an honest
  "nothing happened" result, but only after the first-change window has
  actually elapsed. There is no fixed sleep anywhere in this module.
* **Coverage gaps are reported, not hidden.** Frames that could not be
  scanned (cross-origin, unresolvable origin, evaluation failure) travel with
  the snapshot *and* the diff, so a consumer can tell "no required gaps" from
  "no required gaps that I could see".
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from app.agent.errors import FormSettleTimeout

DEFAULT_QUIET_MS = 600
DEFAULT_SETTLE_TIMEOUT_MS = 15_000
DEFAULT_POLL_INTERVAL_MS = 100
#: How long to wait for the *first* observable field change before concluding
#: that nothing is going to happen. Autofill extensions typically start
#: writing within a few hundred milliseconds; this leaves generous headroom.
DEFAULT_FIRST_CHANGE_TIMEOUT_MS = 3_000

#: Input types that accept free-form prose, i.e. the ones a model or canonical
#: answer could plausibly be asked to complete. Deliberately excludes
#: pickers (`date`, `file`, `color`), toggles, hidden controls, and
#: `password` — a credential is never a question for a model to answer.
TEXTUAL_INPUT_TYPES: frozenset[str] = frozenset(
    {"text", "email", "tel", "url", "search", "number", ""}
)

#: Frame URLs that carry no origin of their own and inherit their parent's.
#: Their real origin is resolved by walking the parent frame chain.
_INHERITED_ORIGIN_SCHEMES: frozenset[str] = frozenset({"about", "blob", "javascript"})

#: Option labels/values that mean "nothing chosen yet". A required select
#: still showing one of these is an unanswered question, not an answer.
_PLACEHOLDER_SELECT = re.compile(
    r"^(please\s+)?(select|choose|pick)\b|^-{1,2}$|^n/?a$|^none$", re.IGNORECASE
)

_LABEL_NOISE = re.compile(r"[\s\u00a0]+")
_LABEL_DECORATION = re.compile(r"[\*:•\-\u2013\u2014\s]+$")
_MAX_LABEL_CHARS = 120
_KEY_DIGEST_CHARS = 20
_VALUE_DIGEST_CHARS = 32
_MAX_FRAME_CHAIN_DEPTH = 16


#: Collects every visible form control reachable from a frame's document,
#: descending through *open* shadow roots (a closed root exposes a null
#: `shadowRoot`, so it is silently — and necessarily — invisible here).
#:
#: Takes `{captureValues, digestKey}`. Values are digested **in the page**
#: with an HMAC keyed by the caller's random per-scanner key, so plaintext
#: never crosses CDP unless `captureValues` is explicitly true. When
#: `crypto.subtle` is unavailable (a non-secure context), it degrades to a
#: keyed non-cryptographic hash — still salted, still value-hiding against
#: casual inspection, but not collision-resistant; change detection keeps
#: working either way.
FIELD_SCAN_SCRIPT = """
(async (options) => {
  const opts = options || {};
  const captureValues = opts.captureValues === true;
  const keyHex = String(opts.digestKey || '');
  const CONTROL_SELECTOR = 'input, textarea, select, [contenteditable="true"]';
  const MAX_ROOT_DEPTH = 8;
  const MAX_FIELDS = 500;
  const PLACEHOLDER_SELECT = /^(please\\s+)?(select|choose|pick)\\b|^-{1,2}$|^n\\/?a$|^none$/i;

  const keyBytes = new Uint8Array((keyHex.match(/../g) || []).map((pair) => parseInt(pair, 16)));
  const subtle = (globalThis.crypto && globalThis.crypto.subtle) || null;
  let cryptoKey = null;
  if (subtle && keyBytes.length) {
    try {
      cryptoKey = await subtle.importKey(
        'raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
      );
    } catch (error) {
      cryptoKey = null;
    }
  }

  const fallbackDigest = (value) => {
    let hash = 0x811c9dc5;
    const material = keyHex + '\\u0000' + value;
    for (let index = 0; index < material.length; index += 1) {
      hash ^= material.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return ('00000000' + hash.toString(16)).slice(-8).repeat(4);
  };

  const digestValue = async (value) => {
    if (!cryptoKey) {
      return fallbackDigest(value);
    }
    const signature = await subtle.sign(
      'HMAC', cryptoKey, new TextEncoder().encode(value),
    );
    return Array.from(new Uint8Array(signature))
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('')
      .slice(0, 32);
  };

  const roots = [];
  const collectRoots = (root, depth, path) => {
    roots.push({ root, depth, path });
    if (depth >= MAX_ROOT_DEPTH) {
      return;
    }
    let hosts = [];
    try {
      hosts = root.querySelectorAll('*');
    } catch (error) {
      return;
    }
    let hostIndex = 0;
    for (const host of hosts) {
      if (host.shadowRoot) {
        const tag = host.tagName.toLowerCase();
        const identity = host.getAttribute('id') || String(hostIndex);
        const childPath = path ? path + '>' + tag + '#' + identity : tag + '#' + identity;
        collectRoots(host.shadowRoot, depth + 1, childPath);
        hostIndex += 1;
      }
    }
  };
  collectRoots(document, 0, '');

  const text = (value) => (value == null ? '' : String(value)).trim();

  const isVisible = (el) => {
    if (el.type === 'hidden') {
      return false;
    }
    if (el.hidden === true) {
      return false;
    }
    const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(el);
    if (style) {
      if (style.display === 'none' || style.visibility === 'hidden'
          || style.visibility === 'collapse') {
        return false;
      }
      if (Number(style.opacity) === 0) {
        return false;
      }
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  };

  const controlType = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') {
      return 'textarea';
    }
    if (tag === 'select') {
      return el.multiple ? 'select-multiple' : 'select-one';
    }
    if (tag === 'input') {
      return text(el.getAttribute('type') || el.type || 'text').toLowerCase();
    }
    return 'contenteditable';
  };

  const controlValue = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') {
      return Array.from(el.selectedOptions || [])
        .map((option) => option.value)
        .join('\\u001f');
    }
    const type = controlType(el);
    if (type === 'checkbox' || type === 'radio') {
      return el.checked ? text(el.value) || 'on' : '';
    }
    if (el.isContentEditable) {
      return text(el.textContent);
    }
    return el.value == null ? '' : String(el.value);
  };

  const selectedLabel = (el) => {
    if (el.tagName.toLowerCase() !== 'select') {
      return '';
    }
    const options = Array.from(el.selectedOptions || []);
    return options.map((option) => text(option.textContent)).join(' ');
  };

  // Mirrors app.agent.form_scanner.is_control_filled: a select still showing
  // its placeholder option is unanswered, not answered.
  const isFilled = (el) => {
    const tag = el.tagName.toLowerCase();
    const value = text(controlValue(el));
    if (tag !== 'select') {
      return value.length > 0;
    }
    if (!value || el.selectedIndex < 0) {
      return false;
    }
    const label = text(selectedLabel(el));
    if (!label) {
      return !PLACEHOLDER_SELECT.test(value);
    }
    return !(PLACEHOLDER_SELECT.test(label) || PLACEHOLDER_SELECT.test(value));
  };

  const lookupById = (el, id) => {
    const root = el.getRootNode();
    if (root && typeof root.getElementById === 'function') {
      return root.getElementById(id);
    }
    return document.getElementById(id);
  };

  const labelText = (el) => {
    const aria = text(el.getAttribute('aria-label'));
    if (aria) {
      return aria;
    }
    const labelledBy = text(el.getAttribute('aria-labelledby'));
    if (labelledBy) {
      const joined = labelledBy
        .split(/\\s+/)
        .map((id) => lookupById(el, id))
        .filter(Boolean)
        .map((node) => text(node.textContent))
        .join(' ')
        .trim();
      if (joined) {
        return joined;
      }
    }
    const labels = el.labels ? Array.from(el.labels) : [];
    for (const label of labels) {
      const value = text(label.textContent);
      if (value) {
        return value;
      }
    }
    const wrapping = el.closest ? el.closest('label') : null;
    if (wrapping) {
      const value = text(wrapping.textContent);
      if (value) {
        return value;
      }
    }
    const placeholder = text(el.getAttribute('placeholder'));
    if (placeholder) {
      return placeholder;
    }
    return text(el.getAttribute('title'));
  };

  const formIdentity = (el) => {
    const form = el.form || (el.closest ? el.closest('form') : null);
    if (!form) {
      return '';
    }
    return text(
      form.getAttribute('id')
        || form.getAttribute('name')
        || form.getAttribute('data-automation-id')
        || form.getAttribute('action'),
    );
  };

  const controlIdentity = (el) => text(
    el.getAttribute('id')
      || el.getAttribute('data-automation-id')
      || el.getAttribute('data-qa')
      || el.getAttribute('data-testid'),
  );

  const isRequired = (el) => {
    if (el.required === true) {
      return true;
    }
    return text(el.getAttribute('aria-required')).toLowerCase() === 'true';
  };

  const records = [];
  for (const entry of roots) {
    let controls = [];
    try {
      controls = entry.root.querySelectorAll(CONTROL_SELECTOR);
    } catch (error) {
      continue;
    }
    for (const el of controls) {
      if (records.length >= MAX_FIELDS) {
        return records;
      }
      const type = controlType(el);
      if (type === 'hidden' || type === 'submit' || type === 'button'
          || type === 'reset' || type === 'image') {
        continue;
      }
      const value = controlValue(el);
      const record = {
        tag: el.tagName.toLowerCase(),
        type,
        name: text(el.getAttribute('name')),
        id: controlIdentity(el),
        label: labelText(el),
        form: formIdentity(el),
        required: isRequired(el),
        disabled: el.disabled === true
          || text(el.getAttribute('aria-disabled')).toLowerCase() === 'true',
        visible: isVisible(el),
        filled: isFilled(el),
        valueDigest: await digestValue(value),
        shadowDepth: entry.depth,
        shadowPath: entry.path,
      };
      if (captureValues) {
        record.value = value;
        record.selectedText = selectedLabel(el);
      }
      records.push(record);
    }
  }
  return records;
})
""".strip()


#: Installs (once per frame) a mutation counter covering the document and every
#: reachable open shadow root, plus capture-phase `input`/`change` listeners so
#: value writes that do not mutate the DOM still register. Returns the running
#: count; re-running it re-walks for shadow roots attached since last time.
MUTATION_PROBE_SCRIPT = """
(() => {
  const KEY = '__jobrightScanState';
  let state = window[KEY];
  if (!state) {
    state = { mutations: 0, observed: new WeakSet(), observer: null };
    window[KEY] = state;
    state.observer = new MutationObserver((records) => {
      state.mutations += records.length;
    });
    const bump = () => {
      state.mutations += 1;
    };
    document.addEventListener('input', bump, true);
    document.addEventListener('change', bump, true);
  }

  const observe = (root) => {
    if (!root || state.observed.has(root)) {
      return;
    }
    state.observed.add(root);
    state.observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      characterData: true,
    });
  };

  observe(document.documentElement || document);

  const walk = (root, depth) => {
    if (depth > 8) {
      return;
    }
    let hosts = [];
    try {
      hosts = root.querySelectorAll('*');
    } catch (error) {
      return;
    }
    for (const host of hosts) {
      if (host.shadowRoot) {
        observe(host.shadowRoot);
        walk(host.shadowRoot, depth + 1);
      }
    }
  };
  walk(document, 0);

  return state.mutations;
})
""".strip()


@dataclass(frozen=True)
class FormField:
    """One visible form control, identified by a value-independent key.

    `value` stays `None` unless the scanner was constructed with
    `capture_values=True`; `value_digest` is always populated and is what
    change attribution compares.
    """

    key: str
    frame_url: str
    form: str
    control_id: str
    name: str
    field_type: str
    label: str
    tag: str
    required: bool
    disabled: bool
    visible: bool
    filled: bool
    free_text: bool
    value_digest: str
    frame_chain: str = ""
    shadow_path: str = ""
    shadow_depth: int = 0
    value: str | None = None

    @property
    def interactable(self) -> bool:
        return self.visible and not self.disabled

    @property
    def is_required_gap(self) -> bool:
        """Required, still empty, and something a human could actually fill."""
        return self.required and not self.filled and self.interactable

    @property
    def is_free_text_gap(self) -> bool:
        """Unanswered prose control, whether or not the page marks it required."""
        return self.free_text and not self.filled and self.interactable


@dataclass(frozen=True)
class FrameSkip:
    """A frame that was deliberately or unavoidably not scanned."""

    frame_url: str
    reason: str


@dataclass(frozen=True)
class FieldChange:
    """The same stable key observed with two different value digests."""

    before: FormField
    after: FormField

    @property
    def became_filled(self) -> bool:
        return not self.before.filled and self.after.filled

    @property
    def became_empty(self) -> bool:
        return self.before.filled and not self.after.filled


@dataclass(frozen=True)
class FormDiff:
    """Attribution of what changed between two snapshots, plus open gaps.

    `still_empty_required` and `unanswered_free_text` are always taken from
    the *after* snapshot: they describe work that remains, not history.
    """

    changed: tuple[FieldChange, ...] = ()
    newly_filled: tuple[FormField, ...] = ()
    cleared: tuple[FormField, ...] = ()
    added: tuple[FormField, ...] = ()
    removed: tuple[FormField, ...] = ()
    still_empty_required: tuple[FormField, ...] = ()
    unanswered_free_text: tuple[FormField, ...] = ()
    #: Frames neither snapshot could scan. Non-empty means the gap lists
    #: above describe only the part of the page that was actually visible.
    skipped_frames: tuple[FrameSkip, ...] = ()

    @property
    def coverage_complete(self) -> bool:
        return not self.skipped_frames

    @property
    def has_changes(self) -> bool:
        """Whether any *existing* control's value moved.

        Deliberately ignores `added`/`removed`: a page that merely rerenders
        its controls (new keys, same emptiness) has not been autofilled, and
        treating that as success would let a trigger tier claim a win it
        did not earn.
        """
        return bool(self.changed or self.newly_filled)


@dataclass(frozen=True)
class FormSnapshot:
    """An immutable view of every scannable control at one point in time."""

    fields: tuple[FormField, ...] = ()
    skipped_frames: tuple[FrameSkip, ...] = ()

    def by_key(self) -> Mapping[str, FormField]:
        return {field.key: field for field in self.fields}

    @property
    def coverage_complete(self) -> bool:
        """Whether every frame of the page was actually scanned."""
        return not self.skipped_frames

    def required_gaps(self) -> tuple[FormField, ...]:
        return tuple(field for field in self.fields if field.is_required_gap)

    def unanswered_free_text(self) -> tuple[FormField, ...]:
        return tuple(field for field in self.fields if field.is_free_text_gap)

    def fingerprint(self) -> tuple[tuple[str, str, bool, bool, bool, bool], ...]:
        """Order-independent identity used to decide whether values settled."""
        return tuple(
            sorted(
                (
                    field.key,
                    field.value_digest,
                    field.filled,
                    field.required,
                    field.visible,
                    field.disabled,
                )
                for field in self.fields
            )
        )

    def diff(self, after: "FormSnapshot") -> FormDiff:
        before_by_key = self.by_key()
        after_by_key = after.by_key()

        changed: list[FieldChange] = []
        newly_filled: list[FormField] = []
        cleared: list[FormField] = []
        added: list[FormField] = []

        for field in after.fields:
            previous = before_by_key.get(field.key)
            if previous is None:
                added.append(field)
                continue
            if previous.value_digest == field.value_digest:
                continue
            change = FieldChange(before=previous, after=field)
            changed.append(change)
            if change.became_filled:
                newly_filled.append(field)
            elif change.became_empty:
                cleared.append(field)

        removed = tuple(field for field in self.fields if field.key not in after_by_key)

        skipped: list[FrameSkip] = []
        seen_skips: set[tuple[str, str]] = set()
        for skip in (*self.skipped_frames, *after.skipped_frames):
            marker = (skip.frame_url, skip.reason)
            if marker in seen_skips:
                continue
            seen_skips.add(marker)
            skipped.append(skip)

        return FormDiff(
            changed=tuple(changed),
            newly_filled=tuple(newly_filled),
            cleared=tuple(cleared),
            added=tuple(added),
            removed=removed,
            still_empty_required=after.required_gaps(),
            unanswered_free_text=after.unanswered_free_text(),
            skipped_frames=tuple(skipped),
        )


@dataclass(frozen=True)
class SettleResult:
    """Outcome of an observed (never assumed) quiescence wait.

    `settled` is `False` only on the result carried by a `FormSettleTimeout`;
    `observed_change` distinguishes "the page autofilled and then went quiet"
    from "the first-change window elapsed with nothing happening at all".
    """

    snapshot: FormSnapshot
    diff: FormDiff
    waited_ms: float
    polls: int
    mutations: int
    settled: bool = True
    observed_change: bool = False


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


def _digest(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def compute_value_digest(key: bytes, value: str) -> str:
    """Keyed digest of a field value.

    Keyed (HMAC), not a bare hash: an unsalted digest of a short, guessable
    value — a name, a phone number, a yes/no answer — is trivially reversible
    by dictionary attack, which would make "we never store values" hollow.
    The key is random per `FormScanner`, so digests are comparable only
    within one scanner instance. Mirrored by `FIELD_SCAN_SCRIPT`, which
    computes the same HMAC inside the page.
    """
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[
        :_VALUE_DIGEST_CHARS
    ]


def is_control_filled(
    tag: str, field_type: str, value: str, selected_text: str = ""
) -> bool:
    """Whether a control actually holds an answer.

    A `select` still showing its placeholder option ("Select one", "-",
    "N/A") is unanswered even though it has a non-empty value, so treating
    it as filled would hide a required question from the gap list. Mirrored
    by `FIELD_SCAN_SCRIPT`'s `isFilled`, which applies the same rule in the
    page so the flag is correct even when values never leave it.
    """
    trimmed = value.strip()
    if tag.lower() != "select" and not field_type.lower().startswith("select"):
        return bool(trimmed)
    if not trimmed:
        return False
    label = selected_text.strip()
    if not label:
        return _PLACEHOLDER_SELECT.match(trimmed) is None
    return (
        _PLACEHOLDER_SELECT.match(label) is None
        and _PLACEHOLDER_SELECT.match(trimmed) is None
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_bool(value: Any) -> bool:
    return bool(value)


def _normalize_label(label: str) -> str:
    """Collapse whitespace, drop required markers, and case-fold.

    Pages routinely repaint a label as "Email *", "Email:", or "EMAIL"
    without the underlying control changing at all; folding those keeps the
    stable key stable.
    """
    collapsed = _LABEL_NOISE.sub(" ", label).strip()
    collapsed = _LABEL_DECORATION.sub("", collapsed)
    return collapsed.casefold()[:_MAX_LABEL_CHARS]


def _normalize_frame_url(url: str) -> str:
    """Drop the fragment so SPA hash routing does not churn field keys."""
    parts = urlsplit(url)
    return parts._replace(fragment="").geturl()


def _origin(url: str) -> tuple[str, str] | None:
    """Return a comparable origin, or `None` when the URL inherits one."""
    parts = urlsplit(url)
    if not parts.scheme or parts.scheme in _INHERITED_ORIGIN_SCHEMES:
        return None
    return parts.scheme, parts.netloc


def frame_chain(frame: Any) -> str:
    """Position of a frame in its page's frame tree, e.g. `"0/2:apply"`.

    Two iframes with the *same* `src` (a very common ATS pattern: repeated
    widgets, or a form re-embedded per section) are otherwise
    indistinguishable, so their controls would collide on one identity. The
    chain walks up `parent_frame`, recording each frame's index among its
    siblings plus its `name` when it has one, which stays stable across
    snapshots of the same page.
    """
    parts: list[str] = []
    current = frame
    for _ in range(_MAX_FRAME_CHAIN_DEPTH):
        parent = getattr(current, "parent_frame", None)
        if parent is None:
            break
        siblings = list(getattr(parent, "child_frames", []) or [])
        index = next(
            (position for position, sibling in enumerate(siblings) if sibling is current),
            -1,
        )
        name = _as_text(getattr(current, "name", ""))
        parts.append(f"{index}:{name}" if name else str(index))
        current = parent
    parts.reverse()
    return "/".join(parts)


def resolve_frame_origin(frame: Any) -> tuple[str, str] | None:
    """Resolve a frame's effective origin, walking inherited ones upward.

    `about:blank`, `about:srcdoc`, and `blob:` frames carry no origin of
    their own: they inherit their parent's. Returns `None` when the origin
    cannot be established — callers must treat that as "not scannable"
    rather than "same origin", since an `about:blank` iframe nested inside a
    third-party frame belongs to that third party, not to us.
    """
    current = frame
    for _ in range(_MAX_FRAME_CHAIN_DEPTH):
        if current is None:
            return None
        origin = _origin(_as_text(getattr(current, "url", "")))
        if origin is not None:
            return origin
        current = getattr(current, "parent_frame", None)
    return None


def same_origin_frames(page: Any) -> tuple[list[Any], tuple[FrameSkip, ...]]:
    """Split a page's frames into the scannable ones and the skipped ones.

    The main frame is always scanned. Every other frame must resolve —
    through its parent chain, for inherited origins — to exactly the main
    frame's origin; anything cross-origin or unresolvable is returned as a
    `FrameSkip` diagnostic instead of being probed. A page object with no
    `frames` collection is treated as its own single frame.
    """
    frames = getattr(page, "frames", None)
    if not frames:
        return [page], ()

    frames = list(frames)
    main_frame = getattr(page, "main_frame", None) or frames[0]
    main_origin = resolve_frame_origin(main_frame)

    accessible: list[Any] = [main_frame]
    skipped: list[FrameSkip] = []
    for frame in frames:
        if frame is main_frame:
            continue
        frame_url = _as_text(getattr(frame, "url", ""))
        frame_origin = resolve_frame_origin(frame)
        if frame_origin is None:
            skipped.append(
                FrameSkip(
                    frame_url,
                    "frame origin could not be resolved through its parent frame "
                    "chain, so it is not treated as same-origin",
                )
            )
        elif main_origin is None:
            skipped.append(
                FrameSkip(
                    frame_url,
                    "main frame origin could not be resolved, so no child frame "
                    "can be confirmed same-origin",
                )
            )
        elif frame_origin != main_origin:
            skipped.append(
                FrameSkip(
                    frame_url,
                    "cross-origin frame is not scannable from the main frame's origin "
                    f"({main_origin[0]}://{main_origin[1]}); its effective origin is "
                    f"{frame_origin[0]}://{frame_origin[1]}",
                )
            )
        else:
            accessible.append(frame)
    return accessible, tuple(skipped)


def _is_free_text(tag: str, field_type: str) -> bool:
    if tag == "textarea" or field_type == "textarea":
        return True
    if tag == "input":
        return field_type in TEXTUAL_INPUT_TYPES
    return field_type == "contenteditable"


class FormScanner:
    """Snapshots, diffs, and settle-detects form state across frames.

    `clock`/`sleep` are injectable so quiescence logic is deterministic under
    test; production uses `time.monotonic` and `asyncio.sleep`.
    """

    def __init__(
        self,
        *,
        capture_values: bool = False,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        digest_key: bytes | None = None,
    ) -> None:
        self._capture_values = capture_values
        self._clock = clock
        self._sleep = sleep
        self._poll_interval_ms = max(1, poll_interval_ms)
        # Random per instance: digests are a change-detection device, not a
        # stable identifier, and a fresh key stops values being correlated
        # across runs or recovered by dictionary attack from logs.
        self._digest_key = digest_key if digest_key is not None else secrets.token_bytes(32)

    async def snapshot(self, page: Any) -> FormSnapshot:
        """Scan every same-origin frame, including open shadow roots."""
        fields: list[FormField] = []
        frames, cross_origin = same_origin_frames(page)
        skipped: list[FrameSkip] = list(cross_origin)
        # Shared across frames so two frames that somehow present the same
        # identity still yield distinct keys within one snapshot.
        seen_identities: dict[tuple[str, ...], int] = {}
        options = {
            "captureValues": self._capture_values,
            "digestKey": self._digest_key.hex(),
        }

        for frame in frames:
            frame_url = _as_text(getattr(frame, "url", ""))
            try:
                records = await frame.evaluate(FIELD_SCAN_SCRIPT, options)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not
                # abort the whole scan; navigation/detachment is routine here.
                skipped.append(FrameSkip(frame_url, f"evaluation failed: {exc}"))
                continue
            fields.extend(
                self._parse_records(frame_url, frame_chain(frame), records, seen_identities)
            )

        return FormSnapshot(fields=tuple(fields), skipped_frames=tuple(skipped))

    async def wait_for_settle(
        self,
        page: Any,
        previous: FormSnapshot,
        quiet_ms: int = DEFAULT_QUIET_MS,
        timeout_ms: int = DEFAULT_SETTLE_TIMEOUT_MS,
        first_change_timeout_ms: int = DEFAULT_FIRST_CHANGE_TIMEOUT_MS,
    ) -> SettleResult:
        """Wait, in two phases, for the page to finish reacting.

        Phase one waits for a real diff against `previous`. Until something
        actually changes, a quiet page proves nothing — an extension that has
        not started writing yet looks exactly like one that never will — so a
        "nothing happened" result is only returned once
        `first_change_timeout_ms` has genuinely elapsed (and the page is
        quiet, so the answer is not read mid-render).

        Phase two, entered as soon as any change is seen, requires the DOM
        mutation counter *and* every field digest to hold still for
        `quiet_ms`, so a result is never read while values are still landing.

        `timeout_ms` bounds both phases together; exceeding it raises
        `FormSettleTimeout` carrying the last unsettled `SettleResult`.
        """
        started = self._clock()
        deadline_s = timeout_ms / 1000
        quiet_s = quiet_ms / 1000
        first_change_s = first_change_timeout_ms / 1000
        poll_s = self._poll_interval_ms / 1000

        last_observation: tuple[int, Any] | None = None
        stable_since = started
        observed_change = False
        polls = 0

        while True:
            mutations = await self._read_mutations(page)
            snapshot = await self.snapshot(page)
            diff = previous.diff(snapshot)
            polls += 1
            now = self._clock()
            observation = (mutations, snapshot.fingerprint())

            if observation != last_observation:
                last_observation = observation
                stable_since = now

            observed_change = observed_change or diff.has_changes
            quiet_enough = now - stable_since >= quiet_s
            window_elapsed = now - started >= first_change_s

            if quiet_enough and (observed_change or window_elapsed):
                return SettleResult(
                    snapshot=snapshot,
                    diff=diff,
                    waited_ms=(now - started) * 1000,
                    polls=polls,
                    mutations=mutations,
                    settled=True,
                    observed_change=observed_change,
                )

            if now - started >= deadline_s:
                raise FormSettleTimeout(
                    quiet_ms=quiet_ms,
                    timeout_ms=timeout_ms,
                    result=SettleResult(
                        snapshot=snapshot,
                        diff=diff,
                        waited_ms=(now - started) * 1000,
                        polls=polls,
                        mutations=mutations,
                        settled=False,
                        observed_change=observed_change,
                    ),
                )

            await self._sleep(poll_s)

    async def _read_mutations(self, page: Any) -> int:
        """Sum the mutation counters of every same-origin frame.

        A frame that cannot be probed contributes nothing rather than
        aborting the wait; its field values are still compared each poll.
        """
        total = 0
        frames, _ = same_origin_frames(page)
        for frame in frames:
            try:
                value = await frame.evaluate(MUTATION_PROBE_SCRIPT)
            except Exception:  # noqa: BLE001 - probing is best effort
                continue
            if isinstance(value, (int, float)):
                total += int(value)
        return total

    def _parse_records(
        self,
        frame_url: str,
        chain: str,
        records: Any,
        seen_identities: dict[tuple[str, ...], int],
    ) -> list[FormField]:
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            return []

        normalized_url = _normalize_frame_url(frame_url)
        fields: list[FormField] = []

        for record in records:
            if not isinstance(record, Mapping):
                continue
            fields.append(
                self._build_field(
                    frame_url, chain, normalized_url, record, seen_identities
                )
            )
        return fields

    def _build_field(
        self,
        frame_url: str,
        chain: str,
        normalized_url: str,
        record: Mapping[str, Any],
        seen_identities: dict[tuple[str, ...], int],
    ) -> FormField:
        tag = _as_text(record.get("tag")).lower()
        field_type = _as_text(record.get("type")).lower()
        name = _as_text(record.get("name"))
        control_id = _as_text(record.get("id"))
        label = _as_text(record.get("label"))
        form = _as_text(record.get("form"))
        shadow_path = _as_text(record.get("shadowPath"))
        shadow_depth = int(record.get("shadowDepth") or 0)
        value = _as_text(record.get("value"))
        has_value = "value" in record

        identity = (
            chain,
            normalized_url,
            shadow_path,
            str(shadow_depth),
            form,
            control_id,
            name,
            field_type,
            _normalize_label(label),
        )
        ordinal = seen_identities.get(identity, 0)
        seen_identities[identity] = ordinal + 1

        digest = _as_text(record.get("valueDigest"))
        if not digest:
            digest = compute_value_digest(self._digest_key, value)

        if "filled" in record:
            filled = _as_bool(record.get("filled"))
        else:
            filled = is_control_filled(
                tag, field_type, value, _as_text(record.get("selectedText"))
            )

        return FormField(
            key=self._field_key(identity, ordinal),
            frame_url=frame_url,
            form=form,
            control_id=control_id,
            name=name,
            field_type=field_type,
            label=label,
            tag=tag,
            required=_as_bool(record.get("required")),
            disabled=_as_bool(record.get("disabled")),
            visible=_as_bool(record.get("visible", True)),
            filled=filled,
            free_text=_is_free_text(tag, field_type),
            value_digest=digest,
            frame_chain=chain,
            shadow_path=shadow_path,
            shadow_depth=shadow_depth,
            value=value if (self._capture_values and has_value) else None,
        )

    @staticmethod
    def _field_key(identity: Sequence[str], ordinal: int) -> str:
        """Hash the identity components into a short, stable, opaque key.

        The ordinal only participates for genuinely indistinguishable
        controls (all of name/id/label/form empty and identical), so the
        first such control keeps its key when a second one appears.
        """
        canonical = "|".join(identity)
        if ordinal:
            canonical = f"{canonical}|#{ordinal}"
        return _digest(canonical, _KEY_DIGEST_CHARS)
