"""Four-tier Jobright Autofill trigger with observed field attribution.

The tiers are attempted in a strict, fixed order, from the least invasive to
the most:

1. `in_page` — deep-query an in-page control whose accessible text matches
   `autofill` (across same-origin frames and open shadow roots) and click it
   with a human-like mouse path.
2. `extension_popup` — bring the ATS tab to the front (extension popups act
   on the *active* tab), open `chrome-extension://<id>/popup.html` and click
   its Autofill control.
3. `service_worker` — ask the MV3 service worker to dispatch the extension
   action, strictly feature-guarded: `chrome.action.openPopup` is used only
   when it actually exists, and the worker script never throws.
4. `native_toolbar` — click the calibrated toolbar pixel with `xdotool`.

A tier "succeeding" is never taken on faith: after the tier acts, the page
must reach mutation/value quiescence *and* show changed field values, or the
tier is recorded as failed and the next one is attempted. Every failure keeps
its diagnostic; the first genuine success short-circuits the rest; all four
failing raises `TriggerFailed` carrying every diagnostic in attempt order.

Playwright is never imported here — pages, frames, contexts, workers, and the
native clicker are all duck-typed, so the whole module is unit-testable
without a browser, a display, or xdotool.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from app.agent.errors import FormSettleTimeout, TriggerFailed
from app.agent.extension import find_service_worker, probe_service_worker
from app.agent.form_scanner import (
    DEFAULT_FIRST_CHANGE_TIMEOUT_MS,
    DEFAULT_QUIET_MS,
    DEFAULT_SETTLE_TIMEOUT_MS,
    FormDiff,
    FormScanner,
    FormSnapshot,
    SettleResult,
    same_origin_frames,
)
from app.agent.native_click import NativeToolbarClick
from app.config import Settings

DEFAULT_POPUP_PATH = "popup.html"
DEFAULT_WORKER_TIMEOUT_MS = 5_000
DEFAULT_POPUP_TIMEOUT_MS = 2_000
DEFAULT_POPUP_POLL_MS = 100
#: Time given to the popup's own click handler to dispatch its message before
#: the popup page is closed again. This is not a settle wait — quiescence is
#: always observed separately by `FormScanner.wait_for_settle`.
DEFAULT_DISPATCH_GRACE_MS = 250

Sleeper = Callable[[float], Awaitable[None]]


class TriggerTier(str, Enum):
    IN_PAGE = "in_page"
    EXTENSION_POPUP = "extension_popup"
    SERVICE_WORKER = "service_worker"
    NATIVE_TOOLBAR = "native_toolbar"


#: The required attempt order. Tiers are never reordered or skipped at runtime.
TIER_ORDER: tuple[TriggerTier, ...] = (
    TriggerTier.IN_PAGE,
    TriggerTier.EXTENSION_POPUP,
    TriggerTier.SERVICE_WORKER,
    TriggerTier.NATIVE_TOOLBAR,
)


class TierActionError(Exception):
    """A tier could not perform its action; the next tier should be tried."""


@dataclass(frozen=True)
class TierAttempt:
    """One tier's outcome, retained even after a later tier succeeds."""

    tier: TriggerTier
    succeeded: bool
    detail: str


@dataclass(frozen=True)
class TriggerResult:
    """The successful tier plus the settled field diff it produced."""

    tier: TriggerTier
    diff: FormDiff
    after: FormSnapshot
    settle: SettleResult
    attempts: tuple[TierAttempt, ...]

    @property
    def failed_attempts(self) -> tuple[TierAttempt, ...]:
        return tuple(attempt for attempt in self.attempts if not attempt.succeeded)

    @property
    def settled(self) -> bool:
        """False when fields changed but the page never went quiet in time."""
        return self.settle.settled


