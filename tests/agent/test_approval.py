"""Tests for the human approval gate.

The approval gate is the last thing standing between a staged form and a
real submission made in someone's name, so the properties under test are
the ones that make a wrong submission impossible rather than unlikely:

* a decision is only accepted for an application that exists and is
  actually waiting for one;
* replaying the *identical* decision is a no-op that returns the first
  record, so a double-clicked button or a retried HTTP request cannot
  rewrite who decided, when, or why;
* a *different* decision is refused outright rather than overwriting the
  audit trail;
* actor, note, and timestamp are persisted, and the persisted record is
  what the graph is resumed with;
* an application id belonging to someone else's thread can never be used
  to resume that thread — the thread is always derived from the record,
  never accepted from the caller.

The CLI and API gates are two front ends over one typed decision, so they
are tested against the same service and asserted to produce equal requests
from equivalent input.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.agent.approval import (
    ApiApprovalGate,
    ApplicationNotAwaitingApproval,
    ApplicationThreadMismatch,
    ApprovalConflict,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalService,
    CliApprovalGate,
    InvalidApprovalRequest,
    UnknownApplicationError,
    UnknownThreadError,
    parse_decision,
)
from app.config import Settings
from app.storage.db import Database
from app.storage.models import ApplicationStatus, ApprovalDecision, Board

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class FrozenClock:
    """A movable UTC clock, so persisted timestamps are exact, not 'recent'."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


class RecordingResumer:
    """Stands in for the graph runner; records what it was asked to resume."""

    def __init__(self, result: object = "resumed") -> None:
        self.result = result
        self.calls: list[tuple[str, ApprovalRequest]] = []

    async def resume_application(
        self, thread_id: str, decision: ApprovalRequest
    ) -> object:
        self.calls.append((thread_id, decision))
        return self.result


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        sqlite_path=tmp_path / "approvals.db",
        artifacts_path=tmp_path / "artifacts",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings)
    database.initialize()
    return database


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def service(db: Database, clock: FrozenClock) -> ApprovalService:
    return ApprovalService(db, clock=clock)


def make_application(
    db: Database,
    *,
    thread_id: str = "application-1",
    status: ApplicationStatus = ApplicationStatus.AWAITING_APPROVAL,
    url: str = "https://www.linkedin.com/jobs/view/1/",
) -> int:
    queue_id = db.enqueue_job(listing_url=url, board=Board.LINKEDIN)
    return db.create_application(
        queue_id=queue_id,
        thread_id=thread_id,
        status=status,
    )


class TestApprovalRequestValidation:
    def test_an_actor_is_required(self) -> None:
        with pytest.raises(InvalidApprovalRequest):
            ApprovalRequest(
                application_id=1, decision=ApprovalDecision.APPROVED, actor="  "
            )

    def test_actor_is_stripped_so_padding_cannot_forge_a_second_identity(self) -> None:
        request = ApprovalRequest(
            application_id=1,
            decision=ApprovalDecision.APPROVED,
            actor="  reviewer@example.com \n",
        )

        assert request.actor == "reviewer@example.com"

    def test_a_positive_application_id_is_required(self) -> None:
        with pytest.raises(InvalidApprovalRequest):
            ApprovalRequest(
                application_id=0, decision=ApprovalDecision.APPROVED, actor="me"
            )

    def test_an_oversized_note_is_refused_rather_than_silently_truncated(self) -> None:
        with pytest.raises(InvalidApprovalRequest):
            ApprovalRequest(
                application_id=1,
                decision=ApprovalDecision.APPROVED,
                actor="me",
                note="x" * 10_000,
            )

    def test_an_oversized_actor_is_refused(self) -> None:
        with pytest.raises(InvalidApprovalRequest):
            ApprovalRequest(
                application_id=1,
                decision=ApprovalDecision.APPROVED,
                actor="a" * 10_000,
            )

    def test_an_empty_note_becomes_none_rather_than_an_empty_audit_entry(self) -> None:
        request = ApprovalRequest(
            application_id=1,
            decision=ApprovalDecision.APPROVED,
            actor="me",
            note="   ",
        )

        assert request.note is None

    def test_the_decision_must_be_the_typed_enum_not_a_lookalike_string(self) -> None:
        with pytest.raises(InvalidApprovalRequest):
            ApprovalRequest(
                application_id=1,
                decision="approved",  # type: ignore[arg-type]
                actor="me",
            )

    def test_repr_does_not_leak_the_note(self) -> None:
        request = ApprovalRequest(
            application_id=1,
            decision=ApprovalDecision.REJECTED,
            actor="me",
            note="salary was wrong",
        )

        assert "salary was wrong" not in repr(request)


