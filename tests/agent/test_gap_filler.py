"""Tests for canonical-answer precedence and protected-category refusal.

The gap filler is the last thing standing between a language model and a form
submitted under someone's name, so most of these tests are about what it
refuses to do: never route a visa, EEO, compensation, or employment-date
question to a model; never invent one locally; never treat a model's
"UNKNOWN" as an answer; and never log a field's value unless the operator
explicitly asked for that.

The router is a double that records every question it is asked, so "was this
ever sent to a model?" is answered by evidence rather than by inspection.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agent.errors import AnswerBookError, ModelUnavailable
from app.agent.form_scanner import FormField
from app.agent.gap_filler import (
    AnswerBook,
    GapFillItem,
    GapFillPlan,
    GapFiller,
    ProtectedCategory,
    Resolution,
    protected_category_for,
)
from app.agent.model_router import Complexity, ModelAnswer, ModelTier, TokenUsage
from app.config import Settings
from app.storage.models import FieldSource

SECRET = "extremely-private-value"


def field(
    key: str = "k1",
    label: str = "Preferred name",
    *,
    name: str = "preferred_name",
    tag: str = "input",
    field_type: str = "text",
    required: bool = True,
    filled: bool = False,
    free_text: bool = True,
    visible: bool = True,
    disabled: bool = False,
    value: str | None = None,
) -> FormField:
    return FormField(
        key=key,
        frame_url="https://ats.example.com/apply",
        form="application",
        control_id=name,
        name=name,
        field_type=field_type,
        label=label,
        tag=tag,
        required=required,
        disabled=disabled,
        visible=visible,
        filled=filled,
        free_text=free_text,
        value_digest="digest",
        value=value,
    )


class RecordingRouter:
    """Answers everything it is asked, and remembers what that was."""

    def __init__(
        self,
        text: str = "A recorded model answer.",
        error: Exception | None = None,
        usage: TokenUsage | None = None,
    ) -> None:
        self.questions: list[tuple[str, Complexity]] = []
        self._text = text
        self._error = error
        self._usage = usage or TokenUsage(input_tokens=100, output_tokens=10)

    async def complete(
        self, question: str, complexity: Complexity = Complexity.ROUTINE
    ) -> ModelAnswer:
        self.questions.append((question, complexity))
        if self._error is not None:
            raise self._error
        return ModelAnswer(
            text=self._text,
            tier=ModelTier.ROUTINE,
            model="routine-model",
            usage=self._usage,
            cost=Decimal("0.000075"),
        )


def book(**entries: str) -> AnswerBook:
    return AnswerBook.from_mapping({"answers": dict(entries)})


def filler(answers: AnswerBook | None = None, **kwargs: Any) -> GapFiller:
    return GapFiller(answers if answers is not None else AnswerBook.empty(), **kwargs)


class TestProtectedClassification:
    @pytest.mark.parametrize(
        "label",
        [
            "Will you now or in the future require sponsorship for employment visa status?",
            "Do you require sponsorship?",
            "Are you legally authorized to work in the United States?",
            "Are you authorised to work in the UK?",
            "Do you have the right to work in Canada?",
            "Visa status",
            "Work authorization",
            "Do you hold a valid work permit?",
            "Are you a U.S. citizen?",
            "Citizenship",
            "What is your immigration status?",
            "Will you require H-1B sponsorship now or in the future?",
        ],
    )
    def test_visa_and_sponsorship_variants(self, label: str) -> None:
        assert protected_category_for(label) is ProtectedCategory.VISA_SPONSORSHIP

    @pytest.mark.parametrize(
        "label",
        [
            "Race/Ethnicity",
            "What is your ethnicity?",
            "Gender",
            "Gender identity",
            "Are you Hispanic or Latino?",
            "Protected veteran status",
            "Voluntary Self-Identification of Disability",
            "Do you have a disability?",
            "Sexual orientation",
            "Date of birth",
            "Marital status",
            "EEO survey (optional)",
            "National origin",
        ],
    )
    def test_eeo_and_demographic_variants(self, label: str) -> None:
        assert protected_category_for(label) is ProtectedCategory.EEO_DEMOGRAPHICS

    @pytest.mark.parametrize(
        "label",
        [
            "Desired salary",
            "Salary expectations",
            "What is your expected compensation?",
            "Current compensation",
            "What is your current base pay?",
            "Desired pay range",
            "Expected hourly rate",
            "Total comp expectations",
            "What are your wage requirements?",
        ],
    )
    def test_compensation_variants(self, label: str) -> None:
        assert protected_category_for(label) is ProtectedCategory.COMPENSATION

    @pytest.mark.parametrize(
        "label",
        [
            "Employment start date",
            "End date",
            "Dates of employment",
            "Employment dates",
            "From (MM/YYYY)",
            "To (MM/YYYY)",
            "When did you start at your current employer?",
            "Earliest start date",
        ],
    )
    def test_employment_date_variants(self, label: str) -> None:
        assert protected_category_for(label) is ProtectedCategory.EMPLOYMENT_DATES

    @pytest.mark.parametrize(
        "label",
        [
            "Cover letter",
            "Why do you want to work here?",
            "Preferred name",
            "LinkedIn profile URL",
            "Portfolio website",
            "Years of experience with Python",
            "How did you hear about us?",
            "Describe a race condition you debugged",
            "Which office location do you prefer?",
            "Tell us about a project you are proud of",
        ],
    )
    def test_ordinary_questions_are_not_protected(self, label: str) -> None:
        assert protected_category_for(label) is None

    def test_classification_is_case_and_punctuation_insensitive(self) -> None:
        assert (
            protected_category_for("DESIRED SALARY *")
            is ProtectedCategory.COMPENSATION
        )
        assert (
            protected_category_for("  visa   sponsorship?  ")
            is ProtectedCategory.VISA_SPONSORSHIP
        )

    def test_any_of_several_texts_can_trigger_the_classification(self) -> None:
        assert (
            protected_category_for("Question 4", "eeo_race_field")
            is ProtectedCategory.EEO_DEMOGRAPHICS
        )


class TestCanonicalPrecedence:
    async def test_a_canonical_answer_is_used_and_no_model_is_asked(self) -> None:
        router = RecordingRouter()
        plan = await filler(
            book(**{"Preferred name": "Alex Kim"}), router=router
        ).fill([field()])

        item = plan.items[0]
        assert item.resolution is Resolution.CANONICAL
        assert item.answer == "Alex Kim"
        assert item.source is FieldSource.USER
        assert router.questions == []

    def test_lookup_ignores_case_whitespace_and_required_markers(self) -> None:
        plan = filler(book(**{"preferred name": "Alex Kim"})).plan(
            [field(label="  Preferred   Name *  ")]
        )

        assert plan.items[0].answer == "Alex Kim"

    def test_lookup_matches_the_control_name(self) -> None:
        answers = AnswerBook.from_mapping(
            {
                "answers": [
                    {
                        "question": "Anything at all",
                        "value": "https://example.com/me",
                        "names": ["linkedin_url"],
                    }
                ]
            }
        )
        plan = filler(answers).plan(
            [field(label="Your professional profile", name="linkedin_url")]
        )

        assert plan.items[0].resolution is Resolution.CANONICAL
        assert plan.items[0].answer == "https://example.com/me"

    def test_lookup_matches_an_alias(self) -> None:
        answers = AnswerBook.from_mapping(
            {
                "answers": [
                    {
                        "question": "Preferred name",
                        "value": "Alex Kim",
                        "aliases": ["What should we call you?"],
                    }
                ]
            }
        )
        plan = filler(answers).plan([field(label="What should we call you?", name="nick")])

        assert plan.items[0].answer == "Alex Kim"

    async def test_canonical_beats_a_model_that_would_have_answered(self) -> None:
        router = RecordingRouter(text="A model answer nobody asked for")
        plan = await filler(book(**{"Cover letter": "My canonical letter"}), router=router).fill(
            [field(label="Cover letter", name="cover_letter", tag="textarea")]
        )

        assert plan.items[0].answer == "My canonical letter"
        assert plan.items[0].source is FieldSource.USER
        assert router.questions == []

    def test_a_missing_answers_file_is_an_empty_book_not_an_error(
        self, tmp_path: Path
    ) -> None:
        answers = AnswerBook.from_yaml(tmp_path / "absent.yaml")

        assert len(answers) == 0
        assert answers.lookup(field()) is None

    def test_from_settings_reads_the_configured_path(self, tmp_path: Path) -> None:
        path = tmp_path / "answers.yaml"
        path.write_text(
            'answers:\n  "Preferred name": "Alex Kim"\n', encoding="utf-8"
        )
        settings = Settings(_env_file=None, answers_path=path)

        answers = AnswerBook.load(settings)

        assert answers.lookup(field()) is not None

    @pytest.mark.parametrize(
        "content",
        [
            "answers: [",
            "answers: 12",
            "answers:\n  - value: no question here\n",
            "answers:\n  - question: Preferred name\n    value: ''\n",
            'answers:\n  "Preferred name": ""\n',
            "answers:\n  - question: Preferred name\n    value: Alex\n"
            "  - question: preferred NAME\n    value: Sam\n",
        ],
    )
    def test_an_unusable_answers_file_is_rejected_whole(
        self, tmp_path: Path, content: str
    ) -> None:
        path = tmp_path / "answers.yaml"
        path.write_text(content, encoding="utf-8")

        with pytest.raises(AnswerBookError):
            AnswerBook.from_yaml(path)

    def test_an_empty_file_is_an_empty_book(self, tmp_path: Path) -> None:
        path = tmp_path / "answers.yaml"
        path.write_text("", encoding="utf-8")

        assert len(AnswerBook.from_yaml(path)) == 0


class TestProtectedDenial:
    PROTECTED_FIELDS = [
        field(key="visa", label="Will you require visa sponsorship?", name="sponsorship"),
        field(key="eeo", label="Race/Ethnicity", name="eeo_race"),
        field(key="pay", label="Desired salary", name="desired_salary"),
        field(key="dates", label="Employment start date", name="start_date"),
    ]

    async def test_protected_questions_are_never_sent_to_a_model(self) -> None:
        router = RecordingRouter(text="Yes, definitely, absolutely")

        plan = await filler(router=router).fill(self.PROTECTED_FIELDS)

        assert router.questions == []
        assert [item.resolution for item in plan.items] == [Resolution.HUMAN] * 4
        assert all(item.answer is None for item in plan.items)
        assert all(item.question is None for item in plan.items)

    async def test_a_model_offering_a_value_does_not_fill_a_protected_field(self) -> None:
        router = RecordingRouter(text="$150,000")
        fields = [
            field(key="pay", label="Desired salary", name="desired_salary"),
            field(key="why", label="Why do you want to work here?", tag="textarea"),
        ]

        plan = await filler(router=router).fill(fields)

        by_key = {item.key: item for item in plan.items}
        assert by_key["pay"].answer is None
        assert by_key["pay"].source is None
        assert by_key["why"].answer == "$150,000"
        assert [question for question, _ in router.questions] == [
            question for question, _ in router.questions if "salary" not in question.lower()
        ]

    def test_protected_gaps_are_flagged_and_require_a_human(self) -> None:
        plan = filler().plan(self.PROTECTED_FIELDS)

        assert plan.requires_human is True
        assert plan.blocks_auto_submit is True
        assert {item.category for item in plan.protected} == {
            ProtectedCategory.VISA_SPONSORSHIP,
            ProtectedCategory.EEO_DEMOGRAPHICS,
            ProtectedCategory.COMPENSATION,
            ProtectedCategory.EMPLOYMENT_DATES,
        }
        assert plan.model_questions == ()

    def test_a_protected_field_answered_canonically_still_needs_review(self) -> None:
        answers = book(**{"Will you require visa sponsorship?": "No"})

        plan = filler(answers).plan([self.PROTECTED_FIELDS[0]])
        item = plan.items[0]

        assert item.resolution is Resolution.CANONICAL
        assert item.answer == "No"
        assert item.source is FieldSource.USER
        assert item.category is ProtectedCategory.VISA_SPONSORSHIP
        assert plan.blocks_auto_submit is True
        assert plan.requires_human is False

    def test_the_reason_names_the_category(self) -> None:
        plan = filler().plan([self.PROTECTED_FIELDS[2]])

        assert "compensation" in plan.items[0].reason.lower()

    def test_a_plan_without_protected_questions_does_not_block_auto_submit(self) -> None:
        plan = filler(book(**{"Preferred name": "Alex Kim"})).plan([field()])

        assert plan.blocks_auto_submit is False
        assert plan.requires_human is False


class TestModelRouting:
    async def test_an_ordinary_gap_is_routed_to_a_model(self) -> None:
        router = RecordingRouter(text="Because of the mission.")

        plan = await filler(router=router).fill(
            [field(key="why", label="Why do you want to work here?", tag="textarea")]
        )

        item = plan.items[0]
        assert item.resolution is Resolution.MODEL
        assert item.answer == "Because of the mission."
        assert item.source is FieldSource.LLM
        assert len(router.questions) == 1
        assert "Why do you want to work here?" in router.questions[0][0]

    @pytest.mark.parametrize(
        "tag,field_type,expected",
        [
            ("textarea", "textarea", Complexity.ESCALATION),
            ("div", "contenteditable", Complexity.ESCALATION),
            ("input", "text", Complexity.ROUTINE),
            ("input", "email", Complexity.ROUTINE),
        ],
    )
    async def test_complexity_selection_is_deterministic(
        self, tag: str, field_type: str, expected: Complexity
    ) -> None:
        router = RecordingRouter()

        for _ in range(3):
            await filler(router=router).fill(
                [field(key="q", label="Tell us about yourself", tag=tag, field_type=field_type)]
            )

        assert {complexity for _, complexity in router.questions} == {expected}

    async def test_model_cost_is_accumulated_as_a_decimal(self) -> None:
        router = RecordingRouter()
        fields = [
            field(key="a", label="Why do you want to work here?", tag="textarea"),
            field(key="b", label="What interests you about this team?", tag="textarea"),
        ]

        plan = await filler(router=router).fill(fields)

        assert plan.model_cost == Decimal("0.00015")
        assert isinstance(plan.model_cost, Decimal)

    async def test_a_declined_answer_becomes_a_human_gap(self) -> None:
        router = RecordingRouter(text="UNKNOWN")

        plan = await filler(router=router).fill(
            [field(key="q", label="How many widgets did you ship?", tag="textarea")]
        )

        item = plan.items[0]
        assert item.resolution is Resolution.HUMAN
        assert item.answer is None
        assert "declined" in item.reason.lower()

    async def test_an_unavailable_model_becomes_a_human_gap_not_an_exception(self) -> None:
        router = RecordingRouter(error=ModelUnavailable("no API key configured"))

        plan = await filler(router=router).fill(
            [field(key="q", label="Why this team?", tag="textarea")]
        )

        assert plan.items[0].resolution is Resolution.HUMAN
        assert "no API key" in plan.items[0].reason
        assert plan.requires_human is True

    async def test_without_a_router_every_unanswered_gap_is_human(self) -> None:
        plan = await filler().fill(
            [field(key="q", label="Why this team?", tag="textarea")]
        )

        assert plan.items[0].resolution is Resolution.HUMAN
        assert "model" in plan.items[0].reason.lower()

    def test_planning_alone_never_calls_the_model(self) -> None:
        router = RecordingRouter()

        plan = filler(router=router).plan(
            [field(key="q", label="Why this team?", tag="textarea")]
        )

        assert router.questions == []
        assert plan.items[0].resolution is Resolution.MODEL
        assert plan.items[0].answer is None
        assert plan.model_questions[0].key == "q"


class TestFieldSelection:
    def test_only_open_gaps_from_the_scanner_are_planned(self) -> None:
        fields = [
            field(key="filled", label="First name", filled=True),
            field(key="hidden", label="Referral code", visible=False),
            field(key="disabled", label="Employee id", disabled=True),
            field(key="optional-empty", label="Cover letter", required=False),
            field(key="required-empty", label="Preferred name"),
        ]

        plan = filler().plan(fields)

        assert [item.key for item in plan.items] == ["optional-empty", "required-empty"]

    def test_required_non_text_gaps_are_human_never_model(self) -> None:
        router = RecordingRouter()
        fields = [
            field(key="resume", label="Resume", field_type="file", free_text=False),
            field(key="pw", label="Create a password", field_type="password", free_text=False),
            field(key="agree", label="I agree to the terms", field_type="checkbox", free_text=False),
        ]

        plan = filler(router=router).plan(fields)

        assert [item.resolution for item in plan.items] == [Resolution.HUMAN] * 3
        assert plan.model_questions == ()

    def test_items_keep_the_scanner_field_identity(self) -> None:
        plan = filler().plan([field(key="stable-key-123", label="Cover letter")])

        item = plan.items[0]
        assert item.key == "stable-key-123"
        assert item.label == "Cover letter"
        assert item.name == "preferred_name"
        assert item.required is True

    def test_a_field_with_no_label_or_name_is_never_asked_of_a_model(self) -> None:
        router = RecordingRouter()

        plan = filler(router=router).plan(
            [field(key="mystery", label="", name="", tag="textarea")]
        )

        assert plan.items[0].resolution is Resolution.HUMAN
        assert "identify" in plan.items[0].reason.lower()
        assert plan.model_questions == ()

    def test_unscannable_frames_are_carried_into_the_plan(self) -> None:
        plan = filler().plan([field()], skipped_frames=("https://third-party.example/x",))

        assert plan.skipped_frames == ("https://third-party.example/x",)
        assert plan.coverage_complete is False

    def test_a_fully_scanned_page_reports_complete_coverage(self) -> None:
        assert filler().plan([field()]).coverage_complete is True

    def test_an_empty_field_list_yields_an_empty_plan(self) -> None:
        plan = filler().plan([])

        assert plan.items == ()
        assert plan.requires_human is False
        assert plan.blocks_auto_submit is False
        assert plan.model_cost == Decimal("0")


class TestValuePrivacy:
    def test_existing_field_values_never_enter_the_plan(self) -> None:
        plan = filler().plan(
            [field(key="q", label="Cover letter", tag="textarea", value=SECRET)]
        )

        assert SECRET not in repr(plan)
        assert plan.items[0].answer is None

    async def test_the_model_question_carries_only_metadata(self) -> None:
        router = RecordingRouter()

        await filler(router=router).fill(
            [field(key="q", label="Cover letter", tag="textarea", value=SECRET)]
        )

        assert SECRET not in router.questions[0][0]

    async def test_answers_are_redacted_from_reprs(self) -> None:
        router = RecordingRouter(text=SECRET)

        plan = await filler(router=router).fill(
            [field(key="q", label="Cover letter", tag="textarea")]
        )

        assert plan.items[0].answer == SECRET
        assert SECRET not in repr(plan.items[0])
        assert SECRET not in repr(plan)
        assert SECRET not in str(plan.items[0])

    async def test_the_log_payload_omits_answers_by_default(self) -> None:
        router = RecordingRouter(text=SECRET)

        plan = await filler(
            book(**{"Preferred name": "Alex Kim"}), router=router
        ).fill([field(), field(key="q", label="Cover letter", tag="textarea")])

        payload = plan.log_payload()

        assert SECRET not in repr(payload)
        assert "Alex Kim" not in repr(payload)
        assert all("answer" not in entry for entry in payload)
        assert {entry["key"] for entry in payload} == {"k1", "q"}
        assert all("resolution" in entry for entry in payload)

    async def test_answers_are_logged_only_when_explicitly_enabled(self) -> None:
        router = RecordingRouter(text=SECRET)
        instance = filler(router=router, log_field_values=True)
        gaps = [field(key="q", label="Cover letter", tag="textarea")]

        plan = await instance.fill(gaps)

        assert instance.log_payload(plan)[0]["answer"] == SECRET
        # The plan itself still withholds values unless asked directly.
        assert "answer" not in plan.log_payload()[0]

    async def test_a_filler_with_logging_off_never_emits_answers(self) -> None:
        router = RecordingRouter(text=SECRET)
        instance = filler(router=router)
        gaps = [field(key="q", label="Cover letter", tag="textarea")]

        plan = await instance.fill(gaps)

        assert SECRET not in repr(instance.log_payload(plan))

    def test_from_settings_defaults_to_withholding_values(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, answers_path=tmp_path / "absent.yaml")

        instance = GapFiller.from_settings(settings)

        assert instance.log_field_values is False


class TestPlanShape:
    def test_plan_and_items_are_immutable(self) -> None:
        plan = filler().plan([field()])

        assert isinstance(plan, GapFillPlan)
        assert isinstance(plan.items, tuple)
        assert isinstance(plan.items[0], GapFillItem)
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.items[0].answer = "nope"  # type: ignore[misc]

    def test_groups_partition_the_items(self) -> None:
        answers = book(**{"Preferred name": "Alex Kim"})
        fields = [
            field(),
            field(key="pay", label="Desired salary", name="desired_salary"),
            field(key="why", label="Why this team?", tag="textarea"),
        ]

        plan = filler(answers, router=RecordingRouter()).plan(fields)

        groups = [plan.canonical, plan.model_questions, plan.human_required]
        assert sum(len(group) for group in groups) == len(plan.items)
        assert {item.key for item in plan.canonical} == {"k1"}
        assert {item.key for item in plan.model_questions} == {"why"}
        assert {item.key for item in plan.human_required} == {"pay"}
