"""Deep form snapshotting, diffing, and mutation/value quiescence detection.

Playwright is never imported here. Everything operates on duck-typed page and
frame objects (`.url`, `.frames`, `.evaluate(script)`), so the whole module is
unit-testable without a browser, a display, or Playwright installed.

Two guarantees shape the design:

* **Values stay out of the models by default.** A field carries a digest of
  its value, never the value itself, unless a caller explicitly opts in via
  `capture_values` (which the application wires to `LOG_FIELD_VALUES`).
  Change attribution only ever needs the digest.
* **Settling is observed, never assumed.** `wait_for_settle` polls both a
  DOM mutation counter and the field digests and only returns once *both*
  have stayed identical for a full quiet period. There is no fixed sleep
  anywhere in this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from app.agent.errors import FormSettleTimeout

DEFAULT_QUIET_MS = 600
DEFAULT_SETTLE_TIMEOUT_MS = 15_000
DEFAULT_POLL_INTERVAL_MS = 100

#: Input types that accept free-form prose, i.e. the ones a model or canonical
#: answer could plausibly be asked to complete. Deliberately excludes
#: pickers (`date`, `file`, `color`), toggles, and hidden controls.
TEXTUAL_INPUT_TYPES: frozenset[str] = frozenset(
    {"text", "email", "tel", "url", "search", "number", "password", ""}
)

#: Frame URLs that inherit their parent's origin and are therefore always
#: reachable from the main frame's script context.
_INHERITED_ORIGIN_SCHEMES: frozenset[str] = frozenset({"about", "blob", "javascript"})

_LABEL_NOISE = re.compile(r"[\s\u00a0]+")
_LABEL_DECORATION = re.compile(r"[\*:•\-\u2013\u2014\s]+$")
_MAX_LABEL_CHARS = 120
_KEY_DIGEST_CHARS = 20
_VALUE_DIGEST_CHARS = 32


#: Collects every visible form control reachable from a frame's document,
#: descending through *open* shadow roots (a closed root exposes a null
#: `shadowRoot`, so it is silently — and necessarily — invisible here).
#: Returns plain JSON records; all identity/keying decisions happen in Python.
FIELD_SCAN_SCRIPT = """
(() => {
  const CONTROL_SELECTOR = 'input, textarea, select, [contenteditable="true"]';
  const MAX_ROOT_DEPTH = 8;
  const MAX_FIELDS = 500;

  const roots = [];
  const collectRoots = (root, depth) => {
    roots.push({ root, depth });
    if (depth >= MAX_ROOT_DEPTH) {
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
        collectRoots(host.shadowRoot, depth + 1);
      }
    }
  };
  collectRoots(document, 0);

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
      records.push({
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
        value: controlValue(el),
        shadowDepth: entry.depth,
      });
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

        return FormDiff(
            changed=tuple(changed),
            newly_filled=tuple(newly_filled),
            cleared=tuple(cleared),
            added=tuple(added),
            removed=removed,
            still_empty_required=after.required_gaps(),
            unanswered_free_text=after.unanswered_free_text(),
        )


@dataclass(frozen=True)
class SettleResult:
    """Outcome of an observed (never assumed) quiescence wait."""

    snapshot: FormSnapshot
    diff: FormDiff
    waited_ms: float
    polls: int
    mutations: int


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


def _digest(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


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


def same_origin_frames(page: Any) -> tuple[list[Any], tuple[FrameSkip, ...]]:
    """Split a page's frames into the scannable ones and the skipped ones.

    A frame is scannable when it shares the main frame's origin or inherits
    it (`about:blank`, `about:srcdoc`, blob frames). Cross-origin frames are
    returned as `FrameSkip` diagnostics instead of being probed, matching the
    approved spec's same-origin-only traversal. A page object with no
    `frames` collection is treated as its own single frame.
    """
    frames = getattr(page, "frames", None)
    if not frames:
        return [page], ()

    main_origin = _origin(_as_text(getattr(page, "url", "")))
    accessible: list[Any] = []
    skipped: list[FrameSkip] = []
    for frame in frames:
        frame_url = _as_text(getattr(frame, "url", ""))
        frame_origin = _origin(frame_url)
        if frame_origin is None or main_origin is None or frame_origin == main_origin:
            accessible.append(frame)
            continue
        skipped.append(
            FrameSkip(
                frame_url,
                "cross-origin frame is not scannable from the main frame's origin "
                f"({main_origin[0]}://{main_origin[1]})",
            )
        )
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
    ) -> None:
        self._capture_values = capture_values
        self._clock = clock
        self._sleep = sleep
        self._poll_interval_ms = max(1, poll_interval_ms)

    async def snapshot(self, page: Any) -> FormSnapshot:
        """Scan every same-origin frame, including open shadow roots."""
        fields: list[FormField] = []
        frames, cross_origin = same_origin_frames(page)
        skipped: list[FrameSkip] = list(cross_origin)

        for frame in frames:
            frame_url = _as_text(getattr(frame, "url", ""))
            try:
                records = await frame.evaluate(FIELD_SCAN_SCRIPT)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not
                # abort the whole scan; navigation/detachment is routine here.
                skipped.append(FrameSkip(frame_url, f"evaluation failed: {exc}"))
                continue
            fields.extend(self._parse_records(frame_url, records))

        return FormSnapshot(fields=tuple(fields), skipped_frames=tuple(skipped))

    async def wait_for_settle(
        self,
        page: Any,
        previous: FormSnapshot,
        quiet_ms: int = DEFAULT_QUIET_MS,
        timeout_ms: int = DEFAULT_SETTLE_TIMEOUT_MS,
    ) -> SettleResult:
        """Wait until DOM mutations *and* field values hold still.

        Polls a per-frame mutation counter alongside a full snapshot and only
        returns once the combined observation has been identical for
        `quiet_ms`. Raises `FormSettleTimeout` (carrying the last snapshot and
        diff) rather than returning a value that was still in motion.
        """
        started = self._clock()
        deadline_s = timeout_ms / 1000
        quiet_s = quiet_ms / 1000
        poll_s = self._poll_interval_ms / 1000

        last_observation: tuple[int, Any] | None = None
        stable_since = started
        polls = 0
        snapshot = FormSnapshot()
        mutations = 0

        while True:
            mutations = await self._read_mutations(page)
            snapshot = await self.snapshot(page)
            polls += 1
            now = self._clock()
            observation = (mutations, snapshot.fingerprint())

            if observation != last_observation:
                last_observation = observation
                stable_since = now
            elif now - stable_since >= quiet_s:
                return SettleResult(
                    snapshot=snapshot,
                    diff=previous.diff(snapshot),
                    waited_ms=(now - started) * 1000,
                    polls=polls,
                    mutations=mutations,
                )

            if now - started >= deadline_s:
                raise FormSettleTimeout(
                    quiet_ms=quiet_ms,
                    timeout_ms=timeout_ms,
                    snapshot=snapshot,
                    diff=previous.diff(snapshot),
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

    def _parse_records(self, frame_url: str, records: Any) -> list[FormField]:
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            return []

        normalized_url = _normalize_frame_url(frame_url)
        seen_identities: dict[tuple[str, ...], int] = {}
        fields: list[FormField] = []

        for record in records:
            if not isinstance(record, Mapping):
                continue
            fields.append(
                self._build_field(frame_url, normalized_url, record, seen_identities)
            )
        return fields

    def _build_field(
        self,
        frame_url: str,
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
        value = _as_text(record.get("value"))

        identity = (
            normalized_url,
            form,
            control_id,
            name,
            field_type,
            _normalize_label(label),
        )
        ordinal = seen_identities.get(identity, 0)
        seen_identities[identity] = ordinal + 1

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
            filled=bool(value.strip()),
            free_text=_is_free_text(tag, field_type),
            value_digest=_digest(value, _VALUE_DIGEST_CHARS),
            shadow_depth=int(record.get("shadowDepth") or 0),
            value=value if self._capture_values else None,
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