class TestParseDecision:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("approve", ApprovalDecision.APPROVED),
            ("APPROVED", ApprovalDecision.APPROVED),
            (" approved ", ApprovalDecision.APPROVED),
            ("reject", ApprovalDecision.REJECTED),
            ("Rejected", ApprovalDecision.REJECTED),
        ],
    )
    def test_accepts_the_words_a_human_or_an_api_client_actually_sends(
        self, text: str, expected: ApprovalDecision
    ) -> None:
        assert parse_decision(text) is expected

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "yes", "no", "y", "n", "ok", "submit", "approve!", "true", "1"],
    )
    def test_refuses_anything_it_is_not_certain_about(self, text: str) -> None:
        with pytest.raises(InvalidApprovalRequest):
            parse_decision(text)

    def test_refuses_a_non_string(self) -> None:
        with pytest.raises(InvalidApprovalRequest):
            parse_decision(True)


class TestDecisionValidation:
    def test_an_unknown_application_is_refused(self, service: ApprovalService) -> None:
        with pytest.raises(UnknownApplicationError):
            service.decide(
                ApprovalRequest(
                    application_id=999,
                    decision=ApprovalDecision.APPROVED,
                    actor="me",
                )
            )

    def test_nothing_is_persisted_for_an_unknown_application(
        self, service: ApprovalService, db: Database
    ) -> None:
        with pytest.raises(UnknownApplicationError):
            service.decide(
                ApprovalRequest(
                    application_id=999,
                    decision=ApprovalDecision.APPROVED,
                    actor="me",
                )
            )

        assert db.get_approval(999) is None

    @pytest.mark.parametrize(
        "status",
        [
            ApplicationStatus.STAGING,
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.FAILED,
        ],
    )
    def test_an_application_not_waiting_for_a_decision_is_refused(
        self, service: ApprovalService, db: Database, status: ApplicationStatus
    ) -> None:
        application_id = make_application(db, status=status)

        with pytest.raises(ApplicationNotAwaitingApproval):
            service.decide(
                ApprovalRequest(
                    application_id=application_id,
                    decision=ApprovalDecision.APPROVED,
                    actor="me",
                )
            )

        assert db.get_approval(application_id) is None

    def test_an_unknown_thread_is_refused(self, service: ApprovalService) -> None:
        with pytest.raises(UnknownThreadError):
            service.application_for_thread("no-such-thread")


class TestPersistedDecision:
    def test_approval_persists_actor_note_and_timestamp(
        self, service: ApprovalService, db: Database, clock: FrozenClock
    ) -> None:
        application_id = make_application(db)

        outcome = service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="reviewer@example.com",
                note="looks right",
            )
        )

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.decision is ApprovalDecision.APPROVED
        assert stored.actor == "reviewer@example.com"
        assert stored.note == "looks right"
        assert stored.timestamp == clock.now
        assert outcome.record == stored
        assert outcome.replayed is False

    def test_rejection_persists_the_same_way(
        self, service: ApprovalService, db: Database, clock: FrozenClock
    ) -> None:
        application_id = make_application(db)

        service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.REJECTED,
                actor="reviewer@example.com",
                note="wrong location",
            )
        )

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.decision is ApprovalDecision.REJECTED
        assert stored.note == "wrong location"
        assert stored.timestamp == clock.now

    def test_a_decision_without_a_note_persists_null_not_an_empty_string(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)

        service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="me",
            )
        )

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.note is None

    def test_the_outcome_carries_the_thread_the_decision_belongs_to(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db, thread_id="application-42")

        outcome = service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="me",
            )
        )

        assert outcome.thread_id == "application-42"
        assert outcome.application_id == application_id


