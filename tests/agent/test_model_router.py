"""Tests for deterministic model routing, exact cost accounting, and secrecy.

Every request here is served by an injected `httpx.MockTransport`, so no test
opens a socket: the provider is optional at runtime and entirely absent at
test time. Three properties matter more than the happy path — the same
question always picks the same model (a run's cost and behaviour must be
reproducible), the cost is exact decimal arithmetic (float pennies compound
into a wrong bill), and the API key never reaches a message, a repr, or a
traceback.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any, Callable

import pytest

# The provider dependency is optional: an installation that never calls a
# model does not need it, so these tests skip rather than fail without it.
httpx = pytest.importorskip("httpx")

from app.agent.errors import (
    ModelResponseError,
    ModelUnavailable,
    ProtectedQuestionError,
)
from app.agent.model_router import (
    Complexity,
    ModelAnswer,
    ModelProfile,
    ModelRouter,
    ModelTier,
    TokenUsage,
    token_cost,
)
from app.config import Settings

API_KEY = "sk-test-do-not-log-me-0123456789"
QUESTION = "How many years of Python experience do you have with async frameworks?"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "routine_model_url": "https://routine.example.com/v1",
        "routine_model_name": "routine-model",
        "escalation_model_url": "https://escalation.example.com/v1",
        "escalation_model_name": "escalation-model",
        "openai_api_key": API_KEY,
        "routine_input_price": Decimal("0.15"),
        "routine_output_price": Decimal("0.60"),
        "escalation_input_price": Decimal("2.50"),
        "escalation_output_price": Decimal("10.00"),
    }
    values.update(overrides)
    return Settings(**values)


def completion_payload(
    text: str = "Eight years.",
    prompt_tokens: int = 1_000,
    completion_tokens: int = 20,
    finish_reason: str | None = "stop",
    **overrides: Any,
) -> dict[str, Any]:
    choice: dict[str, Any] = {
        "index": 0,
        "message": {"role": "assistant", "content": text},
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    payload: dict[str, Any] = {
        "id": "chatcmpl-1",
        "model": "routine-model",
        "choices": [choice],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    payload.update(overrides)
    return payload


class RecordingTransport(httpx.MockTransport):
    """Records every request it serves so a test can assert on the wire form."""

    def __init__(
        self,
        responder: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if responder is not None:
                return responder(request)
            # Echo the requested model, as a real provider does, so a default
            # response is never accidentally a substituted one.
            import json

            requested = json.loads(request.content.decode("utf-8")).get("model")
            return httpx.Response(200, json=completion_payload(model=requested))

        super().__init__(handler)

    @property
    def bodies(self) -> list[Any]:
        import json

        return [json.loads(request.content.decode("utf-8")) for request in self.requests]


def build_router(
    transport: RecordingTransport | None = None, **overrides: Any
) -> tuple[ModelRouter, RecordingTransport]:
    used = transport if transport is not None else RecordingTransport()
    return ModelRouter(make_settings(**overrides), transport=used), used


class TestDeterministicSelection:
    def test_complexity_maps_to_a_fixed_model(self) -> None:
        router, _ = build_router()

        routine = router.select(Complexity.ROUTINE)
        escalation = router.select(Complexity.ESCALATION)

        assert routine.tier is ModelTier.ROUTINE
        assert routine.name == "routine-model"
        assert routine.base_url == "https://routine.example.com/v1"
        assert escalation.tier is ModelTier.ESCALATION
        assert escalation.name == "escalation-model"
        assert escalation.base_url == "https://escalation.example.com/v1"

    def test_selection_never_varies_between_calls(self) -> None:
        router, _ = build_router()

        picks = {router.select(Complexity.ROUTINE) for _ in range(25)}

        assert len(picks) == 1
        assert isinstance(picks.pop(), ModelProfile)

    def test_selection_carries_the_configured_prices(self) -> None:
        router, _ = build_router()

        profile = router.select(Complexity.ESCALATION)

        assert profile.input_price_per_million == Decimal("2.50")
        assert profile.output_price_per_million == Decimal("10.00")

    def test_an_unknown_complexity_is_rejected(self) -> None:
        router, _ = build_router()

        with pytest.raises(ValueError):
            router.select("whatever")  # type: ignore[arg-type]

    async def test_each_complexity_calls_its_own_endpoint(self) -> None:
        router, transport = build_router()

        await router.complete(QUESTION, Complexity.ROUTINE)
        await router.complete(QUESTION, Complexity.ESCALATION)

        assert [str(request.url) for request in transport.requests] == [
            "https://routine.example.com/v1/chat/completions",
            "https://escalation.example.com/v1/chat/completions",
        ]
        assert [body["model"] for body in transport.bodies] == [
            "routine-model",
            "escalation-model",
        ]

    async def test_requests_ask_for_a_deterministic_completion(self) -> None:
        router, transport = build_router()

        await router.complete(QUESTION)

        body = transport.bodies[0]
        assert body["temperature"] == 0
        assert body["messages"][-1]["content"].endswith(QUESTION)
        assert body["messages"][-1]["role"] == "user"

    async def test_the_system_prompt_forbids_inventing_an_answer(self) -> None:
        router, transport = build_router()

        await router.complete(QUESTION)

        system = transport.bodies[0]["messages"][0]
        assert system["role"] == "system"
        assert "invent" in system["content"].lower()

    async def test_routine_is_the_default_complexity(self) -> None:
        router, transport = build_router()

        answer = await router.complete(QUESTION)

        assert answer.tier is ModelTier.ROUTINE
        assert transport.bodies[0]["model"] == "routine-model"


class TestCostAccounting:
    @pytest.mark.parametrize(
        "prompt_tokens,completion_tokens,expected",
        [
            (1_000, 0, Decimal("0.00015")),
            (0, 1_000, Decimal("0.0006")),
            (3, 0, Decimal("0.00000045")),
            (0, 0, Decimal("0")),
            (1_000_000, 1_000_000, Decimal("0.75")),
        ],
    )
    async def test_cost_is_exact_decimal_arithmetic(
        self, prompt_tokens: int, completion_tokens: int, expected: Decimal
    ) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200,
                json=completion_payload(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                ),
            )
        )
        router, _ = build_router(transport)

        answer = await router.complete(QUESTION)

        assert answer.cost == expected
        assert isinstance(answer.cost, Decimal)
        assert not isinstance(answer.cost, float)

    def test_costs_that_binary_floats_get_wrong_are_exact(self) -> None:
        usage = TokenUsage(input_tokens=1, output_tokens=1)
        profile = ModelProfile(
            tier=ModelTier.ROUTINE,
            name="m",
            base_url="https://example.test/v1",
            input_price_per_million=Decimal("0.1"),
            output_price_per_million=Decimal("0.2"),
        )

        cost = token_cost(profile, usage)
        as_float = Decimal(str((1 * 0.1 + 1 * 0.2) / 1_000_000))

        assert cost == Decimal("0.0000003")
        assert cost != as_float

    def test_many_small_costs_accumulate_without_drift(self) -> None:
        usage = TokenUsage(input_tokens=1, output_tokens=1)
        profile = ModelProfile(
            tier=ModelTier.ROUTINE,
            name="m",
            base_url="https://example.test/v1",
            input_price_per_million=Decimal("0.1"),
            output_price_per_million=Decimal("0.2"),
        )

        total = sum((token_cost(profile, usage) for _ in range(1_000)), Decimal(0))

        assert total == Decimal("0.0003")

    async def test_usage_is_reported_alongside_the_answer(self) -> None:
        router, _ = build_router()

        answer = await router.complete(QUESTION, Complexity.ESCALATION)

        assert isinstance(answer, ModelAnswer)
        assert answer.usage == TokenUsage(input_tokens=1_000, output_tokens=20)
        assert answer.model == "escalation-model"
        assert answer.text == "Eight years."
        assert answer.cost == Decimal("2.50") * 1_000 / 1_000_000 + Decimal(
            "10.00"
        ) * 20 / 1_000_000

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            {},
            {"prompt_tokens": 10},
            {"prompt_tokens": "many", "completion_tokens": 1},
            {"prompt_tokens": -1, "completion_tokens": 1},
        ],
    )
    async def test_unusable_token_usage_is_an_error_not_a_guess(
        self, usage: Any
    ) -> None:
        payload = completion_payload()
        if usage is None:
            payload.pop("usage")
        else:
            payload["usage"] = usage
        transport = RecordingTransport(lambda request: httpx.Response(200, json=payload))
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION)

    async def test_prices_parsed_from_the_environment_stay_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROUTINE_INPUT_PRICE", "0.15")
        monkeypatch.setenv("ROUTINE_OUTPUT_PRICE", "0.60")

        settings = Settings(_env_file=None)

        assert settings.routine_input_price == Decimal("0.15")
        assert str(settings.routine_input_price) == "0.15"
        assert isinstance(settings.routine_input_price, Decimal)


class TestSecrecy:
    async def test_the_api_key_is_sent_as_a_bearer_token(self) -> None:
        router, transport = build_router()

        await router.complete(QUESTION)

        assert transport.requests[0].headers["authorization"] == f"Bearer {API_KEY}"

    async def test_the_key_never_appears_in_a_repr(self) -> None:
        router, _ = build_router()

        answer = await router.complete(QUESTION)

        assert API_KEY not in repr(router)
        assert API_KEY not in str(router)
        assert API_KEY not in repr(answer)
        assert API_KEY not in repr(router.select(Complexity.ROUTINE))

    def test_the_key_never_appears_in_a_settings_repr(self) -> None:
        settings = make_settings()

        assert API_KEY not in repr(settings)
        assert API_KEY not in str(settings)
        assert API_KEY not in str(settings.model_dump())

    async def test_a_provider_error_echoing_the_key_is_redacted(self) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                401,
                text=f"Invalid credentials for Authorization: Bearer {API_KEY}",
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert API_KEY not in str(excinfo.value)
        assert API_KEY not in repr(excinfo.value)
        assert excinfo.value.status == 401

    async def test_a_transport_failure_mentioning_the_key_is_redacted(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed while sending Bearer {API_KEY}")

        transport = RecordingTransport(explode)
        router, _ = build_router(transport)

        with pytest.raises(ModelUnavailable) as excinfo:
            await router.complete(QUESTION)

        assert API_KEY not in str(excinfo.value)

    async def test_a_missing_key_refuses_before_any_request(self) -> None:
        router, transport = build_router(openai_api_key="")

        with pytest.raises(ModelUnavailable):
            await router.complete(QUESTION)

        assert transport.requests == []

    async def test_the_question_is_the_only_content_sent(self) -> None:
        router, transport = build_router()

        await router.complete(QUESTION)

        body = transport.bodies[0]
        assert set(body) <= {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "n",
        }
        assert len(body["messages"]) == 2


class TestProtectedQuestionsAreNeverSent:
    @pytest.mark.parametrize(
        "question",
        [
            "Will you now or in the future require visa sponsorship?",
            "Are you legally authorized to work in the United States?",
            "Please self-identify your race and ethnicity.",
            "What is your gender identity?",
            "Are you a protected veteran?",
            "What are your salary expectations for this role?",
            "What was your compensation at your most recent employer?",
            "What were your employment dates at Acme Corp?",
        ],
    )
    async def test_protected_questions_are_refused_at_the_boundary(
        self, question: str
    ) -> None:
        router, transport = build_router()

        with pytest.raises(ProtectedQuestionError):
            await router.complete(question)

        assert transport.requests == []

    async def test_an_ordinary_question_is_still_answered(self) -> None:
        router, transport = build_router()

        answer = await router.complete("Why do you want to work here?")

        assert answer.text == "Eight years."
        assert len(transport.requests) == 1


class TestFailureModes:
    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    async def test_error_statuses_are_reported(self, status: int) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(status, text="upstream said no")
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert excinfo.value.status == status

    @pytest.mark.parametrize(
        "payload",
        [
            {"choices": []},
            {"choices": [{"message": {}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ],
    )
    async def test_unusable_payloads_are_reported(self, payload: Any) -> None:
        transport = RecordingTransport(lambda request: httpx.Response(200, json=payload))
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION)

    async def test_a_non_json_body_is_reported(self) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(200, text="<html>gateway</html>")
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION)

    async def test_a_blank_answer_is_reported_rather_than_returned(self) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(200, json=completion_payload(text="   "))
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION)


class TestTruncationAndRefusal:
    """A partial answer and a polite refusal are both "no answer".

    Text that stopped at the token limit reads like prose and would be typed
    into a form mid-sentence, and "I'm sorry, I don't have enough information"
    reads like an answer to anything that only checks for the literal word
    UNKNOWN. Both have to come back as "ask the applicant".
    """

    @pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
    async def test_a_truncated_answer_is_an_error_not_a_partial_answer(
        self, finish_reason: str
    ) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200,
                json=completion_payload(
                    text="I would be delighted to join because",
                    finish_reason=finish_reason,
                ),
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert "truncat" in str(excinfo.value).lower()

    async def test_a_content_filtered_answer_is_declined(self) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200,
                json=completion_payload(
                    text="I cannot help with that.", finish_reason="content_filter"
                ),
            )
        )
        router, _ = build_router(transport)

        assert (await router.complete(QUESTION)).declined is True

    @pytest.mark.parametrize(
        "finish_reason",
        [
            "model_length",
            "truncated",
            "token_limit",
            "max_output_tokens",
            "tool_calls",
            "function_call",
            "error",
            "cancelled",
            "something_new_from_a_future_provider",
        ],
    )
    async def test_an_unrecognised_finish_reason_is_rejected(
        self, finish_reason: str
    ) -> None:
        """Unknown means unknown, and an unknown stop is not a known-good one.

        Providers spell truncation half a dozen ways — model_length,
        truncated, token_limit, max_output_tokens — and a denylist of the
        spellings we happened to think of accepts the next one as a complete
        answer. Only reasons known to mean "finished" are accepted.
        """
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200, json=completion_payload(finish_reason=finish_reason)
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert finish_reason in str(excinfo.value)

    @pytest.mark.parametrize(
        "finish_reason", ["stop", "end_turn", "eos", "stop_sequence", None, "", "  "]
    )
    async def test_a_completed_answer_is_returned(
        self, finish_reason: str | None
    ) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200, json=completion_payload(finish_reason=finish_reason)
            )
        )
        router, _ = build_router(transport)

        assert (await router.complete(QUESTION)).text == "Eight years."

    @pytest.mark.parametrize(
        "text",
        [
            "UNKNOWN",
            "unknown",
            "  UNKNOWN.  ",
            "UNKNOWN - I was not given enough information.",
            "I'm sorry, but I can't answer that.",
            "I am sorry, I cannot answer this question.",
            "I'm unable to determine that from the question alone.",
            "I don't have enough information to answer.",
            "As an AI language model, I do not know the applicant's details.",
            "I apologize, but there is not enough information here.",
            "Sorry, I cannot help with this request.",
            "There is insufficient information to answer this question.",
            "I cannot determine the answer from the question alone.",
        ],
    )
    def test_refusals_and_apologies_count_as_declined(self, text: str) -> None:
        answer = ModelAnswer(
            text=text,
            tier=ModelTier.ROUTINE,
            model="routine-model",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            cost=Decimal("0"),
        )

        assert answer.declined is True

    @pytest.mark.parametrize(
        "text",
        [
            "Eight years.",
            "I have known this team's work for years and cannot imagine a better fit.",
            "My unknown-unknowns list is short; I ship and then measure.",
            "I would describe my style as sorry-not-sorry about writing tests first.",
        ],
    )
    def test_a_real_answer_is_not_mistaken_for_a_refusal(self, text: str) -> None:
        answer = ModelAnswer(
            text=text,
            tier=ModelTier.ROUTINE,
            model="routine-model",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            cost=Decimal("0"),
        )

        assert answer.declined is False

    async def test_an_explicit_refusal_field_declines(self) -> None:
        """OpenAI reports a hard refusal in `message.refusal`, not in content."""
        payload = completion_payload()
        payload["choices"][0]["message"] = {
            "role": "assistant",
            "content": None,
            "refusal": "I can't help with that.",
        }
        transport = RecordingTransport(lambda request: httpx.Response(200, json=payload))
        router, _ = build_router(transport)

        assert (await router.complete(QUESTION)).declined is True


class TestErrorExcerptRedaction:
    """The key is removed from the whole body before any of it is quoted.

    Truncating first and redacting after leaves a key that straddles the cut
    as an unmatched prefix in the message — the exact case where redaction is
    most needed and least likely to be noticed.
    """

    @pytest.mark.parametrize("offset", [-8, -4, -1, 0, 1, 4])
    async def test_a_key_straddling_the_cut_never_survives(self, offset: int) -> None:
        from app.agent.model_router import _MAX_ERROR_DETAIL_CHARS

        padding = "x" * (_MAX_ERROR_DETAIL_CHARS + offset)
        body = f"{padding}{API_KEY} rejected"
        transport = RecordingTransport(lambda request: httpx.Response(401, text=body))
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        message = str(excinfo.value)
        assert API_KEY not in message
        for cut in range(8, len(API_KEY)):
            assert API_KEY[:cut] not in message

    async def test_a_non_json_body_hiding_a_key_is_redacted(self) -> None:
        from app.agent.model_router import _MAX_ERROR_DETAIL_CHARS

        body = "<html>" + "y" * (_MAX_ERROR_DETAIL_CHARS - 3) + API_KEY + "</html>"
        transport = RecordingTransport(lambda request: httpx.Response(200, text=body))
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        message = str(excinfo.value)
        for cut in range(8, len(API_KEY)):
            assert API_KEY[:cut] not in message

    async def test_a_provider_truncated_key_is_also_scrubbed(self) -> None:
        """Providers echo keys half-masked; a long prefix is still a secret."""
        transport = RecordingTransport(
            lambda request: httpx.Response(
                401, text=f"Incorrect API key provided: {API_KEY[:20]}****"
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert API_KEY[:20] not in str(excinfo.value)

    async def test_every_occurrence_of_a_key_prefix_is_scrubbed(self) -> None:
        """One redaction is not enough when a body repeats the key.

        Providers echo a rejected key in a message and again in an error
        object, and often at two different truncations. Stopping after the
        first match leaves the rest in the log.
        """
        body = (
            f"Incorrect API key provided: {API_KEY[:20]}****. "
            f"See docs. key={API_KEY[:20]} attempted={API_KEY[:16]} "
            f"full={API_KEY}"
        )
        transport = RecordingTransport(lambda request: httpx.Response(401, text=body))
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        message = str(excinfo.value)
        for cut in range(12, len(API_KEY) + 1):
            assert API_KEY[:cut] not in message

    async def test_a_short_coincidental_prefix_is_not_scrubbed(self) -> None:
        """Redaction must not eat the message; "sk-" is not the key."""
        transport = RecordingTransport(
            lambda request: httpx.Response(401, text="Incorrect API key: sk-***")
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert "Incorrect API key" in str(excinfo.value)

    async def test_the_excerpt_is_still_bounded(self) -> None:
        from app.agent.model_router import _MAX_ERROR_DETAIL_CHARS

        transport = RecordingTransport(
            lambda request: httpx.Response(500, text="z" * 10_000)
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        assert len(str(excinfo.value)) < _MAX_ERROR_DETAIL_CHARS + 200


class TestModelSubstitution:
    """The bill is computed from the configured model, so it has to be the
    one that answered.

    A gateway that quietly serves a different model than the one requested
    makes every cost in the ledger wrong — and the ledger is the only record
    of what this agent spent. A dated snapshot of the same model is the one
    substitution that is priced identically, so that is the only one taken.
    """

    async def test_a_substituted_model_is_rejected(self) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200, json=completion_payload(model="some-other-model")
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError) as excinfo:
            await router.complete(QUESTION)

        message = str(excinfo.value)
        assert "some-other-model" in message
        assert "routine-model" in message

    async def test_a_cheaper_substitution_does_not_pass_as_the_configured_model(
        self,
    ) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200, json=completion_payload(model="routine-model-mini-clone")
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION)

    async def test_a_different_model_of_the_same_length_is_not_a_snapshot(self) -> None:
        """The version suffix only counts on the configured name itself."""
        served = "x" * len("routine-model") + "-2026-08-01"
        transport = RecordingTransport(
            lambda request: httpx.Response(200, json=completion_payload(model=served))
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION)

    @pytest.mark.parametrize(
        "served",
        ["routine-model", "routine-model-2026-08-01", "routine-model:20260801"],
    )
    async def test_a_dated_snapshot_of_the_same_model_is_accepted(
        self, served: str
    ) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(200, json=completion_payload(model=served))
        )
        router, _ = build_router(transport)

        answer = await router.complete(QUESTION)

        assert answer.model == served
        assert answer.cost == token_cost(
            router.select(Complexity.ROUTINE),
            TokenUsage(input_tokens=1_000, output_tokens=20),
        )

    async def test_a_response_without_a_model_field_is_still_accepted(self) -> None:
        payload = completion_payload()
        payload.pop("model")
        transport = RecordingTransport(lambda request: httpx.Response(200, json=payload))
        router, _ = build_router(transport)

        answer = await router.complete(QUESTION)

        assert answer.model == "routine-model"

    async def test_the_escalation_model_is_checked_against_its_own_name(self) -> None:
        transport = RecordingTransport(
            lambda request: httpx.Response(
                200, json=completion_payload(model="routine-model")
            )
        )
        router, _ = build_router(transport)

        with pytest.raises(ModelResponseError):
            await router.complete(QUESTION, Complexity.ESCALATION)


class TestOptionalDependency:
    async def test_a_missing_httpx_is_a_typed_unavailability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, _ = build_router()
        monkeypatch.setitem(sys.modules, "httpx", None)

        with pytest.raises(ModelUnavailable):
            await ModelRouter(make_settings()).complete(QUESTION)

        assert router is not None

    def test_the_module_imports_without_a_live_provider(self) -> None:
        import app.agent.model_router as module

        assert hasattr(module, "ModelRouter")
