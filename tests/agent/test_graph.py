"""Tests for the durable per-application LangGraph loop.

Every browser-touching dependency is faked; the storage, rate, gap-filling,
ATS-detection, and approval layers are the real ones, so what these tests
prove about persistence and safety is true of the shipped code rather than
of a mock. Nothing here launches a browser, opens a socket, or submits
anything.

The properties under test are the ones the whole design exists for:

* the staging nodes run in the documented order, and each one that can fail
  routes to a terminal node instead of aborting the process;
* an unknown ATS, a captcha, a login wall, an unavailable extension, a
  failed trigger, and a reached rate cap all end that one application and
  leave the queue behind it intact;
* the approval interrupt is durable: a checkpoint written by one runner is
  resumable by a different runner reading the same SQLite file;
* `AUTO_SUBMIT` is off by default, and even when it is on a protected
  question, an unanswered gap, or incomplete page coverage still stops at
  the gate;
* a decision cannot be aimed at another application's thread, an identical
  decision replays instead of resuming twice, and a conflicting one is
  refused;
* field provenance and model cost are persisted, and answer text stays out
  of both the database and the checkpoint unless `LOG_FIELD_VALUES` is set.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from app.agent.approval import (
    ApiApprovalGate,
    ApplicationThreadMismatch,
    ApprovalConflict,
    ApprovalRequest,
    CliApprovalGate,
    UnknownThreadError,
)
from app.agent.errors import (
    CaptchaEncountered,
    ExtensionNotFoundError,
    LoginWallEncountered,
    TriggerFailed,
)
from app.agent.gap_filler import GapFillItem, GapFillPlan, Resolution
from app.agent.graph import (
    AUTO_SUBMIT_GATE,
    STAGING_NODES,
    RunStatus,
    SkipKind,
    SubmitAuthorization,
    SubmitOutcome,
    ThreadNotAwaitingApproval,
    UnknownQueueItem,
    build_graph,
    checkpointer_for,
    sqlite_checkpointer,
    thread_id_for,
)
from app.agent.jobright_trigger import TriggerTier
from app.boards.base import ApplyResult, ApplyStatus, SkipReason
from app.boards.registry import adapter_for
from app.config import Settings
from app.storage.models import (
    ApplicationStatus,
    ApprovalDecision,
    Board,
    FieldSource,
    QueueState,
)
from tests.agent.support import (
    LISTING_URL,
    PLAIN_HTML,
    UNKNOWN_ATS_URL,
    Crash,
    FakeAdapter,
    FakePage,
    World,
    build_world,
    cover_letter_gap,
    make_field,
    snapshot,
    sponsorship_gap,
    world,
)

__all__ = ["world"]


def approve(application_id: int | None) -> ApprovalRequest:
    return ApprovalRequest(
        application_id=application_id or 0,
        decision=ApprovalDecision.APPROVED,
        actor="me@example.com",
        note="looks right",
    )


class TestNodeOrder:
    async def test_the_staging_path_runs_in_the_documented_order(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.visited == STAGING_NODES

    async def test_the_gate_and_terminal_nodes_follow_the_staging_path(
        self, world: World
    ) -> None:
        """The gate only counts as visited once a decision came back.

        The first pass through `approval_gate` raises the interrupt and
        writes no state, which is exactly why it is safe for that node to
        run twice.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            resumed = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                ),
            )

        assert resumed.visited == STAGING_NODES + ("approval_gate", "submit")

    async def test_the_documented_order_matches_the_design(self) -> None:
        assert STAGING_NODES == (
            "admit",
            "open_listing",
            "start_application",
            "detect_ats",
            "snapshot_fields",
            "trigger_autofill",
            "attribute_fields",
            "fill_gaps",
            "stage",
        )

    async def test_the_thread_is_derived_from_the_queue_item(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.thread_id == thread_id_for(queue_id)
        application = world.db.get_application_by_thread(result.thread_id)
        assert application is not None
        assert application.id == result.application_id

    async def test_an_unknown_queue_item_is_refused_before_anything_is_created(
        self, world: World
    ) -> None:
        async with world.runner() as runner:
            with pytest.raises(UnknownQueueItem):
                await runner.run_application(999)

        assert world.db.get_application_by_thread(thread_id_for(999)) is None


class TestDurableInterrupt:
    async def test_a_run_stops_at_the_approval_gate_by_default(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert world.submitter.calls == 0
        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.status is ApplicationStatus.AWAITING_APPROVAL

    async def test_the_interrupt_describes_what_is_being_approved(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.interrupt is not None
        assert result.interrupt["application_id"] == result.application_id
        assert result.interrupt["thread_id"] == result.thread_id
        assert result.interrupt["listing_url"] == LISTING_URL
        assert result.interrupt["ats"] == "greenhouse"
        assert result.interrupt["trigger_tier"] == 1

    async def test_a_second_runner_reading_the_same_file_sees_the_interrupt(
        self, world: World
    ) -> None:
        """Durability is the point: the checkpoint outlives the runner."""
        queue_id = world.enqueue()

        async with world.runner() as first:
            staged = await first.run_application(queue_id)

        async with world.runner() as second:
            pending = await second.pending_approval(staged.thread_id)

        assert pending is not None
        assert pending["application_id"] == staged.application_id

    async def test_a_second_runner_resumes_the_thread_to_completion(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as first:
            staged = await first.run_application(queue_id)

        async with world.runner() as second:
            resumed = await second.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                ),
            )

        assert resumed.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1

    async def test_a_crash_mid_run_leaves_the_completed_nodes_checkpointed(
        self, world: World
    ) -> None:
        """Checkpoints are written before the next node starts, not after it.

        `Crash` is a `BaseException`, so the contained-failure path
        deliberately does not catch it and the run dies where a real process
        would. What the next runner reads back is what that crash left
        behind.
        """
        queue_id = world.enqueue()
        world.trigger.error = Crash()

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)

        async with world.runner() as second:
            state = await second.graph.aget_state(
                {"configurable": {"thread_id": thread_id_for(queue_id)}}
            )

        assert state.values["ats"] == "greenhouse"
        assert tuple(state.values["visited"]) == STAGING_NODES[
            : STAGING_NODES.index("trigger_autofill")
        ]

    async def test_rerunning_an_interrupted_queue_item_does_not_restage_it(
        self, world: World
    ) -> None:
        """A restarted worker must resume, not click Apply a second time."""
        queue_id = world.enqueue()

        async with world.runner() as first:
            await first.run_application(queue_id)
        async with world.runner() as second:
            again = await second.run_application(queue_id)

        assert again.status is RunStatus.AWAITING_APPROVAL
        assert world.adapter.started == 1
        assert world.trigger.calls == 1

    async def test_rerunning_a_finished_queue_item_reports_its_outcome(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.REJECTED,
                    actor="me@example.com",
                ),
            )
            again = await runner.run_application(queue_id)

        assert again.status is RunStatus.REJECTED
        assert world.submitter.calls == 0


