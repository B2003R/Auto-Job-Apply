"""Tests for what the graph does when something goes wrong twice.

The happy paths, the approval rules, and the typed skips live in
`test_graph.py`. This module is about the second time round: a decision
that arrives twice at once, a node re-executed after a crash, a worker
whose checkpoint file is gone, and a continuation that has lost the
browser artefacts the first attempt was holding.

The properties under test are the ones that decide whether a person's
application is submitted once, twice, or not at all:

* concurrent identical resumes submit once, concurrent conflicting ones
  are refused, and a submitted application is never walked back;
* a re-executed node restates its provenance rather than duplicating it,
  and the money a crashed attempt really spent is still counted;
* a lost checkpoint cannot restage an application that already finished,
  nor replay the approval that finished it;
* a continuation with a cold staging cache skips with a persisted reason
  instead of failing as a malfunction or submitting against nothing;
* the reviewer's identity and words stay in the approvals table and out
  of the checkpoint file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from app.agent.approval import ApprovalConflict, ApprovalRequest
from app.agent.errors import PageUnavailable
from app.agent.gap_filler import (
    GapFillItem,
    GapFillPlan,
    Resolution,
)
from app.agent.graph import (
    BlockingReason,
    ResumeInProgress,
    RunStatus,
    SkipKind,
    SubmitOutcome,
    ThreadNotAwaitingApproval,
    _auto_submittable,
    _blocking,
    thread_id_for,
)
from app.storage.models import (
    ApplicationStatus,
    ApprovalDecision,
    Board,
    FieldSource,
    QueueState,
)
from tests.agent.support import (
    LISTING_URL,
    Crash,
    FakeAdapter,
    FakeSubmitter,
    FakeTrigger,
    MovableClock,
    World,
    build_world,
    cover_letter_gap,
    make_field,
    snapshot,
    world,
)

__all__ = ["world"]

ACTOR = "reviewer@example.com"
NOTE = "checked the salary answer against the offer letter"
T0 = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(seconds=120)


def approve(application_id: int | None, *, actor: str = ACTOR) -> ApprovalRequest:
    return ApprovalRequest(
        application_id=application_id or 0,
        decision=ApprovalDecision.APPROVED,
        actor=actor,
        note=NOTE,
    )


class TestConcurrentResumes:
    async def test_two_identical_decisions_at_once_submit_once(
        self, world: World
    ) -> None:
        """The same reviewer double-clicking Approve.

        Both calls must succeed — refusing one would be a lie, the decision
        was recorded — and exactly one of them may reach the submitter.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = approve(staged.application_id)

            first, second = await asyncio.gather(
                runner.resume_application(staged.thread_id, request),
                runner.resume_application(staged.thread_id, request),
            )

        assert first.status is RunStatus.SUBMITTED
        assert second.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1

    async def test_the_loser_of_a_race_never_reaches_the_submitter(
        self, world: World
    ) -> None:
        """Eight simultaneous approvals are still one submission."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = approve(staged.application_id)

            results = await asyncio.gather(
                *(
                    runner.resume_application(staged.thread_id, request)
                    for _ in range(8)
                )
            )

        assert {result.status for result in results} == {RunStatus.SUBMITTED}
        assert world.submitter.calls == 1

    async def test_a_conflicting_decision_racing_an_approval_is_refused(
        self, world: World
    ) -> None:
        """One of the two must lose, and it must lose loudly.

        Serialising the pair is not enough on its own: whichever runs
        second has to notice that the recorded decision is not the one it
        was asked to apply, rather than quietly reporting the other
        reviewer's outcome as its own.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            approval = approve(staged.application_id)
            rejection = ApprovalRequest(
                application_id=staged.application_id or 0,
                decision=ApprovalDecision.REJECTED,
                actor="someone.else@example.com",
            )

            outcomes = await asyncio.gather(
                runner.resume_application(staged.thread_id, approval),
                runner.resume_application(staged.thread_id, rejection),
                return_exceptions=True,
            )

        refused = [item for item in outcomes if isinstance(item, ApprovalConflict)]
        settled = [item for item in outcomes if not isinstance(item, BaseException)]
        assert len(refused) == 1
        assert len(settled) == 1
        assert world.submitter.calls <= 1

    async def test_a_decision_that_already_submitted_returns_that_outcome(
        self, world: World
    ) -> None:
        """A retry arriving after the winner finished reads the outcome.

        This is the ordinary shape of a client retry: the first request
        submitted, the response was lost, the client asks again. It gets the
        submission it caused, not a second one.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = approve(staged.application_id)
            await runner.resume_application(staged.thread_id, request)

            again = await runner.resume_application(staged.thread_id, request)

        assert again.status is RunStatus.SUBMITTED
        assert world.submitter.calls == 1

    async def test_a_submitted_application_survives_a_lost_checkpoint(
        self, world: World
    ) -> None:
        """The database is the fallback when the checkpoint is not there.

        Without a terminal check against storage, a resume whose checkpoint
        has vanished sees no interrupt, replays the recorded approval, and
        the run reports whatever the empty state says — while the real
        application has already been submitted.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = approve(staged.application_id)
            await runner.resume_application(staged.thread_id, request)

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            replayed = await amnesiac.resume_application(staged.thread_id, request)

        assert replayed.status is RunStatus.SUBMITTED
        assert replayed.decision == ApprovalDecision.APPROVED.value
        assert world.submitter.calls == 1

    async def test_a_conflicting_decision_is_still_refused_without_a_checkpoint(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(staged.thread_id, approve(staged.application_id))

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            with pytest.raises(ApprovalConflict):
                await amnesiac.resume_application(
                    staged.thread_id,
                    ApprovalRequest(
                        application_id=staged.application_id or 0,
                        decision=ApprovalDecision.REJECTED,
                        actor=ACTOR,
                        note=NOTE,
                    ),
                )

        assert world.submitter.calls == 1

    async def test_a_failed_resume_hands_the_lease_back(self, world: World) -> None:
        """A lease outlives its holder only if the holder never releases it.

        The submitter blows up here, which is a genuine failure — but the
        thread must not be left owned by a worker that has given up, with
        every later attempt to decide it refused as in-progress.
        """
        queue_id = world.enqueue()

        async def explode(page: Any) -> SubmitOutcome:
            raise Crash("the worker died mid-submit")

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.submitter.submit = explode  # type: ignore[method-assign]

            with pytest.raises(Crash):
                await runner.resume_application(
                    staged.thread_id, approve(staged.application_id)
                )

        assert world.db.get_lease(staged.thread_id) is None
        record = world.db.get_application(staged.application_id or 0)
        assert record is not None
        assert record.status is ApplicationStatus.AWAITING_APPROVAL


class TestExecutionOwnership:
    """One lease decides who may run a thread, whichever entry point asks.

    Staging and resuming are the same question — "is another process
    driving this graph right now?" — so they take the same lease. Two
    mechanisms answering it separately is how a thread ends up applied for
    twice, and how an application killed mid-flight ends up owned forever.
    """

    async def test_concurrent_first_runs_apply_exactly_once(
        self, world: World
    ) -> None:
        """Four callers, one Apply click, one rate charge, one trigger.

        This is a queue runner started twice by an impatient operator, or a
        retry loop with no backoff. Every one of them believes it is
        starting the application.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            results = await asyncio.gather(
                *(runner.run_application(queue_id) for _ in range(4))
            )

        today = datetime.now(timezone.utc).date()
        assert world.adapter.started == 1
        assert world.trigger.calls == 1
        assert world.db.count_rate_events(Board.LINKEDIN, today) == 1
        assert all(result.awaiting_approval for result in results)
        assert world.submitter.calls == 0

    async def test_a_second_worker_will_not_start_an_application_someone_holds(
        self, world: World
    ) -> None:
        """The lease is taken here directly, as another process would.

        An in-process lock cannot see it, so without the database lease this
        run goes straight through to a second Apply click on a listing
        another worker is already applying to.
        """
        queue_id = world.enqueue()
        thread_id = thread_id_for(queue_id)
        world.db.acquire_lease(thread_id, "worker-elsewhere", ttl=TTL, now=T0)

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.IN_PROGRESS
        assert "worker-elsewhere" in (result.detail or "")
        assert world.adapter.started == 0
        assert world.adapter.opened == []

    async def test_a_second_worker_will_not_resume_a_thread_someone_holds(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            request = approve(staged.application_id)
            world.db.acquire_lease(staged.thread_id, "worker-elsewhere", ttl=TTL, now=T0)

            with pytest.raises(ResumeInProgress) as caught:
                await runner.resume_application(staged.thread_id, request)

        assert caught.value.thread_id == staged.thread_id
        assert "worker-elsewhere" in str(caught.value)
        assert world.submitter.calls == 0

    async def test_a_finished_run_leaves_no_lease_behind(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            assert world.db.get_lease(staged.thread_id) is None

            await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert world.db.get_lease(staged.thread_id) is None

    async def test_a_crashed_staging_run_hands_the_lease_back(
        self, world: World
    ) -> None:
        """A retry has to be possible immediately, not after an expiry."""
        queue_id = world.enqueue()

        async def die(page: Any) -> Any:
            raise Crash("the worker died while snapshotting")

        world.trigger.baseline = die  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)

            assert world.db.get_lease(thread_id_for(queue_id)) is None

            world.trigger.baseline = FakeTrigger(  # type: ignore[method-assign]
                world.trigger.before, world.trigger.after
            ).baseline
            result = await runner.run_application(queue_id)

        assert result.awaiting_approval

    async def test_a_lease_nobody_renewed_is_reclaimed(self, tmp_path: Path) -> None:
        """What stops a SIGKILL owning an application forever.

        Nothing releases a lease on the way out of a process that was killed
        outright. A successor has only the clock to go on, so a lease that
        has not been renewed within its term is treated as abandoned.
        """
        clock = MovableClock(T0)
        world = build_world(tmp_path, clock=clock)
        queue_id = world.enqueue()
        thread_id = thread_id_for(queue_id)
        world.db.acquire_lease(
            thread_id, "worker-that-was-killed", ttl=timedelta(seconds=30), now=T0
        )

        async with world.runner() as runner:
            blocked = await runner.run_application(queue_id)
            assert blocked.status is RunStatus.IN_PROGRESS

            clock.advance(timedelta(seconds=31))
            result = await runner.run_application(queue_id)

        assert result.awaiting_approval
        assert world.adapter.started == 1

    async def test_a_live_heartbeat_keeps_a_slow_worker_in_charge(
        self, tmp_path: Path
    ) -> None:
        """A submit that takes longer than the lease must not be taken over.

        Expiry alone cannot tell a wedged process from a careful one, so the
        holder renews while it works. Here the lease term elapses several
        times over during one submission, and the thread stays owned
        throughout.
        """
        world = build_world(tmp_path)
        queue_id = world.enqueue()
        submitting = asyncio.Event()
        finish = asyncio.Event()

        async def slow(page: Any) -> SubmitOutcome:
            submitting.set()
            await finish.wait()
            return SubmitOutcome(submitted=True, reason="eventually")

        async with world.runner(
            owner="patient-worker",
            lease_ttl=timedelta(milliseconds=200),
            heartbeat=timedelta(milliseconds=20),
        ) as runner:
            staged = await runner.run_application(queue_id)
            world.submitter.submit = slow  # type: ignore[method-assign]

            resuming = asyncio.create_task(
                runner.resume_application(
                    staged.thread_id, approve(staged.application_id)
                )
            )
            await asyncio.wait_for(submitting.wait(), timeout=5)
            await asyncio.sleep(0.6)  # three lease terms

            async with world.runner(owner="impatient-worker") as other:
                with pytest.raises(ResumeInProgress):
                    await other.resume_application(
                        staged.thread_id, approve(staged.application_id)
                    )
                assert (await other.run_application(queue_id)).status is (
                    RunStatus.IN_PROGRESS
                )

            finish.set()
            result = await asyncio.wait_for(resuming, timeout=5)

        assert result.status is RunStatus.SUBMITTED
        assert world.db.get_lease(staged.thread_id) is None


class TestMidFlightThreads:
    """A checkpoint with no interrupt but nodes still to run.

    Read carelessly this looks finished, because the gate is behind it. It
    is not: it is a thread whose owner died between the decision and the
    outcome, and the only safe reading of it is "somebody may still be
    working on this".
    """

    async def _staged_then_died_in_submit(self, world: World, queue_id: int) -> str:
        async def explode(page: Any) -> SubmitOutcome:
            raise Crash("the worker died between approving and submitting")

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.submitter.submit = explode  # type: ignore[method-assign]
            with pytest.raises(Crash):
                await runner.resume_application(
                    staged.thread_id, approve(staged.application_id)
                )
        return staged.thread_id

    async def test_a_resume_continues_it_and_returns_the_real_outcome(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        queue_id = world.enqueue()
        thread_id = await self._staged_then_died_in_submit(world, queue_id)

        application = world.db.get_application_by_thread(thread_id)
        assert application is not None

        world.submitter.submit = FakeSubmitter().submit  # type: ignore[method-assign]
        async with world.runner() as successor:
            result = await successor.resume_application(
                thread_id, approve(application.id)
            )

        assert result.status is RunStatus.SUBMITTED
        assert result.reason == "fake submission"

        record = world.db.get_application(application.id)
        assert record is not None
        assert record.status is ApplicationStatus.SUBMITTED

    async def test_a_resume_refuses_while_another_owner_holds_it(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        queue_id = world.enqueue()
        thread_id = await self._staged_then_died_in_submit(world, queue_id)
        application = world.db.get_application_by_thread(thread_id)
        assert application is not None
        world.db.acquire_lease(thread_id, "worker-elsewhere", ttl=TTL, now=T0)

        world.submitter.submit = FakeSubmitter().submit  # type: ignore[method-assign]
        async with world.runner() as successor:
            with pytest.raises(ResumeInProgress):
                await successor.resume_application(thread_id, approve(application.id))

        assert world.db.get_application(application.id).status is (  # type: ignore[union-attr]
            ApplicationStatus.AWAITING_APPROVAL
        )

    async def test_a_run_refuses_to_continue_it_while_another_owner_holds_it(
        self, tmp_path: Path
    ) -> None:
        """`run_application` must not drive a mid-flight thread either.

        Continuing here means running the submit node of an application
        another worker is already submitting.
        """
        world = build_world(tmp_path)
        queue_id = world.enqueue()
        thread_id = await self._staged_then_died_in_submit(world, queue_id)
        world.db.acquire_lease(thread_id, "worker-elsewhere", ttl=TTL, now=T0)

        submitter = FakeSubmitter()
        world.submitter.submit = submitter.submit  # type: ignore[method-assign]
        async with world.runner() as successor:
            result = await successor.run_application(queue_id)

        assert result.status is RunStatus.IN_PROGRESS
        assert submitter.calls == 0

    async def test_a_run_continues_it_once_the_lease_is_free(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        queue_id = world.enqueue()
        await self._staged_then_died_in_submit(world, queue_id)

        world.submitter.submit = FakeSubmitter().submit  # type: ignore[method-assign]
        async with world.runner() as successor:
            result = await successor.run_application(queue_id)

        assert result.status is RunStatus.SUBMITTED

    async def test_a_thread_that_never_reached_the_gate_cannot_be_resumed(
        self, tmp_path: Path
    ) -> None:
        """Mid-flight in *staging* is not a decision waiting to be applied."""
        world = build_world(tmp_path)
        queue_id = world.enqueue()

        async def die(page: Any, before: Any) -> Any:
            raise Crash("the worker died while triggering autofill")

        world.trigger.trigger = die  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)

            application = world.db.get_application_by_thread(thread_id_for(queue_id))
            assert application is not None

            with pytest.raises(ThreadNotAwaitingApproval):
                await runner.resume_application(
                    thread_id_for(queue_id), approve(application.id)
                )


class TestReportedStatus:
    """What a status endpoint or a CLI listing may honestly say."""

    async def test_a_thread_a_worker_is_running_is_not_offered_for_decision(
        self, world: World
    ) -> None:
        """An interrupt in the checkpoint is not the whole story.

        The holder may be a moment away from resolving that interrupt
        itself. Presenting it as an actionable approval invites a reviewer
        into a race they cannot see, and their decision would be refused as
        in-progress at best.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            assert await runner.pending_approval(staged.thread_id) is not None

            world.db.acquire_lease(staged.thread_id, "worker-elsewhere", ttl=TTL, now=T0)

            assert await runner.pending_approval(staged.thread_id) is None
            status = await runner.thread_status(staged.thread_id)

        assert status.executing is True
        assert status.stale is False
        assert status.awaiting_decision is False
        assert status.interrupt is not None
        assert status.lease is not None and status.lease.owner == "worker-elsewhere"

    async def test_a_stale_lease_does_not_hide_a_pending_decision(
        self, tmp_path: Path
    ) -> None:
        """The worker is gone; the decision it never took is still needed."""
        clock = MovableClock(T0)
        world = build_world(tmp_path, clock=clock)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.db.acquire_lease(
                staged.thread_id, "worker-that-was-killed",
                ttl=timedelta(seconds=30), now=clock.now,
            )
            assert await runner.pending_approval(staged.thread_id) is None

            clock.advance(timedelta(seconds=31))

            assert await runner.pending_approval(staged.thread_id) is not None
            status = await runner.thread_status(staged.thread_id)

        assert status.stale is True
        assert status.executing is False
        assert status.awaiting_decision is True

    async def test_a_finished_thread_reports_neither(self, world: World) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )
            status = await runner.thread_status(staged.thread_id)

        assert status.interrupt is None
        assert status.lease is None
        assert status.executing is False
        assert status.awaiting_decision is False

    async def test_an_unknown_thread_reports_nothing_rather_than_failing(
        self, world: World
    ) -> None:
        async with world.runner() as runner:
            status = await runner.thread_status("application-404")

        assert status.interrupt is None
        assert status.lease is None
        assert status.awaiting_decision is False