class TestIdempotencyAndConflict:
    def test_replaying_an_identical_decision_returns_the_first_record(
        self, service: ApprovalService, db: Database, clock: FrozenClock
    ) -> None:
        application_id = make_application(db)
        request = ApprovalRequest(
            application_id=application_id,
            decision=ApprovalDecision.APPROVED,
            actor="reviewer@example.com",
            note="looks right",
        )

        first = service.decide(request)
        clock.advance(hours=3)
        second = service.decide(request)

        assert first.replayed is False
        assert second.replayed is True
        assert second.record == first.record
        assert second.record.timestamp == NOW

    def test_a_replay_does_not_rewrite_the_persisted_timestamp(
        self, service: ApprovalService, db: Database, clock: FrozenClock
    ) -> None:
        application_id = make_application(db)
        request = ApprovalRequest(
            application_id=application_id,
            decision=ApprovalDecision.APPROVED,
            actor="me",
        )
        service.decide(request)

        clock.advance(days=1)
        service.decide(request)

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.timestamp == NOW

    def test_a_replay_is_accepted_even_once_the_application_has_moved_on(
        self, service: ApprovalService, db: Database
    ) -> None:
        """A retried request must not fail merely because the first one worked.

        By the time an HTTP client retries, the graph has usually already
        resumed and moved the application to `submitted`. Refusing the
        retry would report a failure for a decision that was in fact
        recorded and acted upon.
        """
        application_id = make_application(db)
        request = ApprovalRequest(
            application_id=application_id,
            decision=ApprovalDecision.APPROVED,
            actor="me",
        )
        service.decide(request)
        db.update_application(application_id, status=ApplicationStatus.SUBMITTED)

        outcome = service.decide(request)

        assert outcome.replayed is True

    def test_the_opposite_decision_is_refused_not_applied(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="me",
            )
        )

        with pytest.raises(ApprovalConflict):
            service.decide(
                ApprovalRequest(
                    application_id=application_id,
                    decision=ApprovalDecision.REJECTED,
                    actor="me",
                )
            )

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.decision is ApprovalDecision.APPROVED

    def test_the_same_decision_from_a_different_actor_is_a_conflict(
        self, service: ApprovalService, db: Database
    ) -> None:
        """Two people are not one idempotent request.

        Treating this as a replay would silently keep the first actor while
        telling the second one their decision was accepted, and treating it
        as an overwrite would erase who actually decided.
        """
        application_id = make_application(db)
        service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="first@example.com",
            )
        )

        with pytest.raises(ApprovalConflict):
            service.decide(
                ApprovalRequest(
                    application_id=application_id,
                    decision=ApprovalDecision.APPROVED,
                    actor="second@example.com",
                )
            )

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.actor == "first@example.com"

    def test_the_same_decision_with_a_different_note_is_a_conflict(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="me",
                note="original reasoning",
            )
        )

        with pytest.raises(ApprovalConflict):
            service.decide(
                ApprovalRequest(
                    application_id=application_id,
                    decision=ApprovalDecision.APPROVED,
                    actor="me",
                    note="rewritten reasoning",
                )
            )

        stored = db.get_approval(application_id)
        assert stored is not None
        assert stored.note == "original reasoning"

    def test_the_conflict_names_the_recorded_decision_without_quoting_the_note(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="me",
                note="private reasoning",
            )
        )

        with pytest.raises(ApprovalConflict) as caught:
            service.decide(
                ApprovalRequest(
                    application_id=application_id,
                    decision=ApprovalDecision.REJECTED,
                    actor="me",
                )
            )

        message = str(caught.value)
        assert "approved" in message
        assert "private reasoning" not in message


class TestStorageNeverOverwritesAnApproval:
    def test_a_second_write_leaves_the_first_record_intact(self, db: Database) -> None:
        """The audit trail is append-only at the storage layer, not by convention.

        `ApprovalService` refuses a conflicting decision, but the storage
        call underneath must not be capable of overwriting one either — a
        future caller that skipped the service would otherwise erase who
        approved a submission.
        """
        application_id = make_application(db)
        db.record_approval(
            application_id=application_id,
            decision=ApprovalDecision.APPROVED,
            actor="first@example.com",
            note="first",
            timestamp=NOW,
        )

        recorded, stored = db.try_record_approval(
            application_id=application_id,
            decision=ApprovalDecision.REJECTED,
            actor="second@example.com",
            note="second",
            timestamp=NOW + timedelta(days=1),
        )

        assert recorded is False
        assert stored.decision is ApprovalDecision.APPROVED
        assert stored.actor == "first@example.com"
        assert stored.note == "first"
        assert stored.timestamp == NOW