class TestApprovedAndRejectedRuns:
    async def test_an_approved_run_submits_and_records_the_outcome(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                    note="looks right",
                ),
            )

        assert result.status is RunStatus.SUBMITTED
        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.status is ApplicationStatus.SUBMITTED
        queue_item = world.db.get_queue_item(queue_id)
        assert queue_item is not None
        assert queue_item.state is QueueState.COMPLETED

    async def test_an_approved_run_persists_the_actor_note_and_timestamp(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                    note="looks right",
                ),
            )

        approval = world.db.get_approval(result.application_id or 0)
        assert approval is not None
        assert approval.actor == "me@example.com"
        assert approval.note == "looks right"
        assert result.actor == "me@example.com"
        assert result.decided_at == approval.timestamp.isoformat()

    async def test_a_rejected_run_never_submits(self, world: World) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.REJECTED,
                    actor="me@example.com",
                    note="wrong location",
                ),
            )

        assert result.status is RunStatus.REJECTED
        assert world.submitter.calls == 0
        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.status is ApplicationStatus.REJECTED
        # The outcome reason reaches logs; the reviewer's own words belong in
        # the approvals table, which is where `RunResult.note` reads them from.
        assert "wrong location" not in result.reason
        assert result.note == "wrong location"

    async def test_a_rejected_run_releases_the_page(self, world: World) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.REJECTED,
                    actor="me@example.com",
                ),
            )

        assert world.broker.released == [staged.thread_id]

    async def test_a_submission_that_fails_is_recorded_as_failed_not_submitted(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            submit_outcome=SubmitOutcome(
                submitted=False, reason="the submit control never appeared"
            ),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                ),
            )

        assert result.status is RunStatus.FAILED
        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.status is ApplicationStatus.FAILED

    async def test_an_approved_resume_without_a_staged_page_fails_loudly(
        self, world: World
    ) -> None:
        """A page lost to a restart must not be mistaken for a submission."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.broker.pages.clear()  # the worker restarted; the tab is gone
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                ),
            )

        assert result.status is RunStatus.FAILED
        assert world.submitter.calls == 0
        assert "page" in result.reason


class TestResumeValidation:
    async def test_a_forged_application_id_cannot_resume_another_thread(
        self, world: World
    ) -> None:
        mine = world.enqueue()
        theirs = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            my_run = await runner.run_application(mine)
            their_run = await runner.run_application(theirs)

            with pytest.raises(ApplicationThreadMismatch):
                await runner.resume_application(
                    their_run.thread_id,
                    ApprovalRequest(
                        application_id=my_run.application_id or 0,
                        decision=ApprovalDecision.APPROVED,
                        actor="attacker@example.com",
                    ),
                )

        assert world.submitter.calls == 0
        assert world.db.get_approval(my_run.application_id or 0) is None
        assert world.db.get_approval(their_run.application_id or 0) is None

    async def test_the_victim_thread_is_still_awaiting_approval_afterwards(
        self, world: World
    ) -> None:
        mine = world.enqueue()
        theirs = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            my_run = await runner.run_application(mine)
            their_run = await runner.run_application(theirs)
            with pytest.raises(ApplicationThreadMismatch):
                await runner.resume_application(
                    their_run.thread_id,
                    ApprovalRequest(
                        application_id=my_run.application_id or 0,
                        decision=ApprovalDecision.APPROVED,
                        actor="attacker@example.com",
                    ),
                )
            still_pending = await runner.pending_approval(their_run.thread_id)

        assert still_pending is not None

    async def test_a_forged_id_cannot_read_a_finished_threads_decision(
        self, world: World
    ) -> None:
        """The mismatch is caught before any approval record is read.

        Once the victim's thread has finished it is no longer interrupted,
        which is the branch that inspects the *recorded* approval to decide
        between a replay and a conflict. Without the identity check first,
        an attacker holding only their own application id would learn who
        approved the victim's application and when, from the conflict
        message, or be handed the victim's own run result.
        """
        mine = world.enqueue()
        theirs = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            my_run = await runner.run_application(mine)
            their_run = await runner.run_application(theirs)
            await runner.resume_application(
                their_run.thread_id,
                ApprovalRequest(
                    application_id=their_run.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="victim@example.com",
                    note="the victim's private reasoning",
                ),
            )

            with pytest.raises(ApplicationThreadMismatch) as caught:
                await runner.resume_application(
                    their_run.thread_id,
                    ApprovalRequest(
                        application_id=my_run.application_id or 0,
                        decision=ApprovalDecision.REJECTED,
                        actor="attacker@example.com",
                    ),
                )

        message = str(caught.value)
        assert "victim@example.com" not in message
        assert "the victim's private reasoning" not in message

    async def test_an_unknown_thread_is_refused(self, world: World) -> None:
        async with world.runner() as runner:
            with pytest.raises(UnknownThreadError):
                await runner.resume_application(
                    "application-404",
                    ApprovalRequest(
                        application_id=1,
                        decision=ApprovalDecision.APPROVED,
                        actor="me@example.com",
                    ),
                )

    async def test_a_thread_that_is_not_at_the_gate_cannot_be_resumed(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, adapter=FakeAdapter())
        queue_id = world.enqueue(url=UNKNOWN_ATS_URL)
        world.broker.template = FakePage(url=UNKNOWN_ATS_URL, html=PLAIN_HTML)

        async with world.runner() as runner:
            skipped = await runner.run_application(queue_id)
            with pytest.raises(ThreadNotAwaitingApproval):
                await runner.resume_application(
                    skipped.thread_id,
                    ApprovalRequest(
                        application_id=skipped.application_id or 0,
                        decision=ApprovalDecision.APPROVED,
                        actor="me@example.com",
                    ),
                )

        assert world.submitter.calls == 0

    async def test_an_identical_decision_replays_instead_of_resuming_twice(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = ApprovalRequest(
                application_id=staged.application_id or 0,
                decision=ApprovalDecision.APPROVED,
                actor="me@example.com",
            )
            first = await runner.resume_application(staged.thread_id, request)
            second = await runner.resume_application(staged.thread_id, request)

        assert first.status is RunStatus.SUBMITTED
        assert second.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1

    async def test_a_conflicting_decision_is_refused_after_the_first_resumed(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.APPROVED,
                    actor="me@example.com",
                ),
            )
            with pytest.raises(ApprovalConflict):
                await runner.resume_application(
                    staged.thread_id,
                    ApprovalRequest(
                        application_id=staged.application_id or 0,
                        decision=ApprovalDecision.REJECTED,
                        actor="me@example.com",
                    ),
                )

        application = world.db.get_application(staged.application_id or 0)
        assert application is not None
        assert application.status is ApplicationStatus.SUBMITTED

    async def test_the_graph_only_acts_on_the_persisted_decision(
        self, world: World
    ) -> None:
        """The resume payload is built from the stored approval record."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.REJECTED,
                    actor="me@example.com",
                    note="not this one",
                ),
            )

        approval = world.db.get_approval(staged.application_id or 0)
        assert approval is not None
        assert result.decision == approval.decision.value
        assert result.actor == approval.actor
        assert result.note == approval.note


