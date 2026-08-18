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
    model: str
    usage: TokenUsage
    cost: Decimal

    @property
    def declined(self) -> bool:
        """Whether the model said it could not answer.

        A declined answer is not a gap filled; the caller must route the
        field to a human rather than typing "UNKNOWN" into a form.
        """
        return self.text.strip().upper() == "UNKNOWN"


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
            raise ModelResponseError(
                response.status_code, self._redact(self._excerpt(response))
            )
        return self._parse(response, profile)

    def _parse(self, response: Any, profile: ModelProfile) -> ModelAnswer:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is unusable
            raise ModelResponseError(
                response.status_code,
                "response body was not JSON: " + self._redact(self._excerpt(response)),
            ) from None

        if not isinstance(body, Mapping):
            raise ModelResponseError(response.status_code, "response body was not an object")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelResponseError(response.status_code, "response carried no choices")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise ModelResponseError(
                response.status_code, "response carried an empty answer"
            )

        usage = self._parse_usage(body.get("usage"), response.status_code)
        return ModelAnswer(
            text=text,
            tier=profile.tier,
            model=profile.name,
            usage=usage,
            cost=token_cost(profile, usage),
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

    @staticmethod
    def _excerpt(response: Any) -> str:
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - undecodable bodies still need a message
            return "<unreadable response body>"
        return str(text)[:_MAX_ERROR_DETAIL_CHARS]

    def _redact(self, text: str) -> str:
        """Strip the API key from anything that might be logged or raised."""
        key = self._api_key()
        if key and key in text:
            return text.replace(key, _REDACTED)
        return text
