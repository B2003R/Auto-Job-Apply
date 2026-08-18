"""Minimal, dependency-free HTML page double for board adapter tests.

Board adapters are driven through a duck-typed Playwright-shaped `page`
(`goto`, `query_selector_all`, and whatever an `ElementHandle`-like object
exposes). Rather than hand-building `query_selector_all` return values per
test — which would test the fake, not the actual selector strings shipped in
`app/boards/selectors/*.yaml` — `FakePage` parses a small recorded HTML
snippet with the standard library's `html.parser` and evaluates a
deliberately small subset of CSS against it: the exact subset this project's
own selector maps use — type, `#id`, `.class`,
`[attr]`/`[attr='v']`/`[attr*='v']` (optionally a trailing ` i` for
case-insensitive matching, per the CSS selectors spec Playwright also
implements), and `:not(...)` wrapping one such compound. There are no
combinators (descendant/child/sibling): every selector in this project names
one element directly, and the fixtures below are built so that holds.

This is not a general CSS engine. A selector using anything outside that
subset raises `SelectorSyntaxError` loudly rather than silently matching
nothing, so a test with an unsupported selector fails on its own bug rather
than passing for the wrong reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Sequence


class SelectorSyntaxError(ValueError):
    """A selector used syntax outside this test double's supported subset."""


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)
    clicks: int = 0

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def is_visible(self) -> bool:
        if "hidden" in self.attrs:
            return False
        style = self.attrs.get("style", "").replace(" ", "").lower()
        return "display:none" not in style and "visibility:hidden" not in style

    def description(self) -> str:
        return (
            self.attrs.get("data-test")
            or self.attrs.get("data-hook")
            or self.attrs.get("aria-label")
            or self.tag
        )