class TestGatesResumeTheSameThread:
    async def test_the_api_gate_submits_through_the_runner(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            gate = ApiApprovalGate(world.service(), runner)
            result = await gate.decide_payload(
                staged.application_id or 0,
                {"decision": "approve", "actor": "api@example.com"},
            )

        assert result.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1

    async def test_the_cli_gate_rejects_through_the_same_runner(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            gate = CliApprovalGate(
                world.service(),
                runner,
                reader=iter(["reject", "not a fit"]).__next__,
                writer=lambda _text: None,
            )
            result = await gate.prompt(
                staged.application_id or 0, actor="cli@example.com"
            )

        assert result.status is RunStatus.REJECTED
        assert world.submitter.calls == 0

    async def test_a_gate_cannot_release_a_different_application(
        self, world: World
    ) -> None:
        first = world.enqueue()
        second = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            first_run = await runner.run_application(first)
            second_run = await runner.run_application(second)
            gate = ApiApprovalGate(world.service(), runner)
            await gate.decide_payload(
                first_run.application_id or 0,
                {
                    "decision": "approve",
                    "actor": "api@example.com",
                    "thread_id": second_run.thread_id,
                },
            )
            other = await runner.pending_approval(second_run.thread_id)

        assert other is not None
        assert world.submitter.calls == 1


class TestAutoSubmit:
    async def test_auto_submit_is_off_by_default(self) -> None:
        assert Settings(_env_file=None).auto_submit is False

    async def test_a_clean_form_auto_submits_only_when_explicitly_enabled(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, auto_submit=True)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1
        assert result.interrupt is None

    async def test_a_protected_question_interrupts_even_with_auto_submit_on(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            auto_submit=True,
            before=snapshot(sponsorship_gap()),
            after=snapshot(sponsorship_gap()),
            answers={
                "answers": [
                    {
                        "question": (
                            "Will you now or in the future require visa sponsorship?"
                        ),
                        "name": "sponsorship",
                        "value": "No",
                    }
                ]
            },
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert world.submitter.calls == 0
        assert "protected_question" in result.blocking_reasons

    async def test_an_unanswered_gap_interrupts_even_with_auto_submit_on(
        self, tmp_path: Path
    ) -> None:
        gap = make_field("phone", label="Phone number", required=True)
        world = build_world(
            tmp_path, auto_submit=True, before=snapshot(gap), after=snapshot(gap)
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert "unanswered_gap" in result.blocking_reasons

    async def test_incomplete_page_coverage_interrupts_even_with_auto_submit_on(
        self, tmp_path: Path
    ) -> None:
        """A page that was only partly scanned cannot be declared gap-free."""
        world = build_world(
            tmp_path,
            auto_submit=True,
            before=snapshot(
                make_field("name", label="Full name", required=True),
                skipped=(("https://ats.example/frame", "cross-origin"),),
            ),
            after=snapshot(
                make_field(
                    "name", label="Full name", required=True, filled=True, value="Ada"
                ),
                skipped=(("https://ats.example/frame", "cross-origin"),),
            ),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert "coverage_incomplete" in result.blocking_reasons

    async def test_an_answer_that_could_not_be_typed_interrupts(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            auto_submit=True,
            with_router=True,
            write_succeeds=False,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert "unwritten_answer" in result.blocking_reasons


class TestContainedFailures:
    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            (
                {"guard_error": CaptchaEncountered("recaptcha iframe")},
                SkipKind.CAPTCHA,
            ),
            (
                {"guard_error": LoginWallEncountered("sign-in form")},
                SkipKind.LOGIN_WALL,
            ),
            (
                {
                    "trigger_error": ExtensionNotFoundError(
                        Path("/profile"), "abc", "not installed"
                    )
                },
                SkipKind.EXTENSION_UNAVAILABLE,
            ),
            ({"trigger_error": TriggerFailed(())}, SkipKind.TRIGGER_FAILED),
        ],
    )
    async def test_a_typed_failure_skips_this_application(
        self, tmp_path: Path, kwargs: dict[str, Any], expected: SkipKind
    ) -> None:
        world = build_world(tmp_path, **kwargs)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == expected.value
        queue_item = world.db.get_queue_item(queue_id)
        assert queue_item is not None
        assert queue_item.state is QueueState.SKIPPED
        assert queue_item.error_reason == expected.value

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"guard_error": CaptchaEncountered("recaptcha iframe")},
            {"guard_error": LoginWallEncountered("sign-in form")},
            {"trigger_error": TriggerFailed(())},
        ],
    )
    async def test_a_typed_failure_does_not_abort_the_rest_of_the_queue(
        self, tmp_path: Path, kwargs: dict[str, Any]
    ) -> None:
        world = build_world(tmp_path, **kwargs)
        doomed = world.enqueue()
        later = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            first = await runner.run_application(doomed)
            world.guard.error = None
            world.trigger.error = None
            second = await runner.run_application(later)

        assert first.status is RunStatus.SKIPPED
        assert second.status is RunStatus.AWAITING_APPROVAL

    async def test_an_unknown_ats_is_skipped_before_the_extension_is_triggered(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        world.broker.template = FakePage(url=UNKNOWN_ATS_URL, html=PLAIN_HTML)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.UNKNOWN_ATS.value
        assert world.trigger.calls == 0
        assert result.visited[-1] == "skip"

    async def test_an_unknown_ats_leaves_the_next_item_runnable(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        world.broker.template = FakePage(url=UNKNOWN_ATS_URL, html=PLAIN_HTML)
        doomed = world.enqueue()
        later = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            first = await runner.run_application(doomed)
            world.broker.template = FakePage()
            world.broker.pages.clear()
            second = await runner.run_application(later)

        assert first.status is RunStatus.SKIPPED
        assert second.status is RunStatus.AWAITING_APPROVAL

    async def test_a_reached_rate_cap_skips_without_touching_the_browser(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, caps={"linkedin_daily_cap": 1})
        first_id = world.enqueue()
        second_id = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            await runner.run_application(first_id)
            result = await runner.run_application(second_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.RATE_CAP_REACHED.value
        assert world.adapter.opened == [LISTING_URL]

    async def test_a_capped_board_does_not_stop_another_board(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, caps={"linkedin_daily_cap": 0})
        linkedin = world.enqueue()
        jobright = world.enqueue(
            url="https://jobright.ai/jobs/1", board=Board.JOBRIGHT
        )

        async with world.runner() as runner:
            capped = await runner.run_application(linkedin)
            other = await runner.run_application(jobright)

        assert capped.status is RunStatus.SKIPPED
        assert other.status is RunStatus.AWAITING_APPROVAL

    async def test_an_unsupported_board_flow_is_skipped_with_its_own_reason(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            adapter=FakeAdapter(
                apply_result=ApplyResult(
                    ApplyStatus.SKIPPED, SkipReason.LINKEDIN_EASY_APPLY.value
                )
            ),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipReason.LINKEDIN_EASY_APPLY.value

    async def test_an_untrusted_listing_url_is_skipped_by_the_real_adapter(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, adapter_lookup=adapter_for)
        queue_id = world.enqueue(url="https://linkedin.com.evil.example/jobs/view/1/")

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.UNTRUSTED_LISTING_URL.value

    async def test_an_apply_control_that_failed_marks_the_item_failed(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            adapter=FakeAdapter(
                apply_result=ApplyResult(
                    ApplyStatus.FAILED, "no apply control was found on this listing"
                )
            ),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.FAILED
        queue_item = world.db.get_queue_item(queue_id)
        assert queue_item is not None
        assert queue_item.state is QueueState.FAILED

    async def test_an_unexpected_error_fails_that_item_without_raising(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path, adapter=FakeAdapter(start_error=RuntimeError("driver exploded"))
        )
        doomed = world.enqueue()
        later = world.enqueue(url="https://www.linkedin.com/jobs/view/2/")

        async with world.runner() as runner:
            first = await runner.run_application(doomed)
            world.adapter.start_error = None
            second = await runner.run_application(later)

        assert first.status is RunStatus.FAILED
        assert second.status is RunStatus.AWAITING_APPROVAL

    async def test_a_contained_failure_captures_a_screenshot_when_it_can(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, trigger_error=TriggerFailed(()))
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.screenshot_path is not None
        assert world.screenshots.captured

    async def test_a_contained_failure_releases_the_page(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, trigger_error=TriggerFailed(()))
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert world.broker.released == [result.thread_id]


class TestFieldProvenance:
    async def test_autofilled_fields_are_attributed_to_jobright(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        by_key = {row.stable_key: row for row in rows}
        assert by_key["name"].source is FieldSource.JOBRIGHT
        assert by_key["name"].filled is True
        assert by_key["email"].source is FieldSource.JOBRIGHT

    async def test_the_trigger_tier_is_recorded_with_the_attribution(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.trigger_tier == 1
        rows = world.db.get_application_fields(result.application_id or 0)
        assert rows[0].metadata["trigger_tier"] == TriggerTier.IN_PAGE.value

    async def test_a_model_drafted_answer_is_attributed_to_the_model(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        drafted = [row for row in rows if row.source is FieldSource.LLM]
        assert [row.stable_key for row in drafted] == ["cover_letter"]
        assert drafted[0].filled is True

    async def test_a_canonical_answer_is_attributed_to_the_applicant(
        self, tmp_path: Path
    ) -> None:
        gap = make_field("phone", name="phone", label="Phone number", required=True)
        world = build_world(
            tmp_path,
            before=snapshot(gap),
            after=snapshot(gap),
            answers={
                "answers": [
                    {
                        "question": "Phone number",
                        "name": "phone",
                        "value": "+1 555 0100",
                    }
                ]
            },
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        by_key = {row.stable_key: row for row in rows}
        assert by_key["phone"].source is FieldSource.USER
        assert by_key["phone"].filled is True

    async def test_an_unanswered_gap_is_recorded_as_still_needing_the_applicant(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            before=snapshot(sponsorship_gap()),
            after=snapshot(sponsorship_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        by_key = {row.stable_key: row for row in rows}
        assert by_key["sponsorship"].source is FieldSource.USER
        assert by_key["sponsorship"].filled is False
        assert by_key["sponsorship"].required is True

    async def test_field_values_are_not_stored_by_default(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        assert all(row.value is None for row in rows)

    async def test_field_values_are_stored_when_explicitly_enabled(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            log_field_values=True,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        drafted = [row for row in rows if row.source is FieldSource.LLM]
        assert drafted[0].value == "Because the work matters."

    async def test_the_interrupt_payload_carries_no_answer_text_by_default(
        self, tmp_path: Path
    ) -> None:
        """The payload is written to the checkpoint file, so it is storage too."""
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.interrupt is not None
        assert "Because the work matters." not in repr(result.interrupt)
        assert world.checkpoint_path.read_bytes().find(b"Because the work") == -1


class TestWhatTheWriterIsGiven:
    """A key is a digest; a control is found by the metadata behind it.

    The writer has to locate a live control on a page, which needs the
    frame, the form, the id, the name, the type, and the shadow path the key
    was derived from. Passing only the key would leave an implementation
    guessing, and a guess types somebody's answer into the wrong box.
    """

    async def test_the_writer_is_handed_the_scanned_control(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            await runner.run_application(queue_id)

        assert [field.key for field in world.writer.fields] == ["cover_letter"]
        written = world.writer.fields[0]
        assert written.frame_url == cover_letter_gap().frame_url
        assert written.control_id == "cover_letter"
        assert written.field_type == "textarea"

    async def test_the_key_the_answer_is_recorded_against_is_the_fields_own(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        rows = world.db.get_application_fields(result.application_id or 0)
        assert {row.stable_key for row in rows} == {field.key for field in world.writer.fields}

    async def test_an_answer_with_no_scanned_control_is_never_typed(
        self, tmp_path: Path
    ) -> None:
        """A plan item nothing was scanned for has no control to type into.

        It cannot happen through the real gap filler, which is handed the
        scanned gaps and keys its items by them. It is checked because the
        alternative — passing a fabricated field, or the key alone — is how
        an answer ends up in whichever control happened to look similar.
        """
        world = build_world(tmp_path, with_router=True)
        queue_id = world.enqueue()
        world.deps = replace(
            world.deps, gap_filler=_StrayPlanFiller(world.deps.gap_filler)
        )

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert world.writer.written == []
        assert "unwritten_answer" in result.blocking_reasons
        rows = world.db.get_application_fields(result.application_id or 0)
        stray = [row for row in rows if row.stable_key == "never-scanned"]
        assert [row.filled for row in stray] == [False]


class _StrayPlanFiller:
    """Answers a question nothing on the page was scanned for."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.log_payload = real.log_payload

    async def fill(
        self, gaps: Any, *, skipped_frames: Any = ()
    ) -> GapFillPlan:
        return GapFillPlan(
            items=(
                GapFillItem(
                    key="never-scanned",
                    label="Salary expectation",
                    name="salary",
                    field_type="text",
                    required=True,
                    free_text=False,
                    resolution=Resolution.CANONICAL,
                    reason="answered from the answers file",
                    answer="120000",
                    source=FieldSource.USER,
                ),
            ),
        )


class TestWhatAuthorizesASubmission:
    """The submitter is told why it is allowed to click, in typed data.

    "Only an approved application is submitted" was previously true only of
    the node order. Handing the submitter the recorded decision makes it a
    precondition the submitter itself can check, which is what stops a
    future caller — a retry path, a script, a test — from submitting an
    application nobody released.
    """

    async def test_an_approved_run_authorizes_with_the_recorded_decision(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        authorization = world.submitter.authorizations[-1]
        assert authorization.approved is True
        assert authorization.decision == ApprovalDecision.APPROVED.value
        assert authorization.application_id == staged.application_id
        assert authorization.thread_id == staged.thread_id
        assert authorization.decided_at

    async def test_an_auto_submitted_run_authorizes_through_the_gate(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path, auto_submit=True)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            await runner.run_application(queue_id)

        authorization = world.submitter.authorizations[-1]
        assert authorization.gate == AUTO_SUBMIT_GATE
        assert authorization.blocking_reasons == ()
        assert authorization.approved is True

    async def test_an_authorization_with_neither_refuses_itself(self) -> None:
        unreleased = SubmitAuthorization(
            application_id=7, thread_id="application-7", decision="", decided_at="",
            gate="",
        )

        assert unreleased.approved is False
        assert "no decision has been recorded" in unreleased.refusal()

    async def test_a_rejected_decision_refuses_itself(self) -> None:
        rejected = SubmitAuthorization(
            application_id=7,
            thread_id="application-7",
            decision=ApprovalDecision.REJECTED.value,
            decided_at="2026-08-19T00:00:00+00:00",
            gate="api",
        )

        assert rejected.approved is False
        assert "rejected" in rejected.refusal()

    async def test_auto_submit_with_something_blocking_refuses_itself(self) -> None:
        """The gate alone is not a release; blockers are why it is not."""
        blocked = SubmitAuthorization(
            application_id=7,
            thread_id="application-7",
            decision="",
            decided_at="",
            gate=AUTO_SUBMIT_GATE,
            blocking_reasons=("unanswered_gap",),
        )

        assert blocked.approved is False
        assert "unanswered_gap" in blocked.refusal()


class TestWhenThePageIsChecked:
    """Before every click, not once at the start.

    A challenge or a sign-in can appear at any moment, and the gap between
    opening a listing and submitting it includes a wait for a human. Each
    node that is about to touch the page looks again.
    """

    async def test_the_guard_runs_again_before_the_trigger_and_the_writes(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            await runner.run_application(queue_id)

        # open_listing, start_application, trigger_autofill, fill_gaps.
        assert world.guard.calls == 4

    async def test_the_guard_runs_again_before_the_last_click(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            staged_calls = world.guard.calls
            await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert world.guard.calls == staged_calls + 1
        assert world.submitter.calls == 1

    async def test_a_challenge_at_the_gate_stops_the_submission(
        self, world: World
    ) -> None:
        """Nothing is clicked underneath a captcha that appeared while waiting.

        Recorded as a failure rather than a skip, which is this node's
        existing rule and the right one here: the application was approved,
        so a person is waiting on it and has to be told it did not happen.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.guard.error = CaptchaEncountered("https://ats.example.com/apply")
            result = await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert world.submitter.calls == 0
        assert result.status is RunStatus.FAILED
        assert result.reason == SkipKind.CAPTCHA.value
        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.status is ApplicationStatus.FAILED


class TestCostAccounting:
    async def test_model_cost_is_persisted_on_the_application(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.model_cost == pytest.approx(0.25)
        assert result.model_cost == Decimal("0.25")

    async def test_a_run_with_no_model_call_records_zero_cost(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.model_cost == Decimal("0")
        application = world.db.get_application(result.application_id or 0)
        assert application is not None
        assert application.model_cost == pytest.approx(0.0)

    async def test_the_cost_survives_the_approval_interrupt(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        queue_id = world.enqueue()

        async with world.runner() as first:
            staged = await first.run_application(queue_id)
        async with world.runner() as second:
            resumed = await second.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.REJECTED,
                    actor="me@example.com",
                ),
            )

        assert resumed.model_cost == Decimal("0.25")


class TestResumePayloadIsCheckedByTheGraphItself:
    async def test_a_resume_naming_another_application_does_not_approve(
        self, world: World
    ) -> None:
        """Defence in depth, below the runner's own validation.

        A caller holding the compiled graph can send any `Command(resume=)`
        it likes. The gate node therefore re-checks that the payload names
        the application this thread is actually for.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            values = await runner.graph.ainvoke(
                Command(
                    resume={
                        "application_id": (staged.application_id or 0) + 1000,
                        "decision": "approved",
                        "actor": "attacker@example.com",
                        "note": None,
                        "decided_at": "2026-08-18T12:00:00+00:00",
                    }
                ),
                {"configurable": {"thread_id": staged.thread_id}},
            )

        assert values["outcome"] == RunStatus.FAILED.value
        assert world.submitter.calls == 0

    async def test_a_resume_that_is_not_a_mapping_does_not_approve(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            values = await runner.graph.ainvoke(
                Command(resume="approved"),
                {"configurable": {"thread_id": staged.thread_id}},
            )

        assert values["outcome"] == RunStatus.FAILED.value
        assert world.submitter.calls == 0

    async def test_a_decision_recorded_but_never_applied_still_resumes(
        self, world: World
    ) -> None:
        """Recovers from a crash between recording and resuming.

        The decision is already in the audit table, so the second attempt
        replays it rather than recording a new one — and must still drive
        the thread to completion instead of reporting a stale pending state.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = ApprovalRequest(
                application_id=staged.application_id or 0,
                decision=ApprovalDecision.APPROVED,
                actor="me@example.com",
            )
            world.service().decide(request)  # recorded; the worker then died

            result = await runner.resume_application(staged.thread_id, request)

        assert result.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1


class TestGraphShape:
    async def test_build_graph_returns_a_compiled_graph_with_the_checkpointer(
        self, world: World
    ) -> None:
        async with sqlite_checkpointer(world.checkpoint_path) as checkpointer:
            graph = build_graph(world.deps, checkpointer)

            assert graph.checkpointer is checkpointer

    async def test_every_staging_node_is_a_node_of_the_graph(
        self, world: World
    ) -> None:
        async with sqlite_checkpointer(world.checkpoint_path) as checkpointer:
            graph = build_graph(world.deps, checkpointer)
            nodes = set(graph.get_graph().nodes)

        assert set(STAGING_NODES) <= nodes
        assert {"approval_gate", "submit", "reject", "skip", "fail"} <= nodes

    async def test_the_checkpointer_creates_its_parent_directory(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "nested" / "deeper" / "checkpoints.sqlite"

        async with sqlite_checkpointer(path):
            pass

        assert path.exists()

    async def test_the_configured_checkpointer_sits_beside_the_queue_database(
        self, world: World
    ) -> None:
        async with checkpointer_for(world.settings):
            pass

        assert (world.settings.sqlite_path.parent / "checkpoints.sqlite").exists()
