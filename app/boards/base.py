"""Common typed protocol, safe YAML selector loading, and shared board safety
helpers.

Board adapters translate the fixed, code-reviewed sequence "open a listing,
then start an application" into board-specific CSS selectors. Those
selectors churn constantly — a board redesigns a button, renames a class,
moves a modal — so they live in YAML under `app/boards/selectors/`, never in
Python, and can be edited without a code change or a review of adapter
logic: `load_selector_map` is the only thing that reads them, and every
adapter reads selectors through the small `SelectorMap.require`/`get`
surface rather than a file path of its own.

Two safety properties are enforced here, once, rather than duplicated in
each adapter:

* **A listing URL is only opened once its host is confirmed to belong to the
  board.** A selector map names CSS selectors, not domains; if a listing URL
  merely *looked* like the right board
  (`linkedin.com.evil.example`, `wellfound.co`), an adapter would still
  dutifully click whatever matched `apply_button` on an attacker's page.
  `open_listing` raises `UntrustedListingUrlError` — and never calls
  `page.goto` at all — for anything that is not the board's own registrable
  domain or a subdomain of it, mirroring the same suffix rule
  `app.agent.ats_detector` uses for ATS hosts.
* **A selector map is validated before an adapter can use it.** A missing
  key, an empty selector, or a file that declares the wrong board is refused
  at load time with a specific complaint, rather than surfacing later as "no
  such element" from deep inside a click, or — worse — silently supplying
  one board's selectors to another's adapter.

Playwright is never imported here; `page` is duck-typed (`goto`,
`query_selector_all`, and whatever an `ElementHandle`-like object exposes),
so every adapter is unit-testable with a plain page double.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from app.storage.models import Board

#: Cap on a selector map file. A board's selectors are a handful of short CSS
#: strings; anything this large is either a mistake or a way to exhaust
#: memory from a file loaded automatically on every adapter construction.
MAX_SELECTOR_MAP_BYTES = 200_000

#: Registrable domains that belong to each board, matched as an exact host or
#: a dotted suffix (so `it.linkedin.com` matches `linkedin.com` and
#: `linkedin.com.evil.example` does not).
BOARD_HOSTS: Mapping[Board, tuple[str, ...]] = {
    Board.LINKEDIN: ("linkedin.com",),
    Board.JOBRIGHT: ("jobright.ai",),
    Board.WELLFOUND: ("wellfound.com", "angel.co"),
    Board.HANDSHAKE: ("joinhandshake.com", "handshake.com"),
}


class BoardAdapterError(Exception):
    """Base class for board adapter and selector-map failures."""


class SelectorMapError(BoardAdapterError):
    """Raised when a board's YAML selector map cannot be trusted."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Selector map {path} is unusable: {reason}")


class UntrustedListingUrlError(BoardAdapterError):
    """Raised when a listing URL's host does not belong to the configured board.

    Nothing is navigated when this is raised: `open_listing` checks the host
    before ever calling `page.goto`, so a lookalike domain is never even
    requested, let alone clicked into.
    """

    def __init__(self, board: Board, url: str) -> None:
        self.board = board
        self.url = url
        super().__init__(
            f"Refusing to open {url!r} as a {board.value} listing: its host is "
            f"not a recognised {board.value} domain (or a subdomain of one). "
            "This guards against following a lookalike domain and clicking "
            "whatever it presents as the apply control."
        )


class SkipReason(str, Enum):
    """Distinct, typed reasons a board adapter refuses to start an application.

    Named per unsupported flow rather than a shared "unsupported" string, so
    a queue item's persisted error reason, and any log line, says exactly
    which flow was declined rather than requiring a reader to parse prose.
    """

    LINKEDIN_EASY_APPLY = "linkedin_easy_apply_unsupported"
    WELLFOUND_IN_APP_APPLY = "wellfound_in_app_apply_unsupported"


class ListingStatus(str, Enum):
    OPENED = "opened"


@dataclass(frozen=True)
class ListingResult:
    """The outcome of successfully opening a validated listing URL.

    Only ever produced once the URL's host has been confirmed to belong to
    `board`; a lookalike or unrelated host raises `UntrustedListingUrlError`
    instead of producing a result, so a caller can never receive a "refused"
    value and accidentally proceed with it.
    """

    board: Board
    url: str
    status: ListingStatus = ListingStatus.OPENED

    @property
    def opened(self) -> bool:
        return self.status is ListingStatus.OPENED