class _TreeBuilder(HTMLParser):
    _VOID_TAGS = frozenset({"br", "img", "input", "hr", "meta", "link"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#document", attrs={})
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._push(tag, attrs, self_closing=tag in self._VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._push(tag, attrs, self_closing=True)

    def _push(
        self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool
    ) -> None:
        node = Node(tag=tag, attrs={name: (value or "") for name, value in attrs})
        node.parent = self._stack[-1]
        self._stack[-1].children.append(node)
        if not self_closing:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    return builder.root


def _iter_descendants(node: Node) -> list[Node]:
    result: list[Node] = []
    for child in node.children:
        result.append(child)
        result.extend(_iter_descendants(child))
    return result


@dataclass
class Compound:
    tag: str | None = None
    ids: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    # (name, op, value_or_None, case_insensitive); op is "exists" | "=" | "*="
    attrs: list[tuple[str, str, str | None, bool]] = field(default_factory=list)
    nots: list["Compound"] = field(default_factory=list)


_TAG_RE = re.compile(r"[a-zA-Z][\w-]*")
_ID_RE = re.compile(r"#([\w-]+)")
_CLASS_RE = re.compile(r"\.([\w-]+)")
_NOT_RE = re.compile(r":not\(([^()]*)\)")
_ATTR_RE = re.compile(
    r"""\[\s*(?P<name>[a-zA-Z_:][-\w:.]*)\s*"""
    r"""(?:(?P<op>=|\*=)\s*(?P<quote>['"])(?P<value>.*?)(?P=quote))?\s*"""
    r"""(?P<ci>i)?\s*\]"""
)


def parse_compound(selector: str) -> Compound:
    """Parse one compound selector: an optional tag plus id/class/attr/:not() parts."""
    text = selector.strip()
    compound = Compound()
    pos = 0
    tag_match = _TAG_RE.match(text, pos)
    if tag_match:
        compound.tag = tag_match.group(0)
        pos = tag_match.end()

    length = len(text)
    while pos < length:
        match = _NOT_RE.match(text, pos)
        if match:
            compound.nots.append(parse_compound(match.group(1)))
            pos = match.end()
            continue
        match = _ID_RE.match(text, pos)
        if match:
            compound.ids.append(match.group(1))
            pos = match.end()
            continue
        match = _CLASS_RE.match(text, pos)
        if match:
            compound.classes.append(match.group(1))
            pos = match.end()
            continue
        match = _ATTR_RE.match(text, pos)
        if match:
            compound.attrs.append(
                (match.group("name"), match.group("op") or "exists", match.group("value"), bool(match.group("ci")))
            )
            pos = match.end()
            continue
        raise SelectorSyntaxError(
            f"unsupported selector syntax at position {pos} in {selector!r}; "
            "tests/boards/support.py supports only tag/#id/.class/[attr]/:not(), "
            "with no combinators"
        )
    return compound


def compound_matches(node: Node, compound: Compound) -> bool:
    if compound.tag is not None and node.tag != compound.tag:
        return False
    node_classes = node.classes()
    if any(cls not in node_classes for cls in compound.classes):
        return False
    if any(node.attrs.get("id") != id_ for id_ in compound.ids):
        return False
    for name, op, value, ci in compound.attrs:
        if name not in node.attrs:
            return False
        if op == "exists":
            continue
        actual = node.attrs[name]
        actual_cmp = actual.lower() if ci else actual
        value_cmp = (value or "").lower() if ci else (value or "")
        if op == "=" and actual_cmp != value_cmp:
            return False
        if op == "*=" and value_cmp not in actual_cmp:
            return False
    for negated in compound.nots:
        if compound_matches(node, negated):
            return False
    return True


class FakeElement:
    """An `ElementHandle`-like double wrapping one parsed `Node`."""

    def __init__(self, node: Node, page: "FakePage") -> None:
        self._node = node
        self._page = page

    @property
    def node(self) -> Node:
        return self._node

    async def is_visible(self) -> bool:
        return self._node.is_visible()

    async def click(self) -> None:
        self._node.clicks += 1
        self._page.clicked.append(self._node.description())


class FakePage:
    """A `Page`-like double over a recorded HTML snippet.

    `goto` records the navigated URL and re-parses `html_by_url` for it when
    provided, so `open_listing` tests can assert exactly one navigation
    happened (or none, for a refused lookalike host).
    """

    def __init__(self, html: str, *, url: str = "https://example.invalid/listing") -> None:
        self.url = url
        self.root = parse_html(html)
        self.clicked: list[str] = []
        self.goto_calls: list[str] = []
        self.queried_selectors: list[str] = []

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def query_selector_all(self, selector: str) -> Sequence[FakeElement]:
        self.queried_selectors.append(selector)
        compound = parse_compound(selector)
        return [
            FakeElement(node, self)
            for node in _iter_descendants(self.root)
            if compound_matches(node, compound)
        ]


class ClickRaisesElement:
    """Wraps a `FakeElement`, making `click()` raise instead of clicking.

    Models a Playwright `click()` that fails *after* the element was found
    and confirmed visible — a timeout waiting for the element to be
    "actionable", a detached node from a concurrent re-render, and so on.
    Crucially, unlike a query or visibility failure, a real click may have
    already dispatched pointer/press events to the page before raising, so
    tests using this double can assert that adapters never quietly treat it
    the same as "nothing was there".
    """

    def __init__(self, inner: FakeElement, exc: BaseException) -> None:
        self._inner = inner
        self._exc = exc

    async def is_visible(self) -> bool:
        return await self._inner.is_visible()

    async def click(self) -> None:
        raise self._exc


class VisibilityRaisesElement:
    """Wraps a `FakeElement`, making `is_visible()` raise instead of answering.

    Models a Playwright `is_visible()` that fails outright (e.g. the frame
    navigated away while the check was in flight). Nothing is dispatched to
    the page by a visibility check, so this is always safe to treat the same
    as "not visible" and fall back to a different control.
    """

    def __init__(self, inner: FakeElement, exc: BaseException) -> None:
        self._inner = inner
        self._exc = exc

    async def is_visible(self) -> bool:
        raise self._exc

    async def click(self) -> None:
        await self._inner.click()


class FaultInjectingPage:
    """Wraps a `FakePage`, injecting one fault into one selector's matches.

    `fault="query"` makes `query_selector_all(selector)` itself raise, as if
    the query never ran at all — nothing was found, let alone clicked.
    `fault="visible"` and `fault="click"` wrap every element the wrapped page
    returns for `selector` in `VisibilityRaisesElement` / `ClickRaisesElement`
    respectively, leaving every other selector's results untouched.
    """

    def __init__(
        self, page: FakePage, *, selector: str, fault: str, exc: BaseException
    ) -> None:
        self._page = page
        self._selector = selector
        self._fault = fault
        self._exc = exc

    def __getattr__(self, name: str) -> object:
        return getattr(self._page, name)

    async def goto(self, url: str) -> None:
        await self._page.goto(url)

    async def query_selector_all(self, selector: str) -> Sequence[object]:
        if selector == self._selector and self._fault == "query":
            self._page.queried_selectors.append(selector)
            raise self._exc
        matches = await self._page.query_selector_all(selector)
        if selector != self._selector:
            return matches
        if self._fault == "visible":
            return [VisibilityRaisesElement(m, self._exc) for m in matches]
        if self._fault == "click":
            return [ClickRaisesElement(m, self._exc) for m in matches]
        return matches
