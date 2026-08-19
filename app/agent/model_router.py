"""Deterministic routing to an OpenAI-compatible chat completions endpoint.

Everything about this module is shaped by the fact that a language model is
the *least* trusted source of an answer in this project:

* **Routing is a lookup, not a heuristic.** A question's complexity maps to
  exactly one model through a fixed table, so the same run costs the same
  money and produces the same routing decision every time. `temperature` is
  0 for the same reason.
* **Protected questions never leave the process.** Visa/sponsorship,
  EEO/demographic, compensation, and employment-date questions are refused
  here, at the only place that could actually transmit them, in addition to
  being excluded upstream by the gap filler. Defence in depth, because the
  cost of the failure is an invented answer submitted under someone's name.
* **A partial answer is not an answer.** Only `finish_reason` values known to
  mean "finished" are accepted; anything else, including every spelling of
  truncation and anything a future provider invents, is rejected, because
  truncated prose still reads like prose and would be typed into a form
  mid-sentence. A refusal — reported by `finish_reason`, by
  `message.refusal`, or simply written out as "I'm sorry, I don't have enough
  information" — comes back as `declined`, so the field goes to the applicant
  instead.
* **The model that answered must be the model that was priced.** A response
  attributed to anything but the configured model (or a dated snapshot of it,
  which bills the same) is refused rather than recorded, since there is no
  honest price for a model nobody configured.
* **Cost is exact.** Token prices are `Decimal` from configuration to
  arithmetic; a fraction of a cent per call compounds over thousands of
  calls, and a float would make the ledger disagree with the invoice. Usage
  the provider did not report is an error, never an estimate.
* **The key is write-only.** It lives in a `SecretStr`, is read exactly once
  per request to build a header, and every error string is scrubbed of it
  before it can reach a message, a log, or a traceback.

`httpx` is imported lazily and the transport is injectable, so unit tests run
entirely offline and an installation that never uses a model never needs the
dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from app.agent.errors import ModelResponseError, ModelUnavailable, ProtectedQuestionError
from app.config import Settings

#: Instruction sent with every question. The model is told to decline rather
#: than guess: a refusal routes the field back to the human, while a
#: confident fabrication would be submitted on their behalf.
SYSTEM_PROMPT = (
    "You are helping a job applicant complete an application form. "
    "Answer the question truthfully and concisely using only the information "
    "in the question itself. Do not invent facts about the applicant. "
    "If you cannot answer from what you were given, reply exactly with "
    "'UNKNOWN' so a human can answer instead."
)

#: How much of an error body is quoted back. Enough to diagnose, little
#: enough that a chatty provider cannot flood a log.
_MAX_ERROR_DETAIL_CHARS = 300

_REDACTED = "***redacted***"

#: Shortest key prefix treated as secret. Below this a "prefix" is just a
#: provider's scheme marker ("sk-") appearing in ordinary prose.
_MIN_REDACTED_PREFIX_CHARS = 12

#: A dated or numbered snapshot of the configured model: `-2024-07-18`,
#: `:20260801`, `-v2`. Must start with a digit (or a `v` and a digit), so
#: `-mini-clone` is a different model rather than a version of this one.
_SNAPSHOT_SUFFIX_RE = re.compile(r"[-:@](v?\d[\w.\-]*)", re.IGNORECASE)

#: The only `finish_reason` values that mean "the model finished saying what
#: it had to say". An allowlist, because providers spell truncation half a
#: dozen ways — `length`, `model_length`, `truncated`, `token_limit`,
#: `max_output_tokens` — and a denylist of the spellings we thought of
#: accepts the next one as a complete answer. Tool-call outcomes are absent
#: deliberately: this module sends no tools, so `tool_calls` means the
#: provider did something unrequested rather than that it answered.
_SUCCESSFUL_FINISH_REASONS = frozenset({"stop", "end_turn", "eos", "stop_sequence"})

#: Spellings of truncation that get a message naming the cause. Anything else
#: unrecognised is rejected too, just with a vaguer explanation.
_TRUNCATED_FINISH_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "content_length",
        "model_length",
        "truncated",
        "token_limit",
        "max_output_tokens",
        "max_completion_tokens",
    }
)

#: `finish_reason` values that mean the provider withheld an answer. Unlike
#: truncation this is not a fault to raise on; it is the model declining,
#: which routes the field to the applicant.
_DECLINED_FINISH_REASONS = frozenset({"content_filter", "refusal"})

#: Ways a model says "I can't answer this". Anchored at the start, because
#: a genuine answer can easily *contain* "cannot" or "unknown" ("my
#: unknown-unknowns list is short") while an actual refusal opens with one.
_DECLINED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^unknown\b", re.IGNORECASE),
    re.compile(r"^(i'?m|i am)\s+(sorry|unable|not able|afraid)\b", re.IGNORECASE),
    re.compile(r"^sorry\b", re.IGNORECASE),
    re.compile(r"^(i|we)\s+(apologi[sz]e|cannot|can'?t|can not)\b", re.IGNORECASE),
    re.compile(r"^(i|we)\s+(do not|don'?t)\s+(have|know)\b", re.IGNORECASE),
    re.compile(r"^as an ai\b", re.IGNORECASE),
    re.compile(r"^unfortunately,?\s+(i|we)\b", re.IGNORECASE),
    re.compile(r"^there (is|are)\s+(insufficient|not enough|no)\b", re.IGNORECASE),
    re.compile(r"^(insufficient|not enough|no)\s+information\b", re.IGNORECASE),
)


class Complexity(str, Enum):
    """How hard a question is, as judged by the caller."""

    ROUTINE = "routine"
    ESCALATION = "escalation"


class ModelTier(str, Enum):
    """Which configured model answered."""

    ROUTINE = "routine"
    ESCALATION = "escalation"


#: The whole routing policy. A table, so it can be read and asserted rather
#: than reasoned about.
_TIER_FOR_COMPLEXITY: Mapping[Complexity, ModelTier] = {
    Complexity.ROUTINE: ModelTier.ROUTINE,
    Complexity.ESCALATION: ModelTier.ESCALATION,
}


@dataclass(frozen=True)
class ModelProfile:
    """One configured model and what its tokens cost."""

    tier: ModelTier
    name: str
    base_url: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(frozen=True)
class TokenUsage:
    """Token counts as reported by the provider, never estimated locally."""

    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelAnswer:
    """A model's reply, with the tier, usage, and exact cost behind it."""

    text: str
    tier: ModelTier
    #: What the provider said answered — the configured model, or the dated
    #: snapshot of it that was actually served. Anything else is refused
    #: before an answer is built, so this is never a model whose prices were
    #: not the ones the cost was computed from.
    model: str
    usage: TokenUsage
    cost: Decimal
    #: Set when the provider itself reported a refusal, independently of what
    #: the text says.
    refused: bool = False

    @property
    def declined(self) -> bool:
        """Whether the model said it could not answer.

        A declined answer is not a gap filled; the caller must route the
        field to a human rather than typing "UNKNOWN" — or "I'm sorry, I
        don't have enough information about the applicant" — into a form.

        Matching is anchored at the start of the reply rather than searching
        anywhere in it, so a real answer that happens to contain "cannot" or
        "unknown" is still an answer.
        """
        if self.refused:
            return True
        text = self.text.strip()
        return any(pattern.search(text) for pattern in _DECLINED_PATTERNS)