#: Deep-queries the best control whose accessible name matches `autofill`,
#: descending through open shadow roots. Returns the element itself (via
#: `evaluate_handle`) or `null`.
#:
#: Only genuinely interactive elements are considered — not "anything with an
#: `aria-label`/`data-testid`/`onclick`", which on a real ATS page matches
#: wrappers, tracking divs, and whole page sections. An element's accessible
#: name is taken from its own attributes, or from its text only when that
#: text is short enough to belong to a control; candidates larger than a
#: fraction of the viewport are rejected outright, and among what remains the
#: smallest (and, at equal size, deepest) wins — a real button is small and
#: sits at the bottom of the tree, a container is neither.
AUTOFILL_QUERY_SCRIPT = """
(() => {
  const MATCH = /autofill/i;
  const MAX_TEXT_LENGTH = 80;
  const MAX_AREA_FRACTION = 0.35;
  const MAX_ROOT_DEPTH = 8;
  const CANDIDATE_SELECTOR = [
    'button',
    'input[type="button"]',
    'input[type="submit"]',
    'a[href]',
    'summary',
    '[role="button"]',
    '[role="menuitem"]',
    '[role="link"]',
  ].join(', ');

  const viewportArea = Math.max(
    1,
    (window.innerWidth || 0) * (window.innerHeight || 0),
  );

  const text = (value) => (value == null ? '' : String(value)).replace(/\\s+/g, ' ').trim();

  const accessibleName = (el) => {
    const explicit = text(
      el.getAttribute('aria-label')
        || el.getAttribute('title')
        || el.getAttribute('value'),
    );
    if (explicit) {
      return explicit.slice(0, MAX_TEXT_LENGTH);
    }
    const own = text(el.textContent);
    return own.length <= MAX_TEXT_LENGTH ? own : '';
  };

  const usableRect = (el) => {
    if (el.disabled === true) {
      return null;
    }
    if (text(el.getAttribute('aria-disabled')).toLowerCase() === 'true') {
      return null;
    }
    const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    const style = view.getComputedStyle(el);
    if (style) {
      if (style.display === 'none' || style.visibility === 'hidden'
          || style.visibility === 'collapse') {
        return null;
      }
      if (Number(style.opacity) === 0) {
        return null;
      }
    }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return null;
    }
    const area = rect.width * rect.height;
    if (area / viewportArea > MAX_AREA_FRACTION) {
      return null;
    }
    return { area };
  };

  const candidates = [];
  const collect = (root, depth) => {
    let found = [];
    try {
      found = root.querySelectorAll(CANDIDATE_SELECTOR);
    } catch (error) {
      return;
    }
    for (const el of found) {
      if (!MATCH.test(accessibleName(el))) {
        continue;
      }
      const usable = usableRect(el);
      if (usable) {
        candidates.push({ el, depth, area: usable.area });
      }
    }
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
        collect(host.shadowRoot, depth + 1);
      }
    }
  };
  collect(document, 0);

  candidates.sort((left, right) => (left.area - right.area) || (right.depth - left.depth));
  return candidates.length ? candidates[0].el : null;
})
""".strip()

#: Reads the live viewport so an oversized control can be rejected even when
#: the driver does not expose a configured viewport size.
VIEWPORT_SCRIPT = "() => ({ width: window.innerWidth, height: window.innerHeight })"


#: Asks the MV3 service worker to open the extension's popup, but only when
#: that API genuinely exists in this Chrome build. Always resolves to a
#: `{ok, method, reason}` record — it never throws into the caller, so an
#: unavailable API is a diagnostic rather than an exception.
WORKER_ACTION_SCRIPT = """
(async () => {
  const api = (typeof chrome !== 'undefined' && chrome)
    || (typeof browser !== 'undefined' && browser)
    || null;
  if (!api || !api.action) {
    return { ok: false, method: '', reason: 'chrome.action is unavailable in this service worker' };
  }
  if (typeof api.action.openPopup !== 'function') {
    return {
      ok: false,
      method: '',
      reason: 'chrome.action.openPopup is not available in this Chrome build',
    };
  }
  try {
    await api.action.openPopup();
    return { ok: true, method: 'action.openPopup', reason: '' };
  } catch (error) {
    return {
      ok: false,
      method: 'action.openPopup',
      reason: String((error && error.message) || error),
    };
  }
})
""".strip()


