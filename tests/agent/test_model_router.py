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
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl-1",
        "model": "routine-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
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
            return httpx.Response(200, json=completion_payload())

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
