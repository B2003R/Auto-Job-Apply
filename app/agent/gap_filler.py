"""Decide, per unanswered field, who is allowed to answer it.

Jobright's autofill leaves gaps. Filling them is the point at which this
project can do real harm — a fabricated visa answer, an invented salary, a
guessed employment date — so the decision is expressed as an explicit,
inspectable plan with exactly three outcomes per field:

* **canonical** — the applicant already wrote this answer down in
  `answers.yaml`. It is used verbatim and attributed to the *user*, because
  that is who wrote it.
* **model** — an ordinary question (a cover letter, "why this team?") with no
  canonical answer, which a model may draft.
* **human** — everything else, including every protected question without a
  canonical answer, every model refusal, and every field a model must not
  touch at all (files, passwords, checkboxes).

Four categories are protected: visa/sponsorship, EEO/demographics,
compensation, and employment dates. They are never sent to a model and never
invented here; a protected question also keeps `blocks_auto_submit` true even
when it *is* canonically answered, so the approval gate always sees it. The
classification is deliberately generous — it matches semantic variants of the
same question ("Do you require sponsorship?", "Are you legally authorized to
work in the US?", "What is your immigration status?") — because the cost of
over-classifying is one extra human decision, while the cost of
under-classifying is a fabricated answer submitted under someone's name.

Field values never enter the plan: fields arrive as scanner metadata (key,
label, name, type, required/filled flags) and the value the page already
holds is not read. Answers that the plan *produces* are redacted from every
repr and omitted from `log_payload()` unless `log_field_values` is on.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Pattern, Protocol, Sequence

from app.agent.errors import AnswerBookError, ModelError
from app.agent.form_scanner import FormField
from app.agent.model_router import Complexity, ModelAnswer
from app.config import Settings
from app.storage.models import FieldSource

#: Wrapper that gives a model the question and nothing else — no page, no
#: existing values, no applicant profile.
QUESTION_TEMPLATE = (
    "A job application form asks the following question. Draft a short, "
    "truthful answer, or reply UNKNOWN if it cannot be answered from the "
    "question alone.\n\nQuestion: {label}"
)

_REDACTED = "<redacted>"

_WHITESPACE = re.compile(r"[\s\u00a0]+")
_DECORATION = re.compile(r"[\*:?•\-\u2013\u2014\s]+$")


class ProtectedCategory(str, Enum):
    """Question categories only the applicant may answer."""

    VISA_SPONSORSHIP = "visa_sponsorship"
    EEO_DEMOGRAPHICS = "eeo_demographics"
    COMPENSATION = "compensation"
    EMPLOYMENT_DATES = "employment_dates"


class Resolution(str, Enum):
    """Who is allowed to supply this field's answer."""

    CANONICAL = "canonical"
    MODEL = "model"
    HUMAN = "human"