class AutofillTier(Protocol):
    """One trigger tier: act on the page, or raise with a diagnostic."""

    tier: TriggerTier

    async def attempt(self, page: Any) -> str:
        """Perform this tier's action and describe what it did."""


class AutofillClicker(Protocol):
    async def click_in(self, page: Any) -> str:
        """Find and click an Autofill control inside `page`."""


async def _bring_to_front(page: Any) -> None:
    """Focus a page, tolerating drivers/pages that cannot be focused."""
    bring_to_front = getattr(page, "bring_to_front", None)
    if bring_to_front is None:
        return
    try:
        await bring_to_front()
    except Exception:  # noqa: BLE001 - focusing is best effort
        pass


async def _close_quietly(page: Any) -> None:
    close = getattr(page, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 - teardown must not mask the real outcome
        pass


async def _dispose_quietly(handle: Any) -> None:
    dispose = getattr(handle, "dispose", None)
    if dispose is None:
        return
    try:
        await dispose()
    except Exception:  # noqa: BLE001 - handle cleanup is best effort
        pass


async def wait_for_extension_page(
    context: Any,
    extension_id: str,
    *,
    timeout_ms: int,
    sleep: Sleeper,
    known_pages: Sequence[Any] = (),
    poll_interval_ms: int = DEFAULT_POPUP_POLL_MS,
    clock: Callable[[], float] = time.monotonic,
) -> Any | None:
    """Poll a context for a *newly created* page served by `extension_id`.

    Pages that already existed before the caller acted are never returned:
    an options tab or a popup left open from an earlier attempt says nothing
    about whether this dispatch worked, and adopting one would mean clicking
    — and then closing — a page the caller does not own.

    Chrome does not always surface a natively opened popup as a driver-visible
    page, so callers treat `None` as "not reachable" rather than an error.
    """
    prefix = f"chrome-extension://{extension_id}/"
    deadline = clock() + timeout_ms / 1000
    while True:
        for page in list(getattr(context, "pages", []) or []):
            if not str(getattr(page, "url", "")).startswith(prefix):
                continue
            if any(page is known for known in known_pages):
                continue
            return page
        if clock() >= deadline:
            return None
        await sleep(poll_interval_ms / 1000)


def _existing_pages(context: Any) -> tuple[Any, ...]:
    return tuple(getattr(context, "pages", []) or [])


class DeepAutofillClicker:
    """Finds an Autofill control anywhere reachable and clicks it like a human.

    "Anywhere reachable" means every same-origin frame plus every open shadow
    root within them. The click is a smoothed multi-step mouse path with
    jitter and a randomized press duration rather than a synthetic
    `element.click()`, because extension UIs commonly react to real pointer
    events. `rng`/`sleep` are injectable so the path is deterministic in
    tests.
    """

    def __init__(
        self,
        *,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        seed: int | None = None,
        script: str = AUTOFILL_QUERY_SCRIPT,
        min_steps: int = 6,
        max_steps: int = 12,
        max_area_fraction: float = 0.35,
        min_container_area: float = 40_000.0,
    ) -> None:
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random(seed)
        self._script = script
        self._min_steps = min_steps
        self._max_steps = max_steps
        self._max_area_fraction = max_area_fraction
        self._min_container_area = min_container_area

    async def click_in(self, page: Any) -> str:
        frames, _ = same_origin_frames(page)
        for frame in frames:
            element = await self._query(frame)
            if element is None:
                continue
            try:
                await self._humanized_click(page, element)
            finally:
                await _dispose_quietly(element)
            return (
                "clicked an accessible 'autofill' control in "
                f"{getattr(frame, 'url', '') or 'the page'}"
            )
        raise TierActionError(
            "no visible control with accessible text matching 'autofill' was found "
            "in any same-origin frame or open shadow root"
        )

    async def _query(self, frame: Any) -> Any | None:
        evaluate_handle = getattr(frame, "evaluate_handle", None)
        if evaluate_handle is None:
            return None
        try:
            handle = await evaluate_handle(self._script)
        except Exception:  # noqa: BLE001 - a detached/navigating frame is not fatal
            return None
        if handle is None:
            return None

        as_element = getattr(handle, "as_element", None)
        element = as_element() if as_element is not None else handle
        if element is None:
            await _dispose_quietly(handle)
            return None
        return element

    async def _humanized_click(self, page: Any, element: Any) -> None:
        scroll = getattr(element, "scroll_into_view_if_needed", None)
        if scroll is not None:
            try:
                await scroll()
            except Exception:  # noqa: BLE001 - scrolling is best effort
                pass

        box = await element.bounding_box()
        if not box:
            raise TierActionError(
                "the matched autofill control has no bounding box (off-screen or "
                "not rendered), so it cannot be clicked like a human would"
            )
        await self._reject_oversized(page, box)

        mouse = getattr(page, "mouse", None)
        if mouse is None:
            raise TierActionError("page exposes no mouse for a human-like click")

        target_x = box["x"] + box["width"] * self._rng.uniform(0.35, 0.65)
        target_y = box["y"] + box["height"] * self._rng.uniform(0.35, 0.65)
        start_x = target_x - self._rng.uniform(80, 220)
        start_y = target_y - self._rng.uniform(60, 160)
        steps = self._rng.randint(self._min_steps, self._max_steps)

        for step in range(1, steps + 1):
            progress = step / steps
            # Smoothstep easing: accelerate away from the start, decelerate
            # into the target, the way a hand-driven pointer does.
            eased = progress * progress * (3 - 2 * progress)
            jitter_x = 0.0 if step == steps else self._rng.uniform(-1.5, 1.5)
            jitter_y = 0.0 if step == steps else self._rng.uniform(-1.5, 1.5)
            await mouse.move(
                start_x + (target_x - start_x) * eased + jitter_x,
                start_y + (target_y - start_y) * eased + jitter_y,
            )
            await self._sleep(self._rng.uniform(0.012, 0.035))

        await mouse.down()
        await self._sleep(self._rng.uniform(0.04, 0.09))
        await mouse.up()

    async def _reject_oversized(self, page: Any, box: Mapping[str, float]) -> None:
        """Refuse to click something the size of the page itself.

        The in-page query already rejects oversized candidates, but this is
        the last line of defence before a synthetic pointer press lands
        somewhere arbitrary: clicking the centre of a full-page container
        could hit any link or button underneath it. Small controls are always
        allowed, so a legitimately large button in a tiny extension popup
        still works.
        """
        viewport = await self._viewport(page)
        if viewport is None:
            return
        width, height = viewport
        area = float(box.get("width", 0.0)) * float(box.get("height", 0.0))
        if area <= self._min_container_area:
            return
        fraction = area / max(1.0, width * height)
        if fraction <= self._max_area_fraction:
            return
        raise TierActionError(
            f"the matched autofill control covers {fraction:.0%} of the viewport, "
            "which is a page container rather than a button; refusing to click it"
        )

    async def _viewport(self, page: Any) -> tuple[float, float] | None:
        size = getattr(page, "viewport_size", None)
        if isinstance(size, Mapping) and size.get("width") and size.get("height"):
            return float(size["width"]), float(size["height"])
        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return None
        try:
            measured = await evaluate(VIEWPORT_SCRIPT)
        except Exception:  # noqa: BLE001 - an unmeasurable viewport just skips the guard
            return None
        if isinstance(measured, Mapping) and measured.get("width") and measured.get("height"):
            return float(measured["width"]), float(measured["height"])
        return None


class InPageAutofillTier:
    """Tier 1: click the extension's own in-page Autofill control."""

    tier = TriggerTier.IN_PAGE

    def __init__(self, *, clicker: AutofillClicker | None = None) -> None:
        self._clicker = clicker if clicker is not None else DeepAutofillClicker()

    async def attempt(self, page: Any) -> str:
        return await self._clicker.click_in(page)


class ExtensionPopupTier:
    """Tier 2: drive `chrome-extension://<id>/popup.html` directly.

    The ATS page is brought to the front *before* the popup page is created,
    because an extension popup acts on whichever tab is active; opening it
    while some other tab has focus would autofill the wrong page. The popup
    page is always closed and focus always returned to the ATS page, including
    when the click fails.
    """

    tier = TriggerTier.EXTENSION_POPUP

    def __init__(
        self,
        extension_id: str,
        *,
        popup_path: str = DEFAULT_POPUP_PATH,
        clicker: AutofillClicker | None = None,
        sleep: Sleeper = asyncio.sleep,
        dispatch_grace_ms: int = DEFAULT_DISPATCH_GRACE_MS,
    ) -> None:
        self._extension_id = extension_id
        self._popup_path = popup_path
        self._clicker = clicker if clicker is not None else DeepAutofillClicker()
        self._sleep = sleep
        self._dispatch_grace_ms = dispatch_grace_ms

    @property
    def popup_url(self) -> str:
        return f"chrome-extension://{self._extension_id}/{self._popup_path}"

    async def attempt(self, page: Any) -> str:
        context = getattr(page, "context", None)
        if context is None:
            raise TierActionError(
                "page has no browser context, so the extension popup page cannot be opened"
            )

        await _bring_to_front(page)
        popup = await context.new_page()
        try:
            await popup.goto(self.popup_url)
            # Opening the popup page made *it* the active tab; the extension
            # acts on whichever tab is active, so hand focus back to the ATS
            # page before the click. Driving the popup itself does not need
            # OS focus, since its input is delivered over CDP.
            await _bring_to_front(page)
            detail = await self._clicker.click_in(popup)
            # Let the popup's handler dispatch its message before the page is
            # torn down; quiescence itself is observed by the caller.
            await self._sleep(self._dispatch_grace_ms / 1000)
            return f"opened the extension popup page and {detail}"
        finally:
            await _close_quietly(popup)
            await _bring_to_front(page)


class ServiceWorkerTier:
    """Tier 3: ask the MV3 service worker to dispatch the extension action.

    Strictly guarded: the worker must be discoverable *and* responsive, and
    the in-worker script only calls `chrome.action.openPopup` when that
    function actually exists, reporting a reason instead of throwing when it
    does not.
    """

    tier = TriggerTier.SERVICE_WORKER

    def __init__(
        self,
        extension_id: str,
        *,
        worker_finder: Callable[..., Awaitable[Any]] = find_service_worker,
        worker_probe: Callable[..., Awaitable[None]] = probe_service_worker,
        worker_timeout_ms: int = DEFAULT_WORKER_TIMEOUT_MS,
        clicker: AutofillClicker | None = None,
        sleep: Sleeper = asyncio.sleep,
        popup_timeout_ms: int = DEFAULT_POPUP_TIMEOUT_MS,
    ) -> None:
        self._extension_id = extension_id
        self._worker_finder = worker_finder
        self._worker_probe = worker_probe
        self._worker_timeout_ms = worker_timeout_ms
        self._clicker = clicker if clicker is not None else DeepAutofillClicker()
        self._sleep = sleep
        self._popup_timeout_ms = popup_timeout_ms

    async def attempt(self, page: Any) -> str:
        context = getattr(page, "context", None)
        if context is None:
            raise TierActionError(
                "page has no browser context, so the extension service worker "
                "cannot be reached"
            )

        worker = await self._worker_finder(
            context, self._extension_id, self._worker_timeout_ms
        )
        await self._worker_probe(worker, self._extension_id, self._worker_timeout_ms)

        # The action is dispatched against the active tab, so the ATS page
        # must be in the foreground *at the moment the worker runs*.
        await _bring_to_front(page)
        known_pages = _existing_pages(context)
        outcome = await worker.evaluate(WORKER_ACTION_SCRIPT)
        if not isinstance(outcome, Mapping):
            raise TierActionError(
                f"service worker returned an unexpected response: {outcome!r}"
            )
        if not outcome.get("ok"):
            reason = str(outcome.get("reason") or "no reason reported")
            raise TierActionError(
                f"service worker could not dispatch the extension action: {reason}"
            )

        method = str(outcome.get("method") or "the extension action")
        popup = await wait_for_extension_page(
            context,
            self._extension_id,
            timeout_ms=self._popup_timeout_ms,
            sleep=self._sleep,
            known_pages=known_pages,
        )
        if popup is None:
            return (
                f"service worker dispatched {method}, but a newly opened popup page "
                "was not reachable for a follow-up click"
            )
        try:
            await _bring_to_front(page)
            detail = await self._clicker.click_in(popup)
        finally:
            await _close_quietly(popup)
        return f"service worker dispatched {method} and {detail}"


class NativeToolbarTier:
    """Tier 4: click the calibrated toolbar pixel with `xdotool`.

    The ATS page is focused first so the toolbar click applies to it. If
    Chrome happens to expose the resulting popup as a driver-visible page,
    its Autofill control is clicked too; when it does not, the tier still
    reports what it did and the caller's field-change verification decides
    whether anything actually happened.
    """

    tier = TriggerTier.NATIVE_TOOLBAR

    def __init__(
        self,
        native_click: NativeToolbarClick | None,
        *,
        extension_id: str = "",
        clicker: AutofillClicker | None = None,
        sleep: Sleeper = asyncio.sleep,
        popup_timeout_ms: int = DEFAULT_POPUP_TIMEOUT_MS,
    ) -> None:
        self._native_click = native_click
        self._extension_id = extension_id
        self._clicker = clicker if clicker is not None else DeepAutofillClicker()
        self._sleep = sleep
        self._popup_timeout_ms = popup_timeout_ms

    async def attempt(self, page: Any) -> str:
        if self._native_click is None:
            raise TierActionError(
                "no calibrated native toolbar click is configured; run "
                "scripts/calibrate_toolbar.py and set TOOLBAR_X/TOOLBAR_Y"
            )

        context = getattr(page, "context", None)
        await _bring_to_front(page)
        known_pages = _existing_pages(context) if context is not None else ()
        await self._native_click.click()
        detail = "clicked the calibrated extension toolbar button with xdotool"

        if context is None or not self._extension_id:
            return detail

        popup = await wait_for_extension_page(
            context,
            self._extension_id,
            timeout_ms=self._popup_timeout_ms,
            sleep=self._sleep,
            known_pages=known_pages,
        )
        if popup is None:
            return detail
        try:
            await _bring_to_front(page)
            clicked = await self._clicker.click_in(popup)
        except TierActionError as exc:
            return f"{detail}; the extension popup opened but {exc}"
        finally:
            await _close_quietly(popup)
        return f"{detail} and {clicked} in the extension popup"


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


class JobrightTrigger:
    """Runs the four tiers in order and returns the settled field diff."""

    def __init__(
        self,
        extension_id: str,
        *,
        tiers: Sequence[AutofillTier] | None = None,
        scanner: Any | None = None,
        native_click: NativeToolbarClick | None = None,
        clicker: AutofillClicker | None = None,
        quiet_ms: int = DEFAULT_QUIET_MS,
        settle_timeout_ms: int = DEFAULT_SETTLE_TIMEOUT_MS,
        first_change_timeout_ms: int = DEFAULT_FIRST_CHANGE_TIMEOUT_MS,
        require_field_changes: bool = True,
    ) -> None:
        self._extension_id = extension_id
        self._scanner = scanner if scanner is not None else FormScanner()
        self._quiet_ms = quiet_ms
        self._settle_timeout_ms = settle_timeout_ms
        self._first_change_timeout_ms = first_change_timeout_ms
        self._require_field_changes = require_field_changes
        self._tiers: tuple[AutofillTier, ...] = (
            tuple(tiers)
            if tiers is not None
            else self._default_tiers(extension_id, native_click, clicker)
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "JobrightTrigger":
        kwargs.setdefault("scanner", FormScanner(capture_values=settings.log_field_values))
        kwargs.setdefault("native_click", NativeToolbarClick.from_settings(settings))
        return cls(settings.jobright_extension_id, **kwargs)

    @staticmethod
    def _default_tiers(
        extension_id: str,
        native_click: NativeToolbarClick | None,
        clicker: AutofillClicker | None,
    ) -> tuple[AutofillTier, ...]:
        shared = clicker if clicker is not None else DeepAutofillClicker()
        return (
            InPageAutofillTier(clicker=shared),
            ExtensionPopupTier(extension_id, clicker=shared),
            ServiceWorkerTier(extension_id, clicker=shared),
            NativeToolbarTier(native_click, extension_id=extension_id, clicker=shared),
        )

    @property
    def tiers(self) -> tuple[AutofillTier, ...]:
        return self._tiers

    @property
    def scanner(self) -> Any:
        """The scanner that defines which snapshots are comparable.

        Value digests are keyed per `FormScanner` instance, so a baseline
        taken with a *different* scanner would make every field look changed.
        Callers take their `before` snapshot from here (or via `baseline`).
        """
        return self._scanner

    async def baseline(self, page: Any) -> FormSnapshot:
        """Snapshot `page` with this trigger's scanner, ready for `trigger`."""
        snapshot: FormSnapshot = await self._scanner.snapshot(page)
        return snapshot

    async def trigger(self, page: Any, before: FormSnapshot) -> TriggerResult:
        """Trigger Autofill and return the tier that actually changed fields.

        Raises `TriggerFailed` — carrying one diagnostic per attempted tier —
        when no tier both acted and produced observable field changes.
        """
        attempts: list[TierAttempt] = []

        for tier in self._tiers:
            try:
                detail = await tier.attempt(page)
            except Exception as exc:  # noqa: BLE001 - a failing tier is a diagnostic
                attempts.append(TierAttempt(tier.tier, False, _describe(exc)))
                continue

            try:
                settled = await self._scanner.wait_for_settle(
                    page,
                    before,
                    quiet_ms=self._quiet_ms,
                    timeout_ms=self._settle_timeout_ms,
                    first_change_timeout_ms=self._first_change_timeout_ms,
                )
            except FormSettleTimeout as exc:
                if self._require_field_changes and exc.result.diff.has_changes:
                    # The tier worked: fields demonstrably changed. The page
                    # merely never went quiet (a spinner, a poller, an
                    # animation). Firing another tier now would re-trigger
                    # autofill on an already-filled form, so stop here and
                    # report the timeout honestly instead.
                    attempts.append(
                        TierAttempt(
                            tier.tier,
                            True,
                            f"{detail}; field values changed but the form never settled "
                            f"within {exc.timeout_ms}ms "
                            f"(no {exc.quiet_ms}ms quiet period), so the observed "
                            "changes are returned without firing further tiers",
                        )
                    )
                    return TriggerResult(
                        tier=tier.tier,
                        diff=exc.result.diff,
                        after=exc.result.snapshot,
                        settle=exc.result,
                        attempts=tuple(attempts),
                    )
                attempts.append(
                    TierAttempt(
                        tier.tier, False, f"{detail}, but the form never settled: {exc}"
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - rescan failure is a diagnostic
                attempts.append(
                    TierAttempt(
                        tier.tier,
                        False,
                        f"{detail}, but the form could not be rescanned: {_describe(exc)}",
                    )
                )
                continue

            if self._require_field_changes and not settled.diff.has_changes:
                attempts.append(
                    TierAttempt(tier.tier, False, f"{detail}, but no field values changed")
                )
                continue

            attempts.append(TierAttempt(tier.tier, True, detail))
            return TriggerResult(
                tier=tier.tier,
                diff=settled.diff,
                after=settled.snapshot,
                settle=settled,
                attempts=tuple(attempts),
            )

        raise TriggerFailed(attempts)
