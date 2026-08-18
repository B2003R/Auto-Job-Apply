"""Classify which applicant tracking system is serving a page.

The classification drives which form the rest of the graph expects to see, so
the expensive mistake is a *confident wrong answer*, not an unknown one:
`AtsKind.UNKNOWN` is a typed, recoverable outcome that skips the listing,
while a page misread as Greenhouse would be driven with Greenhouse's
selectors. Three rules follow from that:

* **Hosts are matched as registrable-domain suffixes, never as substrings.**
  `boards.greenhouse.io.evil.example` and `mylever.co` are not the ATS they
  imitate, and an ATS host that appears only in a query parameter
  (`?utm_source=boards.greenhouse.io`) is a referrer, not a form.
* **The DOM is read as structure, never as prose.** Only attribute *names* and
  attribute *values* are examined — plus URLs appearing in them — so a page
  about cutting greenhouse gas emissions, or one that leverages new tooling,
  produces no signal at all. `<meta>` tags are the one place markup carries
  human copy in an attribute, so only the handful that declare the *software*
  behind the page (`generator`, `application-name`, `powered-by`) are read;
  descriptions, keywords, and Open Graph cards are ignored. HTML comments are
  stripped first, since commented markup is not what is rendering.
* **One bare vendor word is not evidence.** A lone `class="greenhouse"` or
  `id="lever"` scores below `MIN_CONFIDENT_SCORE` and yields `UNKNOWN`,
  because that is equally what a careers blog, a CSS theme, or an analytics
  hook looks like. Vendor-specific markers — an ATS host, `grnhse_app`,
  `lever-application-form`, `data-source="greenhouse"`, Workday's
  `data-automation-id` — still decide on their own.
* **Conflicts are resolved by evidence strength, and ties are refused.** A
  Workday URL beats a stray `lever` class name; two equally-supported DOM
  candidates with no URL evidence return `UNKNOWN` with `ambiguous=True`
  rather than picking the alphabetically luckier one.

`AtsDetection` carries the signals behind the answer for logging, and those
signals only ever contain the matched marker (a host, an attribute name, a
short token) — never surrounding markup or field values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

#: Records one piece of evidence: `(kind, source, marker, weight)`.
_SignalSink = Callable[["AtsKind", str, str, int], None]

#: Cap on how much markup is scanned. A listing page is a few hundred KB; a
#: multi-megabyte document is either an outlier or a denial-of-service, and
#: the markers we look for live in the head and the form wrapper anyway.
MAX_HTML_CHARS = 2_000_000


class AtsKind(str, Enum):
    """Recognised applicant tracking systems, plus the honest fallback.

    Values are the strings persisted in `applications.ats`.
    """

    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ICIMS = "icims"
    SMARTRECRUITERS = "smartrecruiters"
    UNKNOWN = "unknown"


#: Evidence weights. A host in the address bar is the strongest statement a
#: page can make about who serves it; a bare vendor word in a class name is
#: the weakest thing that counts at all.
WEIGHT_URL_HOST = 4
WEIGHT_URL_PATH = 2
WEIGHT_ATTR_URL = 3
WEIGHT_ATTR_NAME = 2
WEIGHT_META_GENERATOR = 2
WEIGHT_VENDOR_TOKEN = 2
WEIGHT_GENERIC_TOKEN = 1

#: Minimum score required to name an ATS at all. Set above
#: `WEIGHT_GENERIC_TOKEN` on purpose: a company page mentions its tooling in
#: class names, theme names, and analytics hooks all the time, and a single
#: bare "greenhouse" or "lever" is not evidence that the page *is* that ATS.
#: Anything vendor-specific — a URL, a provider attribute, a framework
#: identifier, a `data-automation-id` — clears the bar on its own.
MIN_CONFIDENT_SCORE = 2

#: Registrable domains that belong to each ATS. Matched as exact host or as a
#: dotted suffix, so `acme.wd1.myworkdayjobs.com` matches and
#: `fakemyworkdayjobs.com` does not.
_HOSTS: Mapping[AtsKind, tuple[str, ...]] = {
    AtsKind.WORKDAY: (
        "myworkdayjobs.com",
        "myworkdaysite.com",
        "myworkday.com",
        "workday.com",
    ),
    AtsKind.GREENHOUSE: ("greenhouse.io", "grnh.se"),
    AtsKind.LEVER: ("lever.co",),
    AtsKind.ICIMS: ("icims.com",),
    AtsKind.SMARTRECRUITERS: ("smartrecruiters.com",),
}

#: Path fragments that identify an ATS even when it is served from a customer
#: domain (a reverse proxy or a vanity careers host).
_PATHS: Mapping[AtsKind, tuple[re.Pattern[str], ...]] = {
    AtsKind.WORKDAY: (re.compile(r"/wday/"),),
    AtsKind.GREENHOUSE: (re.compile(r"/embed/job_(?:board|app)\b"),),
    AtsKind.ICIMS: (re.compile(r"/icims/"),),
}

#: Attribute *names* that only one ATS emits. `data-automation-id` is
#: Workday's own test hook and appears on every control it renders.
_ATTRIBUTE_NAMES: Mapping[AtsKind, tuple[str, ...]] = {
    AtsKind.WORKDAY: ("data-automation-id", "data-metadata-id"),
}

#: Vendor-specific markers: identifiers that only the ATS itself emits, or a
#: vendor name fused to an application-form role. Each one alone is enough.
_VENDOR_TOKENS: Mapping[AtsKind, tuple[re.Pattern[str], ...]] = {
    AtsKind.WORKDAY: (
        re.compile(r"\bworkday[-_]?(application|job|jobs|form|site|requisition)", re.IGNORECASE),
        re.compile(r"\bmyworkday(jobs|site)?\b", re.IGNORECASE),
        re.compile(r"\bwd[1-9]\d?[-_]", re.IGNORECASE),
    ),
    AtsKind.GREENHOUSE: (
        re.compile(r"\bgrnhse[-_]?", re.IGNORECASE),
        re.compile(
            r"\bgreenhouse[-_]?(application|apply|job|jobs|board|form|iframe|embed)",
            re.IGNORECASE,
        ),
    ),
    AtsKind.LEVER: (
        re.compile(r"\blever[-_]?(application|apply|job|jobs|form|postings)", re.IGNORECASE),
        re.compile(r"\bpostings[-_]?lever\b", re.IGNORECASE),
    ),
    # "icims" and "smartrecruiters" are invented words with no ordinary
    # English meaning, so their bare presence is already vendor-specific.
    AtsKind.ICIMS: (re.compile(r"\bicims", re.IGNORECASE),),
    AtsKind.SMARTRECRUITERS: (re.compile(r"\bsmart[-_]?recruiters\b", re.IGNORECASE),),
}

#: Bare vendor words. Real when they appear on the ATS's own markup, but also
#: what a careers blog, a CSS theme, or an analytics hook says, so one alone
#: never reaches `MIN_CONFIDENT_SCORE`.
_GENERIC_TOKENS: Mapping[AtsKind, tuple[re.Pattern[str], ...]] = {
    AtsKind.WORKDAY: (re.compile(r"\bworkday\b", re.IGNORECASE),),
    AtsKind.GREENHOUSE: (re.compile(r"\bgreenhouse\b", re.IGNORECASE),),
    AtsKind.LEVER: (re.compile(r"\blever\b", re.IGNORECASE),),
}

#: Attributes whose values are URLs worth resolving against the host rules.
#: `content` is absent deliberately — see `_meta_signals`.
_URL_ATTRIBUTES = frozenset({"src", "href", "action", "formaction", "data-src", "data-url"})

#: Attributes whose values name something structural (an id, a class, a data
#: hook). Deliberately excludes `value`, `placeholder`, `alt`, `title`, and
#: `content`, which carry user-facing prose rather than framework identity.
_TOKEN_ATTRIBUTES = frozenset(
    {
        "id",
        "class",
        "name",
        "rel",
        "data-source",
        "data-provider",
        "data-ats",
        "data-qa",
        "data-testid",
        "data-automation-id",
    }
)

#: Attributes that exist to *declare* which system rendered the form. A
#: vendor name here is a statement of provenance, not a coincidence, so even
#: a bare one counts as vendor-specific.
_PROVIDER_ATTRIBUTES = frozenset({"data-source", "data-provider", "data-ats"})

#: `<meta name="...">` values that describe the software that built the page.
#: Everything else a `<meta>` can carry — descriptions, keywords, Open Graph
#: and Twitter cards — is marketing copy written for humans, and classifying
#: on it would let "we cut greenhouse gas emissions" pick an ATS.
_FRAMEWORK_META_NAMES = frozenset(
    {"generator", "application-name", "powered-by", "framework", "ats", "x-powered-by"}
)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ATTRIBUTE_RE = re.compile(
    r"""([a-zA-Z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))"""
)
_META_TAG_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)


@dataclass(frozen=True)
class AtsSignal:
    """One piece of evidence for one ATS.

    `marker` is always the matched identifier itself (a host, an attribute
    name, a normalized token), never the markup it was found in.
    """

    kind: AtsKind
    source: str  # "url_host" | "url_path" | "attr_url" | "attr_name" | "attr_token"
    marker: str
    weight: int


@dataclass(frozen=True)
class AtsDetection:
    """The classification plus the evidence behind it."""

    kind: AtsKind
    confidence: float
    signals: tuple[AtsSignal, ...] = ()
    #: True when two ATSes were equally well supported, which is why `kind`
    #: is `UNKNOWN` despite there being evidence.
    ambiguous: bool = False

    @property
    def recognised(self) -> bool:
        return self.kind is not AtsKind.UNKNOWN


def detect_ats(url: str, html: str | None = None) -> AtsKind:
    """Classify a page, falling back to `AtsKind.UNKNOWN`."""
    return classify_ats(url, html).kind


def classify_ats(url: str, html: str | None = None) -> AtsDetection:
    """Classify a page and report the evidence that decided it."""
    signals = tuple((*_url_signals(url or ""), *_dom_signals(html or "")))
    if not signals:
        return AtsDetection(kind=AtsKind.UNKNOWN, confidence=0.0)

    scores: dict[AtsKind, int] = {}
    for signal in signals:
        scores[signal.kind] = scores.get(signal.kind, 0) + signal.weight

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_kind, best_score = ranked[0]
    if best_score < MIN_CONFIDENT_SCORE:
        # Something matched, but only a bare vendor word in a class or an id.
        # That is how a careers blog, a CSS theme, or an analytics hook looks
        # too, so it is reported as evidence without naming an ATS.
        return AtsDetection(
            kind=AtsKind.UNKNOWN,
            confidence=0.0,
            signals=tuple(signal for signal in signals if signal.kind is best_kind),
        )
    if len(ranked) > 1 and ranked[1][1] == best_score:
        # Two ATSes are equally well supported. Guessing between them would
        # hand the graph selectors for a form that is not on the page; the
        # tied evidence travels with the refusal so it can be logged.
        tied = {kind for kind, score in ranked if score == best_score}
        return AtsDetection(
            kind=AtsKind.UNKNOWN,
            confidence=0.0,
            signals=tuple(signal for signal in signals if signal.kind in tied),
            ambiguous=True,
        )

    return AtsDetection(
        kind=best_kind,
        confidence=min(1.0, best_score / WEIGHT_URL_HOST),
        signals=tuple(signal for signal in signals if signal.kind is best_kind),
    )


def _host_kind(host: str) -> AtsKind | None:
    """Match a hostname as an exact or dotted-suffix registrable domain."""
    cleaned = host.strip().lower().rstrip(".")
    if not cleaned:
        return None
    for kind, domains in _HOSTS.items():
        for domain in domains:
            if cleaned == domain or cleaned.endswith("." + domain):
                return kind
    return None


def _split_host(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:  # malformed IPv6 literals and similar
        return ""
    host = parts.hostname or ""
    return host


def _url_signals(url: str) -> tuple[AtsSignal, ...]:
    """Signals from the address bar: host first, then telling path shapes.

    The query string is never consulted — an ATS host arriving as
    `?utm_source=` or `?next=` describes where the visitor came from, not
    what is rendering the form.
    """
    signals: list[AtsSignal] = []
    host = _split_host(url)
    kind = _host_kind(host)
    if kind is not None:
        signals.append(AtsSignal(kind, "url_host", host.lower(), WEIGHT_URL_HOST))

    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    if path:
        for path_kind, patterns in _PATHS.items():
            for pattern in patterns:
                if pattern.search(path):
                    signals.append(
                        AtsSignal(path_kind, "url_path", pattern.pattern, WEIGHT_URL_PATH)
                    )
                    break
    return tuple(signals)


def _attribute_pairs(markup: str) -> Iterable[tuple[str, str]]:
    """Yield `(name, value)` for every attribute assignment in `markup`."""
    for match in _ATTRIBUTE_RE.finditer(markup):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        yield name, value


def _meta_signals(html: str, add: "_SignalSink") -> None:
    """Classify only `<meta>` tags that declare the page's software.

    A `<meta name="generator" content="Greenhouse">` is the page saying what
    built it. A description, a keyword list, or an Open Graph card is copy
    written for people, and reading those as evidence would let "we cut
    greenhouse gas emissions" or "Lever Careers at Acme" decide which form
    parser runs.
    """
    for match in _META_TAG_RE.finditer(html):
        attributes = {name: value for name, value in _attribute_pairs(match.group(1))}
        name = attributes.get("name", "").strip().lower()
        if name not in _FRAMEWORK_META_NAMES:
            continue
        matched = _token_kind(attributes.get("content", ""), provider_attribute=True)
        if matched is not None:
            kind, _marker_text, weight = matched
            add(kind, "meta_generator", name, max(weight, WEIGHT_META_GENERATOR))


def _token_kind(
    value: str, *, provider_attribute: bool
) -> tuple[AtsKind, str, int] | None:
    """Classify one attribute value, reporting how specific the match was.

    A vendor-specific marker (`grnhse_app`, `lever-application`, `icims`)
    stands on its own; a bare vendor word only does when the attribute that
    carries it exists to declare a provider. The matched text is returned so
    the logged marker stays a short token rather than the whole value.
    """
    for kind, patterns in _VENDOR_TOKENS.items():
        for pattern in patterns:
            match = pattern.search(value)
            if match is not None:
                return kind, match.group(0).lower(), WEIGHT_VENDOR_TOKEN
    for kind, patterns in _GENERIC_TOKENS.items():
        for pattern in patterns:
            match = pattern.search(value)
            if match is not None:
                weight = (
                    WEIGHT_VENDOR_TOKEN if provider_attribute else WEIGHT_GENERIC_TOKEN
                )
                return kind, match.group(0).lower(), weight
    return None


def _dom_signals(html: str) -> tuple[AtsSignal, ...]:
    """Signals from attribute names, attribute values, and embedded URLs.

    Text nodes are never read, so page copy cannot influence the answer, and
    comments are stripped first: markup that is commented out is not what the
    page is rendering, so a stale integration snippet cannot decide the
    classification.
    """
    if not html:
        return ()

    signals: list[AtsSignal] = []
    seen: set[tuple[AtsKind, str, str]] = set()

    def add(kind: AtsKind, source: str, marker: str, weight: int) -> None:
        identity = (kind, source, marker)
        if identity in seen:
            return
        seen.add(identity)
        signals.append(AtsSignal(kind, source, marker, weight))

    trimmed = _COMMENT_RE.sub(" ", html[:MAX_HTML_CHARS])
    _meta_signals(trimmed, add)

    for name, value in _attribute_pairs(trimmed):
        for kind, markers in _ATTRIBUTE_NAMES.items():
            if name in markers:
                add(kind, "attr_name", name, WEIGHT_ATTR_NAME)

        if name in _URL_ATTRIBUTES and "//" in value:
            url_kind = _host_kind(_split_host(value))
            if url_kind is not None:
                add(url_kind, "attr_url", _split_host(value).lower(), WEIGHT_ATTR_URL)

        if name not in _TOKEN_ATTRIBUTES:
            continue
        matched = _token_kind(value, provider_attribute=name in _PROVIDER_ATTRIBUTES)
        if matched is not None:
            kind, marker, weight = matched
            add(kind, "attr_token", marker, weight)

    return tuple(signals)