#: Semantic variants per protected category. Patterns are word-bounded so
#: they match phrasing rather than substrings; `race` carries one explicit
#: exception because "race condition" is a normal engineering question, and
#: mis-routing it to a human would be a papercut on every technical form.
_PROTECTED_PATTERNS: Mapping[ProtectedCategory, tuple[Pattern[str], ...]] = {
    ProtectedCategory.VISA_SPONSORSHIP: (
        re.compile(r"\bsponsor(ship|ships|ed|ing)?\b", re.IGNORECASE),
        re.compile(r"\bvisas?\b", re.IGNORECASE),
        re.compile(r"\bh-?1-?b\b", re.IGNORECASE),
        re.compile(r"\b(opt|cpt|ead|tn)\s+status\b", re.IGNORECASE),
        re.compile(
            r"\bwork\s+(authori[sz]ation|permit|eligibility|status)\b", re.IGNORECASE
        ),
        re.compile(r"\bauthori[sz]ed\s+to\s+work\b", re.IGNORECASE),
        re.compile(r"\bright\s+to\s+work\b", re.IGNORECASE),
        re.compile(r"\bcitizens?(hip)?\b", re.IGNORECASE),
        re.compile(r"\bimmigration\b", re.IGNORECASE),
        re.compile(r"\bnationality\b", re.IGNORECASE),
    ),
    ProtectedCategory.EEO_DEMOGRAPHICS: (
        re.compile(r"\brace\b(?!\s*condition)", re.IGNORECASE),
        re.compile(r"\bethnic(ity|ities)?\b", re.IGNORECASE),
        re.compile(r"\bhispanic\b|\blatino\b|\blatinx\b", re.IGNORECASE),
        re.compile(r"\bgender\b|\bsex\b(?!\w)", re.IGNORECASE),
        re.compile(r"\bsexual\s+orientation\b", re.IGNORECASE),
        re.compile(r"\bveterans?\b", re.IGNORECASE),
        re.compile(r"\bdisabilit(y|ies)\b|\bdisabled\b", re.IGNORECASE),
        re.compile(r"\bself[-_\s]?identif(y|ication|ying)\b", re.IGNORECASE),
        re.compile(r"\beeo(c|-1)?\b", re.IGNORECASE),
        re.compile(r"\bequal\s+(employment|opportunity)\b", re.IGNORECASE),
        re.compile(r"\bdate\s+of\s+birth\b|\bbirth\s*date\b|\bdob\b", re.IGNORECASE),
        re.compile(r"\bmarital\s+status\b", re.IGNORECASE),
        re.compile(r"\bnational\s+origin\b", re.IGNORECASE),
        re.compile(r"\bpronouns?\b", re.IGNORECASE),
        re.compile(r"\breligion\b|\breligious\b", re.IGNORECASE),
    ),
    ProtectedCategory.COMPENSATION: (
        re.compile(r"\bsalar(y|ies)\b", re.IGNORECASE),
        re.compile(r"\bcompensation\b|\btotal\s+comp\b", re.IGNORECASE),
        re.compile(r"\bwages?\b", re.IGNORECASE),
        re.compile(r"\b(base|hourly|desired|expected|current)\s+(pay|rate)\b", re.IGNORECASE),
        re.compile(r"\bpay\s+(expectation|expectations|range|rate|requirements?)\b", re.IGNORECASE),
        re.compile(r"\brate\s+expectation", re.IGNORECASE),
        re.compile(r"\bequity\s+expectation", re.IGNORECASE),
    ),
    ProtectedCategory.EMPLOYMENT_DATES: (
        re.compile(r"\bemployment\s+dates?\b", re.IGNORECASE),
        re.compile(r"\bdates?\s+of\s+employment\b", re.IGNORECASE),
        re.compile(r"\b(start|end|leaving|termination)\s+date\b", re.IGNORECASE),
        re.compile(r"\bdate\s+(you\s+)?(started|left|ended)\b", re.IGNORECASE),
        re.compile(r"\bdid\s+you\s+start\b", re.IGNORECASE),
        re.compile(r"\bmm\s*/\s*yyyy\b", re.IGNORECASE),
        re.compile(r"\b(from|to)\s*\(\s*mm", re.IGNORECASE),
    ),
}

#: Category order is fixed so classification is deterministic when a label
#: could match more than one (e.g. "salary history dates").
_CATEGORY_ORDER: tuple[ProtectedCategory, ...] = (
    ProtectedCategory.VISA_SPONSORSHIP,
    ProtectedCategory.EEO_DEMOGRAPHICS,
    ProtectedCategory.COMPENSATION,
    ProtectedCategory.EMPLOYMENT_DATES,
)


def normalize_question(text: str) -> str:
    """Fold a label into a lookup key.

    Mirrors the scanner's label normalization (collapse whitespace, drop
    trailing required markers and punctuation, casefold) so a canonical
    answer written as "Preferred name" matches a page rendering "Preferred
    Name *".
    """
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    return _DECORATION.sub("", collapsed).casefold()


def protected_category_for(*texts: str) -> ProtectedCategory | None:
    """Classify any of several texts into a protected category.

    Callers pass everything they know about a field — its label, its control
    name, its id — because ATS pages routinely leave the label blank and
    carry the meaning in `name="eeo_race"` instead.
    """
    for category in _CATEGORY_ORDER:
        patterns = _PROTECTED_PATTERNS[category]
        for text in texts:
            if not text:
                continue
            haystack = _WHITESPACE.sub(" ", text.replace("_", " ").replace(".", " "))
            for pattern in patterns:
                if pattern.search(haystack):
                    return category
    return None


class AnswerRouter(Protocol):
    """The slice of `ModelRouter` this module uses (duck-typed for tests)."""

    async def complete(
        self, question: str, complexity: Complexity = Complexity.ROUTINE
    ) -> ModelAnswer: ...


@dataclass(frozen=True)
class CanonicalAnswer:
    """One human-authored answer and the phrasings it responds to."""

    question: str
    value: str
    aliases: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"CanonicalAnswer(question={self.question!r}, value={_REDACTED})"