class TestForgedApplicationIds:
    def test_a_decision_cannot_be_pointed_at_another_applications_thread(
        self, service: ApprovalService, db: Database
    ) -> None:
        mine = make_application(db, thread_id="application-1")
        someone_elses = make_application(db, thread_id="application-2")

        with pytest.raises(ApplicationThreadMismatch):
            service.decide(
                ApprovalRequest(
                    application_id=mine,
                    decision=ApprovalDecision.APPROVED,
                    actor="me",
                ),
                thread_id="application-2",
            )

        assert db.get_approval(mine) is None
        assert db.get_approval(someone_elses) is None

    def test_the_mismatch_is_refused_before_anything_is_recorded(
        self, service: ApprovalService, db: Database
    ) -> None:
        mine = make_application(db, thread_id="application-1")
        make_application(db, thread_id="application-2")

        with pytest.raises(ApplicationThreadMismatch):
            service.decide(
                ApprovalRequest(
                    application_id=mine,
                    decision=ApprovalDecision.APPROVED,
                    actor="me",
                ),
                thread_id="application-2",
            )

        assert db.get_approval(mine) is None

    def test_the_matching_thread_is_accepted(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db, thread_id="application-7")

        outcome = service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="me",
            ),
            thread_id="application-7",
        )

        assert outcome.thread_id == "application-7"

    def test_a_thread_lookup_returns_only_its_own_application(
        self, service: ApprovalService, db: Database
    ) -> None:
        first = make_application(db, thread_id="application-1")
        second = make_application(db, thread_id="application-2")

        assert service.application_for_thread("application-1").id == first
        assert service.application_for_thread("application-2").id == second

    def test_two_applications_cannot_share_one_thread(self, db: Database) -> None:
        """Thread ids are the resume key, so they must identify one row.

        If two applications could share a thread, resolving a thread to an
        application would be ambiguous and a decision recorded against one
        could resume the other.
        """
        make_application(db, thread_id="application-1")

        with pytest.raises(sqlite3.IntegrityError):
            make_application(db, thread_id="application-1")