class ApplyStatus(str, Enum):
    STARTED = "started"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of attempting to start an application from a listing page."""

    status: ApplyStatus
    reason: str
    #: The selector-map key that was clicked, when `status` is `STARTED`.
    clicked: str | None = None

    @property
    def started(self) -> bool:
        return self.status is ApplyStatus.STARTED

    @property
    def skipped(self) -> bool:
        return self.status is ApplyStatus.SKIPPED

    @property
    def failed(self) -> bool:
        return self.status is ApplyStatus.FAILED


@runtime_checkable
class BoardAdapter(Protocol):
    """The common typed protocol every board adapter implements."""

    board: Board

    async def open_listing(self, page: Any, url: str) -> ListingResult:
        """Validate that `url` belongs to this board, then navigate to it.

        Raises `UntrustedListingUrlError` — before touching `page` at all —
        when the host is not this board's own domain or a subdomain of it.
        """
        ...

    async def start_application(self, page: Any) -> ApplyResult:
        """Begin an application from an already-open listing page."""
        ...


@dataclass(frozen=True)
class SelectorMap:
    """A validated, immutable mapping of selector name to CSS selector."""

    board: Board
    source: str
    selectors: Mapping[str, str]

    def require(self, name: str) -> str:
        """Return the named selector, or raise if it is not configured."""
        try:
            return self.selectors[name]
        except KeyError:
            raise SelectorMapError(
                self.source, f"missing required selector {name!r}"
            ) from None

    def get(self, name: str) -> str | None:
        """Return the named selector, or `None` when it is not configured.

        Used for selectors that are genuinely optional (Jobright's direct
        Apply-with-Autofill control): its absence is a normal configuration,
        not an error.
        """
        return self.selectors.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self.selectors


def default_selector_path(board: Board) -> Path:
    return Path(__file__).parent / "selectors" / f"{board.value}.yaml"


def load_selector_map(
    board: Board,
    path: Path | str | None = None,
    *,
    required: Sequence[str] = (),
) -> SelectorMap:
    """Load, parse, and validate one board's YAML selector map.

    Selectors are free to churn: redesigning a button only requires editing
    this file, with no code change and no adapter test needing to change.
    What *is* enforced here is the shape adapters depend on, so a broken file
    fails loudly at load time rather than surfacing later as a confusing
    mid-application "element not found":

    * the file must parse as a mapping with a top-level `board` key naming
      *this* board — a copy-pasted `linkedin.yaml` loaded for `jobright`
      would otherwise silently supply the wrong board's selectors;
    * `selectors` must be a mapping of non-empty selector names to
      non-empty, non-whitespace CSS selector strings;
    * every name in `required` must be present, so an adapter's own
      construction fails before it is ever asked to open a page.

    A missing file is an error, not an empty map: unlike an optional answers
    file, an adapter with no selectors at all cannot do anything, so silently
    proceeding would only fail later and more confusingly.
    """
    location = Path(path) if path is not None else default_selector_path(board)
    if not location.exists():
        raise SelectorMapError(str(location), "file does not exist")

    try:
        size = location.stat().st_size
    except OSError as exc:
        raise SelectorMapError(str(location), f"could not be read: {exc}") from None
    if size > MAX_SELECTOR_MAP_BYTES:
        raise SelectorMapError(
            str(location),
            f"is {size} bytes, larger than the {MAX_SELECTOR_MAP_BYTES}-byte "
            "limit. A selector map is a short list of CSS selectors; "
            "something this size is a mistake, and parsing it would be a way "
            "to exhaust memory from a file loaded automatically.",
        )

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a dependency
        raise SelectorMapError(str(location), f"PyYAML is not installed ({exc})") from None
    try:
        raw = yaml.safe_load(location.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - every parse failure is fatal
        raise SelectorMapError(str(location), f"could not be parsed: {exc}") from None

    return _validate_selector_map(board, str(location), raw, required)


def _validate_selector_map(
    board: Board, source: str, raw: Any, required: Sequence[str]
) -> SelectorMap:
    if not isinstance(raw, Mapping):
        raise SelectorMapError(source, f"top level must be a mapping; got {type(raw).__name__}")

    declared_board = raw.get("board")
    if declared_board != board.value:
        raise SelectorMapError(
            source,
            f"declares board {declared_board!r}, but was loaded for "
            f"{board.value!r}; a mismatched file would silently supply the "
            "wrong board's selectors",
        )

    raw_selectors = raw.get("selectors")
    if not isinstance(raw_selectors, Mapping):
        raise SelectorMapError(
            source, f"'selectors' must be a mapping; got {type(raw_selectors).__name__}"
        )

    selectors: dict[str, str] = {}
    for name, value in raw_selectors.items():
        if not isinstance(name, str) or not name.strip():
            raise SelectorMapError(source, f"selector name {name!r} must be a non-empty string")
        if not isinstance(value, str) or not value.strip():
            raise SelectorMapError(
                source, f"selector {name!r} must be a non-empty string, got {value!r}"
            )
        selectors[name] = value

    missing = sorted(name for name in required if name not in selectors)
    if missing:
        raise SelectorMapError(
            source, f"missing required selector(s): {', '.join(missing)}"
        )

    return SelectorMap(board=board, source=source, selectors=selectors)


def _split_host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:  # malformed IPv6 literals and similar
        return ""
    return host.strip().lower().rstrip(".")


def host_owned_by_board(url: str, board: Board) -> bool:
    """Whether `url`'s host is `board`'s own registrable domain (or a subdomain).

    Matched as an exact host or a dotted suffix, never a substring, so
    `linkedin.com.evil.example` and `notlinkedin.com` are refused just like
    `app.agent.ats_detector` refuses the equivalent ATS lookalikes. The query
    string and path are never consulted: only the host in the address bar
    says who actually served the page.
    """
    host = _split_host(url)
    if not host:
        return False
    for domain in BOARD_HOSTS.get(board, ()):
        if host == domain or host.endswith("." + domain):
            return True
    return False


async def _find_single(page: Any, selector: str) -> Any | None:
    """Return the one element matching `selector` on `page`, or `None`.

    Zero matches and *more than one* match are both treated as "not found":
    clicking an ambiguous selector could land on the wrong one of several
    similar controls, which is worse than not clicking anything.
    """
    query_all = getattr(page, "query_selector_all", None)
    if query_all is None:
        return None
    try:
        matches = list(await query_all(selector))
    except Exception:  # noqa: BLE001 - a selector/query failure means "not present"
        return None
    matches = [match for match in matches if match is not None]
    if len(matches) != 1:
        return None
    return matches[0]


async def _is_visible(element: Any) -> bool:
    is_visible = getattr(element, "is_visible", None)
    if is_visible is None:
        return True
    try:
        return bool(await is_visible())
    except Exception:  # noqa: BLE001 - an unverifiable element is not clickable
        return False


async def click_selector(page: Any, selector: str) -> bool:
    """Find exactly one visible element matching `selector` and click it.

    Returns whether a click happened, and raises nothing: no match, more
    than one match, and an invisible match are all ordinary "did not click"
    outcomes as a page's markup varies, not exceptions. This is the only way
    adapters click an "explicit configured control" — an ambiguous or absent
    selector is refused rather than guessed at.
    """
    element = await _find_single(page, selector)
    if element is None:
        return False
    if not await _is_visible(element):
        return False
    click = getattr(element, "click", None)
    if click is None:
        return False
    await click()
    return True


async def selector_present(page: Any, selector: str) -> bool:
    """Whether at least one *visible* element matches `selector`.

    Used for detection-only checks (e.g. "is this an Easy Apply listing?")
    where an adapter must know a control exists without touching anything —
    unlike `click_selector`, more than one match is fine here, since nothing
    is clicked either way.
    """
    query_all = getattr(page, "query_selector_all", None)
    if query_all is None:
        return False
    try:
        matches = await query_all(selector)
    except Exception:  # noqa: BLE001 - a selector/query failure means "not present"
        return False
    for match in matches or []:
        if match is None:
            continue
        if await _is_visible(match):
            return True
    return False


class BaseBoardAdapter:
    """Shared `open_listing` safety check for every concrete board adapter.

    Subclasses set the `board` class attribute and implement
    `start_application`; construction refuses a `SelectorMap` built for a
    different board, so an adapter can never be silently wired to the wrong
    selectors.
    """

    board: Board

    def __init__(self, selectors: SelectorMap) -> None:
        if selectors.board is not self.board:
            raise SelectorMapError(
                selectors.source,
                f"selector map is for board {selectors.board.value!r}, not "
                f"{self.board.value!r}",
            )
        self.selectors = selectors

    async def open_listing(self, page: Any, url: str) -> ListingResult:
        if not host_owned_by_board(url, self.board):
            raise UntrustedListingUrlError(self.board, url)
        goto = getattr(page, "goto", None)
        if goto is None:
            raise BoardAdapterError(
                f"page has no goto(); cannot open {url!r} as a {self.board.value} listing"
            )
        await goto(url)
        return ListingResult(board=self.board, url=url)

    async def start_application(self, page: Any) -> ApplyResult:  # pragma: no cover
        raise NotImplementedError