class AnswerBook:
    """Canonical answers, consulted before any model.

    A malformed file is rejected whole rather than partially applied: a
    dropped entry would silently send a question the applicant already
    answered to a model instead.
    """

    def __init__(self, entries: Sequence[CanonicalAnswer] = (), source: str = "<memory>") -> None:
        self._entries = tuple(entries)
        self._source = source
        self._by_question: dict[str, CanonicalAnswer] = {}
        self._by_name: dict[str, CanonicalAnswer] = {}
        for entry in self._entries:
            for phrase in (entry.question, *entry.aliases):
                key = normalize_question(phrase)
                if key in self._by_question:
                    raise AnswerBookError(
                        source,
                        f"question {phrase!r} is answered more than once, so which "
                        "answer applies is ambiguous",
                    )
                self._by_question[key] = entry
            for name in entry.names:
                self._by_name[name.strip().casefold()] = entry

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"AnswerBook(source={self._source!r}, entries={len(self._entries)})"

    @property
    def entries(self) -> tuple[CanonicalAnswer, ...]:
        return self._entries

    @property
    def source(self) -> str:
        return self._source

    @classmethod
    def empty(cls) -> "AnswerBook":
        return cls((), "<empty>")

    @classmethod
    def load(cls, settings: Settings) -> "AnswerBook":
        return cls.from_yaml(settings.answers_path)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "AnswerBook":
        """Read a YAML answers file; a missing file is simply empty.

        Missing is not an error because running with no canonical answers is
        a legitimate (if human-heavy) configuration; unreadable or malformed
        *is* an error, because it means answers exist but are not being
        applied.
        """
        location = Path(path)
        if not location.exists():
            return cls((), str(location))
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - PyYAML is a dependency
            raise AnswerBookError(str(location), f"PyYAML is not installed ({exc})") from None
        try:
            raw = yaml.safe_load(location.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - every parse failure is fatal
            raise AnswerBookError(str(location), f"could not be parsed: {exc}") from None
        return cls.from_mapping(raw, source=str(location))

    @classmethod
    def from_mapping(cls, data: Any, source: str = "<memory>") -> "AnswerBook":
        """Build a book from either supported shape.

        Both a mapping (`"Question": "Answer"`) and a list of entries with
        aliases/names are accepted, because the mapping is what a person
        writes by hand and the list is what a person needs as soon as one
        answer has to match several phrasings.
        """
        if data is None:
            return cls((), source)
        if not isinstance(data, Mapping):
            raise AnswerBookError(source, "top level must be a mapping with an 'answers' key")

        raw = data.get("answers")
        if raw is None:
            return cls((), source)

        entries: list[CanonicalAnswer] = []
        if isinstance(raw, Mapping):
            for question, value in raw.items():
                entries.append(cls._entry(source, {"question": question, "value": value}))
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                if not isinstance(item, Mapping):
                    raise AnswerBookError(
                        source, f"each answer must be a mapping; got {type(item).__name__}"
                    )
                entries.append(cls._entry(source, item))
        else:
            raise AnswerBookError(
                source, f"'answers' must be a mapping or a list; got {type(raw).__name__}"
            )
        return cls(entries, source)

    @staticmethod
    def _entry(source: str, item: Mapping[str, Any]) -> CanonicalAnswer:
        question = str(item.get("question", "")).strip()
        if not question:
            raise AnswerBookError(source, "an answer is missing its 'question'")
        value = item.get("value")
        text = "" if value is None else str(value).strip()
        if not text:
            raise AnswerBookError(
                source,
                f"answer for {question!r} is empty; an empty canonical answer would "
                "silently fall through to a model",
            )
        return CanonicalAnswer(
            question=question,
            value=text,
            aliases=tuple(str(alias) for alias in _as_sequence(item.get("aliases"))),
            names=tuple(str(name) for name in _as_sequence(item.get("names"))),
        )

    def lookup(self, field: FormField) -> CanonicalAnswer | None:
        """Find the applicant's own answer for a field, if they wrote one.

        Control names are checked before labels: a name like
        `linkedin_url` is chosen by the ATS and stable, while a label is
        prose that a page may reword.
        """
        for candidate in (field.name, field.control_id):
            entry = self._by_name.get(candidate.strip().casefold()) if candidate else None
            if entry is not None:
                return entry
        return self._by_question.get(normalize_question(field.label))


@dataclass(frozen=True)
class GapFillItem:
    """One unanswered field and who may answer it."""

    key: str
    label: str
    name: str
    field_type: str
    required: bool
    free_text: bool
    resolution: Resolution
    reason: str
    category: ProtectedCategory | None = None
    #: The prompt that would be (or was) sent to a model. `None` for every
    #: item a model must not see.
    question: str | None = None
    complexity: Complexity | None = None
    answer: str | None = None
    source: FieldSource | None = None
    cost: Decimal = Decimal("0")

    def __repr__(self) -> str:
        """Never renders the answer; a repr reaches logs and tracebacks."""
        answered = _REDACTED if self.answer is not None else None
        return (
            f"GapFillItem(key={self.key!r}, label={self.label!r}, "
            f"resolution={self.resolution.value!r}, category="
            f"{self.category.value if self.category else None!r}, answer={answered})"
        )

    __str__ = __repr__

    @property
    def protected(self) -> bool:
        return self.category is not None

    @property
    def answered(self) -> bool:
        return self.answer is not None


@dataclass(frozen=True)
class GapFillPlan:
    """Every open gap, with its decided (and possibly executed) resolution."""

    items: tuple[GapFillItem, ...] = ()
    #: Frames the scanner could not read, carried through so a consumer can
    #: tell "no gaps" from "no gaps that were visible".
    skipped_frames: tuple[str, ...] = ()

    @property
    def canonical(self) -> tuple[GapFillItem, ...]:
        return tuple(item for item in self.items if item.resolution is Resolution.CANONICAL)

    @property
    def model_questions(self) -> tuple[GapFillItem, ...]:
        return tuple(item for item in self.items if item.resolution is Resolution.MODEL)

    @property
    def human_required(self) -> tuple[GapFillItem, ...]:
        return tuple(item for item in self.items if item.resolution is Resolution.HUMAN)

    @property
    def protected(self) -> tuple[GapFillItem, ...]:
        return tuple(item for item in self.items if item.protected)

    @property
    def requires_human(self) -> bool:
        """Whether some field can only be answered by the applicant."""
        return bool(self.human_required)

    @property
    def blocks_auto_submit(self) -> bool:
        """Whether the approval gate must run regardless of `AUTO_SUBMIT`.

        True when any field needs the applicant, and also whenever a
        protected question appears at all — including one already answered
        canonically, because a human should still see a visa, EEO,
        compensation, or employment-date answer before it is submitted on
        their behalf.
        """
        return bool(self.protected) or self.requires_human

    @property
    def model_cost(self) -> Decimal:
        return sum((item.cost for item in self.items), Decimal("0"))

    @property
    def coverage_complete(self) -> bool:
        """Whether every frame of the page was actually scanned.

        False means this plan describes the gaps that were *visible*, not
        necessarily all of them, so "nothing left to answer" cannot be taken
        at face value.
        """
        return not self.skipped_frames

    def log_payload(self, *, include_values: bool = False) -> tuple[dict[str, Any], ...]:
        """Structured provenance for the application log.

        Answers are omitted unless `include_values` is set (which the filler
        wires to `LOG_FIELD_VALUES`), so the default log records *what was
        decided* without recording what was written.
        """
        payload: list[dict[str, Any]] = []
        for item in self.items:
            entry: dict[str, Any] = {
                "key": item.key,
                "label": item.label,
                "name": item.name,
                "field_type": item.field_type,
                "required": item.required,
                "resolution": item.resolution.value,
                "category": item.category.value if item.category else None,
                "source": item.source.value if item.source else None,
                "answered": item.answered,
                "reason": item.reason,
                "cost": str(item.cost),
            }
            if include_values and item.answer is not None:
                entry["answer"] = item.answer
            payload.append(entry)
        return tuple(payload)


class GapFiller:
    """Plans, and optionally executes, the filling of unanswered fields."""

    def __init__(
        self,
        answers: AnswerBook,
        *,
        router: AnswerRouter | None = None,
        log_field_values: bool = False,
    ) -> None:
        self._answers = answers
        self._router = router
        self._log_field_values = log_field_values

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        router: AnswerRouter | None = None,
    ) -> "GapFiller":
        return cls(
            AnswerBook.load(settings),
            router=router,
            log_field_values=settings.log_field_values,
        )

    @property
    def log_field_values(self) -> bool:
        return self._log_field_values

    @property
    def answers(self) -> AnswerBook:
        return self._answers

    def plan(
        self,
        fields: Iterable[FormField],
        *,
        skipped_frames: Sequence[str] = (),
    ) -> GapFillPlan:
        """Decide who may answer each open gap. Calls no model.

        `skipped_frames` carries the scanner's coverage diagnostics through
        to the plan, so a consumer can tell an empty gap list from an empty
        *observation*.
        """
        return GapFillPlan(
            items=tuple(self._plan_field(field) for field in _gaps(fields)),
            skipped_frames=tuple(skipped_frames),
        )

    async def fill(
        self,
        fields: Iterable[FormField],
        *,
        skipped_frames: Sequence[str] = (),
    ) -> GapFillPlan:
        """Plan, then ask the model only about the fields it may see."""
        plan = self.plan(fields, skipped_frames=skipped_frames)
        resolved: list[GapFillItem] = []
        for item in plan.items:
            if item.resolution is not Resolution.MODEL:
                resolved.append(item)
                continue
            resolved.append(await self._ask_model(item))
        return GapFillPlan(items=tuple(resolved), skipped_frames=plan.skipped_frames)

    def log_payload(self, plan: GapFillPlan) -> tuple[dict[str, Any], ...]:
        return plan.log_payload(include_values=self._log_field_values)

    def _plan_field(self, field: FormField) -> GapFillItem:
        category = protected_category_for(field.label, field.name, field.control_id)
        canonical = self._answers.lookup(field)

        if canonical is not None:
            return self._item(
                field,
                Resolution.CANONICAL,
                reason=(
                    "answered by the applicant in the canonical answers file"
                    + (f" ({category.value} question)" if category else "")
                ),
                category=category,
                answer=canonical.value,
                source=FieldSource.USER,
            )

        if category is not None:
            return self._item(
                field,
                Resolution.HUMAN,
                reason=(
                    f"{category.value} questions are never answered by a model or "
                    "inferred here; the applicant must answer this one"
                ),
                category=category,
            )

        if not field.free_text:
            return self._item(
                field,
                Resolution.HUMAN,
                reason=(
                    f"a {field.field_type or field.tag} control is not a free-text "
                    "question a model can answer"
                ),
            )

        question_text = field.label.strip() or field.name.strip()
        if not question_text:
            return self._item(
                field,
                Resolution.HUMAN,
                reason=(
                    "the control carries no label or name to identify what it is "
                    "asking, so there is no question to put to a model"
                ),
            )

        if self._router is None:
            return self._item(
                field,
                Resolution.HUMAN,
                reason="no model is configured, so this gap needs a human answer",
            )

        complexity = complexity_for(field)
        return self._item(
            field,
            Resolution.MODEL,
            reason=f"ordinary free-text question routed to the {complexity.value} model",
            question=QUESTION_TEMPLATE.format(label=question_text),
            complexity=complexity,
        )

    async def _ask_model(self, item: GapFillItem) -> GapFillItem:
        # Belt and braces: `plan` never marks a protected field as MODEL, but
        # this is the last point before a question would be transmitted.
        if item.category is not None or item.question is None:
            return _replace(
                item,
                resolution=Resolution.HUMAN,
                reason="refused to send a protected question to a model",
                question=None,
            )

        router = self._router
        if router is None:  # pragma: no cover - plan never routes without one
            return _replace(
                item,
                resolution=Resolution.HUMAN,
                reason="no model is configured, so this gap needs a human answer",
            )

        try:
            answer = await router.complete(
                item.question, item.complexity or Complexity.ROUTINE
            )
        except ModelError as exc:
            return _replace(
                item,
                resolution=Resolution.HUMAN,
                reason=f"the model could not be reached ({exc}); a human must answer",
            )

        if answer.declined:
            return _replace(
                item,
                resolution=Resolution.HUMAN,
                reason="the model declined to answer, so a human must",
                cost=answer.cost,
            )
        return _replace(
            item,
            answer=answer.text,
            source=FieldSource.LLM,
            reason=f"drafted by the {answer.tier.value} model",
            cost=answer.cost,
        )

    @staticmethod
    def _item(
        field: FormField,
        resolution: Resolution,
        *,
        reason: str,
        category: ProtectedCategory | None = None,
        question: str | None = None,
        complexity: Complexity | None = None,
        answer: str | None = None,
        source: FieldSource | None = None,
    ) -> GapFillItem:
        return GapFillItem(
            key=field.key,
            label=field.label,
            name=field.name,
            field_type=field.field_type,
            required=field.required,
            free_text=field.free_text,
            resolution=resolution,
            reason=reason,
            category=category,
            question=question,
            complexity=complexity,
            answer=answer,
            source=source,
        )


def complexity_for(field: FormField) -> Complexity:
    """Route long-form prose to the escalation model, everything else routine.

    A table, not a judgement call: the same field always costs the same.
    """
    if field.tag == "textarea" or field.field_type in {"textarea", "contenteditable"}:
        return Complexity.ESCALATION
    return Complexity.ROUTINE


def _gaps(fields: Iterable[FormField]) -> list[FormField]:
    """Open gaps as the scanner defines them: required-empty or empty prose."""
    return [field for field in fields if field.is_required_gap or field.is_free_text_gap]


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value)
    return (value,)


def _replace(item: GapFillItem, **changes: Any) -> GapFillItem:
    return dataclasses.replace(item, **changes)