class TestFinishedRecordsAreNotRestaged:
    """Finding: a lost checkpoint must not reopen a finished application."""

    async def test_a_submitted_application_is_not_applied_for_again(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            replayed = await amnesiac.run_application(queue_id)

        assert replayed.status is RunStatus.SUBMITTED
        assert replayed.application_id == staged.application_id
        assert world.adapter.started == 1
        assert world.submitter.calls == 1

    async def test_a_rejected_application_is_not_applied_for_again(
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
                    actor=ACTOR,
                ),
            )

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            replayed = await amnesiac.run_application(queue_id)

        assert replayed.status is RunStatus.REJECTED
        assert replayed.decision == ApprovalDecision.REJECTED.value
        assert world.adapter.started == 1
        assert world.submitter.calls == 0

    async def test_a_skipped_queue_item_is_not_reopened(self, tmp_path: Path) -> None:
        world = build_world(tmp_path, adapter=None)
        queue_id = world.enqueue(url="https://careers.example.com/apply/1")
        world.db.update_queue_state(
            queue_id, QueueState.SKIPPED, SkipKind.UNKNOWN_ATS.value
        )

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.UNKNOWN_ATS.value
        assert world.adapter.started == 0

    async def test_a_failed_queue_item_reports_its_recorded_reason(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        queue_id = world.enqueue()
        world.db.update_queue_state(queue_id, QueueState.FAILED, "apply_failed")

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.FAILED
        assert result.reason == "apply_failed"
        assert world.adapter.opened == []

    async def test_a_decision_whose_checkpoint_is_gone_is_not_replayed_onto_a_new_form(
        self, world: World
    ) -> None:
        """The dangerous middle state: decided, not submitted, unresumable.

        Restaging would click Apply again and reach the gate with a freshly
        scanned form — and the recorded approval would then release *that*
        form, which nobody has looked at. The reviewer approved a specific
        set of answers, so the application is abandoned instead and the
        listing has to be queued again to get a decision of its own.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.service().decide(approve(staged.application_id))

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            result = await amnesiac.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.STALE_APPROVAL.value
        assert world.adapter.started == 1
        assert world.submitter.calls == 0

        item = world.db.get_queue_item(queue_id)
        assert item is not None
        assert item.error_reason == SkipKind.STALE_APPROVAL.value

    async def test_resuming_a_decision_whose_checkpoint_is_gone_abandons_it(
        self, world: World
    ) -> None:
        """The same state, reached from the gate instead of the queue.

        The decision was recorded and the worker died before applying it.
        Reporting "no outcome" would leave the application awaiting a
        decision it has already been given, forever.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            world.service().decide(approve(staged.application_id))

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            result = await amnesiac.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.STALE_APPROVAL.value
        assert world.submitter.calls == 0

        application = world.db.get_application_by_thread(staged.thread_id)
        assert application is not None
        assert application.status is ApplicationStatus.SKIPPED

    async def test_an_undecided_application_with_no_checkpoint_starts_again(
        self, world: World
    ) -> None:
        """Nothing was decided and nothing was submitted, so restaging is fine."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            await runner.run_application(queue_id)

        elsewhere = world.tmp_path / "fresh" / "checkpoints.sqlite"
        async with world.runner(elsewhere) as amnesiac:
            result = await amnesiac.run_application(queue_id)

        assert result.awaiting_approval
        assert world.adapter.started == 2

    async def test_a_pending_item_is_still_staged_normally(
        self, world: World
    ) -> None:
        """The guard must refuse finished work, not all work."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert world.adapter.started == 1

    async def test_an_interrupted_application_is_still_resumable(
        self, world: World
    ) -> None:
        """`awaiting_approval` is not terminal, and must not be treated as such."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            again = await runner.run_application(queue_id)

            assert again.awaiting_approval
            assert again.interrupt is not None

            decided = await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert decided.status is RunStatus.SUBMITTED
        assert world.adapter.started == 1


class TestReplayedNodesDoNotDuplicateWork:
    """Finding: a node body can run twice; its writes must survive that."""

    def _world(self, tmp_path: Path) -> World:
        return build_world(
            tmp_path,
            with_router=True,
            after=snapshot(
                make_field("name", label="Full name", required=True, filled=True, value="Ada"),
                cover_letter_gap(),
            ),
        )

    async def test_a_crash_while_writing_answers_leaves_one_row_per_field(
        self, tmp_path: Path
    ) -> None:
        """The classic duplicate: crash after the model call, then continue.

        `fill_gaps` calls the model, then types each answer. Dying part-way
        through the typing means LangGraph re-executes the whole node on the
        next invocation, so every provenance row it had already written is
        written a second time. With identity on the row rather than on the
        insert, the reviewer sees one form; without it, they see two.
        """
        world = self._world(tmp_path)
        queue_id = world.enqueue()
        attempts = {"count": 0}
        real_write = world.writer.write

        async def flaky(page: Any, key: str, value: str) -> bool:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise Crash("the worker died while typing the cover letter")
            return await real_write(page, key, value)

        world.writer.write = flaky  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)
            result = await runner.run_application(queue_id)

        assert result.awaiting_approval
        application_id = result.application_id or 0
        fields = world.db.get_application_fields(application_id)
        identities = [(field.stable_key, field.source) for field in fields]
        assert len(identities) == len(set(identities))
        assert ("cover_letter", FieldSource.LLM) in identities

    async def test_a_crash_after_a_model_call_still_counts_what_it_spent(
        self, tmp_path: Path
    ) -> None:
        """Two model passes cost two model passes.

        The crashed attempt's tokens were billed by the provider whether or
        not its answer was ever typed. Assigning the retry's cost over the
        top would report half the real spend, and the daily cost ceiling
        would be measured against a number that is quietly too small.
        """
        world = self._world(tmp_path)
        queue_id = world.enqueue()
        attempts = {"count": 0}
        real_write = world.writer.write

        async def flaky(page: Any, key: str, value: str) -> bool:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise Crash("the worker died while typing the cover letter")
            return await real_write(page, key, value)

        world.writer.write = flaky  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)
            result = await runner.run_application(queue_id)

        assert world.router is not None
        assert len(world.router.questions) == 2

        record = world.db.get_application(result.application_id or 0)
        assert record is not None
        assert Decimal(str(record.model_cost)) == Decimal("0.50")

    async def test_spend_is_recorded_before_the_answers_are_typed(
        self, tmp_path: Path
    ) -> None:
        """A crash between the model call and the first write still bills.

        The provider charged for the completion the moment it produced it.
        Recording the charge only after the writing loop would lose exactly
        the spend of every attempt that never got that far.
        """
        world = self._world(tmp_path)
        queue_id = world.enqueue()

        async def die(page: Any, key: str, value: str) -> bool:
            raise Crash("the worker died before typing anything")

        world.writer.write = die  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)

        application = world.db.get_application_by_thread(thread_id_for(queue_id))
        assert application is not None
        assert Decimal(str(application.model_cost)) == Decimal("0.25")


class TestColdStagingCache:
    """Finding: a continuation without its browser artefacts is a skip."""

    async def test_a_continuation_that_lost_its_cache_skips_rather_than_fails(
        self, tmp_path: Path
    ) -> None:
        """A crash in `fill_gaps`, then a fresh process.

        The new worker holds the checkpoint but none of the in-memory
        artefacts the crashed one had, so `fill_gaps` cannot re-run. That is
        a safe abandonment — nothing was submitted and the listing can be
        picked up again — not a malfunction, and the reason for it belongs
        on the queue row where an operator will look for it.
        """
        world = build_world(
            tmp_path,
            with_router=True,
            after=snapshot(
                make_field("name", label="Full name", required=True, filled=True, value="Ada"),
                cover_letter_gap(),
            ),
        )
        queue_id = world.enqueue()

        async def die(page: Any, key: str, value: str) -> bool:
            raise Crash("the worker died mid-fill")

        world.writer.write = die  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)

        # A different runner: same checkpoint file, empty staging cache.
        async with world.runner() as successor:
            result = await successor.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.STAGING_LOST.value
        assert world.submitter.calls == 0

        item = world.db.get_queue_item(queue_id)
        assert item is not None
        assert item.state is QueueState.SKIPPED
        assert item.error_reason == SkipKind.STAGING_LOST.value

        application = world.db.get_application_by_thread(thread_id_for(queue_id))
        assert application is not None
        assert application.status is ApplicationStatus.SKIPPED

    async def test_a_lost_page_mid_staging_skips_with_its_own_reason(
        self, world: World
    ) -> None:
        """A tab closed under a running application is not a crash either."""
        queue_id = world.enqueue()

        async def vanished(page: Any) -> Any:
            raise PageUnavailable(thread_id_for(queue_id))

        world.trigger.baseline = vanished  # type: ignore[method-assign]

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.SKIPPED
        assert result.reason == SkipKind.STAGED_PAGE_LOST.value

        item = world.db.get_queue_item(queue_id)
        assert item is not None
        assert item.error_reason == SkipKind.STAGED_PAGE_LOST.value

    async def test_a_skip_keeps_the_underlying_detail(self, world: World) -> None:
        """The kind routes; the detail explains.

        `reason` stays a stable enum value so a caller can branch on it, but
        the sentence that says which thread lost which page has to survive
        somewhere, or every skip of a given kind reads identically.
        """
        queue_id = world.enqueue()

        async def vanished(page: Any) -> Any:
            raise PageUnavailable("application-99")

        world.trigger.baseline = vanished  # type: ignore[method-assign]

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert "application-99" in (result.detail or "")


class TestSubmitFailuresAreExplained:
    async def test_a_refused_submission_records_why_on_the_queue_row(
        self, tmp_path: Path
    ) -> None:
        world = build_world(
            tmp_path,
            submit_outcome=SubmitOutcome(
                submitted=False, reason="the submit button never appeared"
            ),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert result.status is RunStatus.FAILED
        item = world.db.get_queue_item(queue_id)
        assert item is not None
        assert item.state is QueueState.FAILED
        assert item.error_reason == "the submit button never appeared"

    async def test_a_submission_that_lost_its_page_says_so(
        self, world: World
    ) -> None:
        """The reason must never be NULL: it is all an operator has."""
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            await world.broker.release(staged.thread_id)

            result = await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert result.status is RunStatus.FAILED
        assert world.submitter.calls == 0

        item = world.db.get_queue_item(queue_id)
        assert item is not None
        assert item.error_reason == SkipKind.STAGED_PAGE_LOST.value


class TestLangGraphSignalsAreNotFailures:
    async def test_an_interrupt_raised_inside_a_node_is_not_swallowed(
        self, tmp_path: Path
    ) -> None:
        """LangGraph's control flow travels as an exception.

        An `interrupt()` anywhere below a node — in a dependency that asks
        for a credential, say — raises `GraphInterrupt` on its way out.
        Catching it as a node failure would convert a request for a human
        into a permanently failed application that nobody was ever asked
        about, and the run would report a malfunction that never happened.
        """
        adapter = FakeAdapter(open_error=GraphInterrupt((Interrupt(value="who?"),)))
        world = build_world(tmp_path, adapter=adapter)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.status is RunStatus.AWAITING_APPROVAL
        assert result.interrupt == {"value": "who?"}

        item = world.db.get_queue_item(queue_id)
        assert item is not None
        assert item.state is QueueState.RUNNING


class TestTheCheckpointHoldsNoReviewer:
    async def test_the_reviewers_name_and_words_stay_in_the_approvals_table(
        self, world: World
    ) -> None:
        """The checkpoint is a machine's working state, not an audit log.

        Two files hold this decision. The approvals table is the one that is
        authoritative, access-controlled, and retained on purpose; the
        checkpoint is a resumption artefact that gets copied around with the
        working directory. The reviewer's identity and their private note
        belong only in the first.
        """
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id, approve(staged.application_id)
            )

        assert result.actor == ACTOR
        assert result.note == NOTE
        assert result.decided_at

        checkpoint = world.checkpoint_path.read_bytes()
        assert ACTOR.encode() not in checkpoint
        assert NOTE.encode() not in checkpoint

    async def test_a_rejection_keeps_its_note_out_of_the_checkpoint_too(
        self, world: World
    ) -> None:
        queue_id = world.enqueue()

        async with world.runner() as runner:
            staged = await runner.run_application(queue_id)
            result = await runner.resume_application(
                staged.thread_id,
                ApprovalRequest(
                    application_id=staged.application_id or 0,
                    decision=ApprovalDecision.REJECTED,
                    actor=ACTOR,
                    note=NOTE,
                ),
            )

        assert result.status is RunStatus.REJECTED
        assert result.note == NOTE
        assert NOTE.encode() not in world.checkpoint_path.read_bytes()


class TestAutoSubmitBlockers:
    async def test_a_field_only_the_applicant_can_operate_stops_the_gate(
        self, tmp_path: Path
    ) -> None:
        """`human_required`: a file upload is not something to guess at."""
        world = build_world(
            tmp_path,
            auto_submit=True,
            after=snapshot(
                make_field("name", label="Full name", required=True, filled=True, value="Ada"),
                make_field(
                    "resume",
                    label="Upload your resume",
                    field_type="file",
                    required=True,
                ),
            ),
        )
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.awaiting_approval
        assert BlockingReason.HUMAN_REQUIRED.value in result.blocking_reasons
        assert world.submitter.calls == 0

    async def test_a_page_that_never_settled_stops_the_gate(
        self, tmp_path: Path
    ) -> None:
        """`page_never_settled`: the form may still have been changing."""
        world = build_world(tmp_path, auto_submit=True, trigger_settled=False)
        queue_id = world.enqueue()

        async with world.runner() as runner:
            result = await runner.run_application(queue_id)

        assert result.awaiting_approval
        assert BlockingReason.PAGE_NEVER_SETTLED.value in result.blocking_reasons
        assert world.submitter.calls == 0

    def test_an_unrecorded_settle_is_treated_as_unsettled(self) -> None:
        """An older checkpoint has no `settled` key. Assume the worse one.

        Defaulting to "it settled" would let a state written by a version
        that never measured settling auto-submit a form that was still
        changing under it.
        """
        plan = GapFillPlan()

        assert BlockingReason.PAGE_NEVER_SETTLED.value in _blocking(plan, (), False)
        assert BlockingReason.PAGE_NEVER_SETTLED.value not in _blocking(plan, (), True)

    async def test_fill_gaps_itself_blocks_when_the_state_never_recorded_settling(
        self, tmp_path: Path
    ) -> None:
        """The call site, not just the function it calls.

        `_blocking` can default correctly while `fill_gaps` hands it the
        wrong thing, and the default is only reachable through a state
        written by a version that did not record settling — which no
        end-to-end run can produce. So the node is driven directly, with a
        state that has everything except `settled`.

        Its staging cache is left warm by a crash part-way through the
        writing loop, which is exactly the situation a resumed old
        checkpoint would arrive in.
        """
        world = build_world(
            tmp_path,
            with_router=True,
            after=snapshot(
                make_field("name", label="Full name", required=True, filled=True, value="Ada"),
                cover_letter_gap(),
            ),
        )
        queue_id = world.enqueue()
        attempts = {"count": 0}
        real_write = world.writer.write

        async def flaky(page: Any, key: str, value: str) -> bool:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise Crash("the worker died while typing")
            return await real_write(page, key, value)

        world.writer.write = flaky  # type: ignore[method-assign]

        async with world.runner() as runner:
            with pytest.raises(Crash):
                await runner.run_application(queue_id)

            application = world.db.get_application_by_thread(thread_id_for(queue_id))
            assert application is not None
            unsettled = {
                "queue_id": queue_id,
                "thread_id": thread_id_for(queue_id),
                "application_id": application.id,
                "listing_url": LISTING_URL,
                "board": Board.LINKEDIN.value,
                "visited": [],
            }

            update = await runner.graph.nodes["fill_gaps"].bound.ainvoke(unsettled)

        assert "settled" not in unsettled
        assert BlockingReason.PAGE_NEVER_SETTLED.value in update["blocking_reasons"]

    def test_an_answered_field_the_applicant_must_operate_still_blocks(
        self,
    ) -> None:
        """`human_required` blocks on its own, not only via `unanswered_gap`."""
        plan = GapFillPlan(
            items=(
                GapFillItem(
                    key="resume",
                    label="Upload your resume",
                    name="resume",
                    field_type="file",
                    required=True,
                    free_text=False,
                    resolution=Resolution.HUMAN,
                    reason="the applicant uploads this themselves",
                    answer="/tmp/resume.pdf",
                    source=FieldSource.USER,
                ),
            )
        )

        reasons = _blocking(plan, (), True)

        assert BlockingReason.HUMAN_REQUIRED.value in reasons
        assert BlockingReason.UNANSWERED_GAP.value not in reasons

    def test_a_state_with_no_recorded_blockers_never_auto_submits(self) -> None:
        """Absent is not the same as empty.

        A state that never reached `fill_gaps` has no `blocking_reasons` key
        at all. Reading that as "nothing is blocking" would auto-submit a
        form nobody had checked for protected questions.
        """
        assert _auto_submittable({}, auto_submit=True) is False
        assert _auto_submittable({"blocking_reasons": []}, auto_submit=True) is True
        assert (
            _auto_submittable(
                {"blocking_reasons": [BlockingReason.UNANSWERED_GAP.value]},
                auto_submit=True,
            )
            is False
        )
        assert _auto_submittable({"blocking_reasons": []}, auto_submit=False) is False