class TestGatesShareOneTypedDecision:
    async def test_the_api_gate_derives_the_thread_from_the_record(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db, thread_id="application-9")
        resumer = RecordingResumer()
        gate = ApiApprovalGate(service, resumer)

        await gate.decide_payload(
            application_id, {"decision": "approve", "actor": "api@example.com"}
        )

        assert [thread for thread, _ in resumer.calls] == ["application-9"]

    async def test_the_api_gate_has_no_way_to_name_a_thread(
        self, service: ApprovalService, db: Database
    ) -> None:
        """A caller-supplied thread id is ignored, not trusted.

        The payload comes straight from an HTTP body; if a `thread_id` in it
        could steer the resume, an attacker with any valid application id
        could release someone else's staged application.
        """
        application_id = make_application(db, thread_id="application-9")
        make_application(db, thread_id="application-victim")
        resumer = RecordingResumer()
        gate = ApiApprovalGate(service, resumer)

        await gate.decide_payload(
            application_id,
            {
                "decision": "approve",
                "actor": "api@example.com",
                "thread_id": "application-victim",
            },
        )

        assert [thread for thread, _ in resumer.calls] == ["application-9"]

    async def test_the_api_gate_refuses_an_unparseable_decision(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        resumer = RecordingResumer()
        gate = ApiApprovalGate(service, resumer)

        with pytest.raises(InvalidApprovalRequest):
            await gate.decide_payload(
                application_id, {"decision": "yes", "actor": "api@example.com"}
            )

        assert resumer.calls == []

    async def test_the_api_gate_refuses_a_missing_actor(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        resumer = RecordingResumer()
        gate = ApiApprovalGate(service, resumer)

        with pytest.raises(InvalidApprovalRequest):
            await gate.decide_payload(application_id, {"decision": "approve"})

        assert resumer.calls == []

    async def test_the_cli_gate_builds_the_same_request_as_the_api_gate(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db, thread_id="application-5")
        api_resumer = RecordingResumer()
        cli_resumer = RecordingResumer()
        written: list[str] = []

        await ApiApprovalGate(service, api_resumer).decide_payload(
            application_id,
            {"decision": "approve", "actor": "me@example.com", "note": "fine"},
        )
        await CliApprovalGate(
            service,
            cli_resumer,
            reader=iter(["approve", "fine"]).__next__,
            writer=written.append,
        ).prompt(application_id, actor="me@example.com")

        (api_thread, api_request) = api_resumer.calls[0]
        (cli_thread, cli_request) = cli_resumer.calls[0]
        assert api_request == cli_request
        assert api_thread == cli_thread == "application-5"

    async def test_the_cli_gate_shows_the_listing_before_asking(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(
            db, url="https://www.linkedin.com/jobs/view/1234/"
        )
        written: list[str] = []

        await CliApprovalGate(
            service,
            RecordingResumer(),
            reader=iter(["reject", ""]).__next__,
            writer=written.append,
        ).prompt(application_id, actor="me@example.com")

        rendered = "\n".join(written)
        assert "https://www.linkedin.com/jobs/view/1234/" in rendered
        assert str(application_id) in rendered

    async def test_the_cli_gate_reprompts_rather_than_guessing(
        self, service: ApprovalService, db: Database
    ) -> None:
        """An unrecognised answer is asked again, never read as approval."""
        application_id = make_application(db)
        resumer = RecordingResumer()

        await CliApprovalGate(
            service,
            resumer,
            reader=iter(["yes", "sure", "reject", ""]).__next__,
            writer=lambda _text: None,
        ).prompt(application_id, actor="me@example.com")

        (_thread, request) = resumer.calls[0]
        assert request.decision is ApprovalDecision.REJECTED

    async def test_the_cli_gate_gives_up_rather_than_looping_forever(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        resumer = RecordingResumer()

        with pytest.raises(InvalidApprovalRequest):
            await CliApprovalGate(
                service,
                resumer,
                reader=iter(["yes"] * 20).__next__,
                writer=lambda _text: None,
                max_attempts=3,
            ).prompt(application_id, actor="me@example.com")

        assert resumer.calls == []

    async def test_an_unknown_application_never_reaches_the_resumer(
        self, service: ApprovalService
    ) -> None:
        resumer = RecordingResumer()
        gate = ApiApprovalGate(service, resumer)

        with pytest.raises(UnknownApplicationError):
            await gate.decide_payload(
                4242, {"decision": "approve", "actor": "api@example.com"}
            )

        assert resumer.calls == []

    async def test_the_gate_returns_whatever_the_resume_produced(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        resumer = RecordingResumer(result={"outcome": "submitted"})
        gate = ApiApprovalGate(service, resumer)

        result = await gate.decide_payload(
            application_id, {"decision": "approve", "actor": "api@example.com"}
        )

        assert result == {"outcome": "submitted"}


class TestResumePayload:
    def test_the_payload_carries_the_persisted_decision_not_the_request(
        self, service: ApprovalService, db: Database, clock: FrozenClock
    ) -> None:
        """The graph resumes with what was stored, so the two cannot diverge."""
        application_id = make_application(db)
        first = service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.APPROVED,
                actor="first@example.com",
                note="first",
            )
        )

        payload = first.resume_payload()

        assert payload == {
            "application_id": application_id,
            "decision": "approved",
            "actor": "first@example.com",
            "note": "first",
            "decided_at": clock.now.isoformat(),
        }

    def test_the_payload_is_plain_json_types_so_a_checkpointer_can_store_it(
        self, service: ApprovalService, db: Database
    ) -> None:
        application_id = make_application(db)
        outcome = service.decide(
            ApprovalRequest(
                application_id=application_id,
                decision=ApprovalDecision.REJECTED,
                actor="me",
            )
        )

        payload: dict[str, Any] = outcome.resume_payload()

        assert all(
            isinstance(value, (str, int, type(None))) for value in payload.values()
        )

    def test_a_replayed_outcome_produces_the_same_payload(
        self, service: ApprovalService, db: Database, clock: FrozenClock
    ) -> None:
        application_id = make_application(db)
        request = ApprovalRequest(
            application_id=application_id,
            decision=ApprovalDecision.APPROVED,
            actor="me",
        )
        first = service.decide(request)
        clock.advance(hours=5)
        replay = service.decide(request)

        assert isinstance(replay, ApprovalOutcome)
        assert replay.resume_payload() == first.resume_payload()