def token_cost(profile: ModelProfile, usage: TokenUsage) -> Decimal:
    """Exact cost of one completion, in USD.

    Prices are quoted per million tokens, and dividing by a power of ten is
    exact in `Decimal`, so the result carries no rounding error at all.
    """
    return (
        Decimal(usage.input_tokens) * profile.input_price_per_million
        + Decimal(usage.output_tokens) * profile.output_price_per_million
    ) / Decimal(1_000_000)


class ModelRouter:
    """Selects a model for a question's complexity and completes it."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Any | None = None,
        timeout_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._timeout_s = timeout_s if timeout_s is not None else settings.model_timeout_s
        self._max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else settings.model_max_output_tokens
        )

    def __repr__(self) -> str:
        """Deliberately omits the API key, which a dataclass repr would leak."""
        routine = self.select(Complexity.ROUTINE)
        escalation = self.select(Complexity.ESCALATION)
        return (
            f"ModelRouter(routine={routine.name!r}, escalation={escalation.name!r}, "
            f"timeout_s={self._timeout_s})"
        )

    def select(self, complexity: Complexity) -> ModelProfile:
        """Map a complexity to its configured model. Pure and total."""
        try:
            tier = _TIER_FOR_COMPLEXITY[Complexity(complexity)]
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"unknown question complexity {complexity!r}; expected one of "
                f"{[member.value for member in Complexity]}"
            ) from exc

        settings = self._settings
        if tier is ModelTier.ROUTINE:
            return ModelProfile(
                tier=tier,
                name=settings.routine_model_name,
                base_url=settings.routine_model_url,
                input_price_per_million=Decimal(settings.routine_input_price),
                output_price_per_million=Decimal(settings.routine_output_price),
            )
        return ModelProfile(
            tier=tier,
            name=settings.escalation_model_name,
            base_url=settings.escalation_model_url,
            input_price_per_million=Decimal(settings.escalation_input_price),
            output_price_per_million=Decimal(settings.escalation_output_price),
        )

    async def complete(
        self,
        question: str,
        complexity: Complexity = Complexity.ROUTINE,
    ) -> ModelAnswer:
        """Answer one question with the model its complexity selects.

        Raises `ProtectedQuestionError` before building a request when the
        question belongs to a category only the applicant may answer, and
        `ModelUnavailable` when no request could be made at all — both of
        which the caller turns into "a human must answer this", never into a
        filled field.
        """
        self._refuse_protected(question)

        profile = self.select(complexity)
        api_key = self._api_key()
        if not api_key:
            raise ModelUnavailable(
                "no OPENAI_API_KEY is configured, so no completion was requested; "
                "this question needs a human answer"
            )

        httpx = self._httpx()
        payload = {
            "model": profile.name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
        }

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout_s
            ) as client:
                response = await client.post(
                    profile.endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - every transport failure is
            # the same outcome for the caller: no answer, ask a human.
            raise ModelUnavailable(
                f"{type(exc).__name__}: {self._redact(str(exc))}"
            ) from None

        if response.status_code != 200:
            raise ModelResponseError(response.status_code, self._detail(response))
        return self._parse(response, profile)

    def _parse(self, response: Any, profile: ModelProfile) -> ModelAnswer:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is unusable
            raise ModelResponseError(
                response.status_code,
                "response body was not JSON: " + self._detail(response),
            ) from None

        if not isinstance(body, Mapping):
            raise ModelResponseError(response.status_code, "response body was not an object")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelResponseError(response.status_code, "response carried no choices")
        choice: Mapping[str, Any] = choices[0] if isinstance(choices[0], Mapping) else {}
        raw_message = choice.get("message")
        message: Mapping[str, Any] = raw_message if isinstance(raw_message, Mapping) else {}

        finish_reason = choice.get("finish_reason")
        finish = finish_reason.strip().lower() if isinstance(finish_reason, str) else ""
        if finish in _TRUNCATED_FINISH_REASONS:
            raise ModelResponseError(
                response.status_code,
                f"answer was truncated at the output limit (finish_reason "
                f"{finish!r}); a partial answer must not be typed into a form",
            )
        if finish and finish not in _SUCCESSFUL_FINISH_REASONS | _DECLINED_FINISH_REASONS:
            # Fail closed. An unrecognised reason is not evidence that the
            # model finished, and treating it as one is how the next
            # provider's word for truncation gets typed into a form.
            raise ModelResponseError(
                response.status_code,
                f"unrecognised finish_reason {finish!r}; only "
                f"{sorted(_SUCCESSFUL_FINISH_REASONS)} are known to mean the "
                "answer is complete, so this reply is not usable",
            )

        refusal = message.get("refusal")
        refused = finish in _DECLINED_FINISH_REASONS or (
            isinstance(refusal, str) and bool(refusal.strip())
        )

        content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        if not text and not refused:
            raise ModelResponseError(
                response.status_code, "response carried an empty answer"
            )

        served = self._verify_model(body.get("model"), profile, response.status_code)

        usage = self._parse_usage(body.get("usage"), response.status_code)
        return ModelAnswer(
            # A refusal with no content still needs a text: `UNKNOWN` is the
            # word this module's own system prompt asks for, so the caller
            # sees one shape of "no answer" rather than two.
            text=text or "UNKNOWN",
            tier=profile.tier,
            model=served,
            usage=usage,
            cost=token_cost(profile, usage),
            refused=refused,
        )

    @staticmethod
    def _verify_model(raw: Any, profile: ModelProfile, status: int) -> str:
        """Refuse an answer from a model other than the one that was priced.

        Cost is computed from the *configured* model's prices, so a gateway
        that quietly serves something else makes every figure in the ledger
        wrong — and the ledger is the only record of what this agent spent.
        Rejecting is preferred to re-pricing because there is no honest price
        for a model nobody configured.

        A dated snapshot (`gpt-4o-mini-2024-07-18` for `gpt-4o-mini`) is the
        one substitution that bills identically, so it is accepted and
        recorded under the name the provider reported.
        """
        if not isinstance(raw, str) or not raw.strip():
            # Not every OpenAI-compatible server echoes the model; absent is
            # not the same claim as a different one.
            return profile.name
        served = raw.strip()
        configured = profile.name.strip()
        if served == configured:
            return served
        if served.startswith(configured) and _SNAPSHOT_SUFFIX_RE.fullmatch(
            served[len(configured) :]
        ):
            return served
        raise ModelResponseError(
            status,
            f"answer came from model {served!r}, but {configured!r} was requested "
            "and is what its token prices describe; refusing rather than billing "
            "one model at another's rates",
        )

    @staticmethod
    def _parse_usage(raw: Any, status: int) -> TokenUsage:
        """Read reported token counts, refusing to invent missing ones.

        An estimated cost would quietly understate spend and make the
        recorded per-application cost unfalsifiable, so an unusable usage
        block fails the call instead.
        """
        if not isinstance(raw, Mapping):
            raise ModelResponseError(status, "response reported no token usage")
        counts: list[int] = []
        for field in ("prompt_tokens", "completion_tokens"):
            value = raw.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelResponseError(
                    status, f"response reported an unusable {field}: {value!r}"
                )
            counts.append(value)
        return TokenUsage(input_tokens=counts[0], output_tokens=counts[1])

    @staticmethod
    def _refuse_protected(question: str) -> None:
        # Imported lazily: the protected-category taxonomy belongs to the gap
        # filler (the safety layer), and importing it at module scope would
        # invert the dependency between a transport and its policy.
        from app.agent.gap_filler import protected_category_for

        category = protected_category_for(question)
        if category is not None:
            raise ProtectedQuestionError(category.value, question)

    def _api_key(self) -> str:
        secret = self._settings.openai_api_key
        value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        return str(value).strip()

    def _httpx(self) -> Any:
        """Import `httpx` on use, so the dependency is genuinely optional."""
        try:
            import httpx
        except ImportError as exc:
            raise ModelUnavailable(
                f"httpx is not installed, so no completion could be requested ({exc})"
            ) from None
        if httpx is None:  # pragma: no cover - defensive, seen only when stubbed
            raise ModelUnavailable("httpx is unavailable")
        return httpx

    def _detail(self, response: Any) -> str:
        """Quote a bounded piece of a response body, key-free.

        Redaction runs over the *whole* body before the excerpt is cut. Doing
        it the other way round leaves a key that straddles the cut as an
        unmatched prefix in the message: the exact leak redaction exists to
        prevent, and one that only appears when the body happens to be the
        wrong length.
        """
        try:
            text = str(response.text)
        except Exception:  # noqa: BLE001 - undecodable bodies still need a message
            return "<unreadable response body>"
        return self._redact(text)[:_MAX_ERROR_DETAIL_CHARS]

    def _redact(self, text: str) -> str:
        """Strip the API key from anything that might be logged or raised.

        Long prefixes go too: providers echo keys half-masked ("Incorrect API
        key provided: sk-abc123...****"), and a 16-character prefix of a
        secret is still a piece of the secret. The floor keeps the scrub from
        eating ordinary text that merely starts the same way ("sk-").
        """
        key = self._api_key()
        if not key:
            return text
        scrubbed = text.replace(key, _REDACTED)
        # Every length, not just the first that matches: a body often echoes
        # the key more than once and at more than one truncation ("key=sk-abc…"
        # in the message, "attempted=sk-ab…" in the error object), and
        # stopping at the first leaves the rest in the log. Longest first, so
        # each occurrence is replaced at its full length.
        for length in range(len(key) - 1, _MIN_REDACTED_PREFIX_CHARS - 1, -1):
            prefix = key[:length]
            if prefix in scrubbed:
                scrubbed = scrubbed.replace(prefix, _REDACTED)
        return scrubbed
