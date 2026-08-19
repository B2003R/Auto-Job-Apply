"""Tests for SQLite persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.storage.db import Database
from app.storage.logger import ApplicationLogger
from app.storage.models import (
    ApprovalDecision,
    ApplicationStatus,
    Board,
    FieldSource,
    QueueState,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        sqlite_path=tmp_path / "test.db",
        artifacts_path=tmp_path / "artifacts",
        log_field_values=False,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings)
    database.initialize()
    return database


def test_initialize_is_idempotent(settings: Settings) -> None:
    database = Database(settings)
    database.initialize()
    database.initialize()

    with database.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert tables >= {
        "job_queue",
        "applications",
        "application_fields",
        "approvals",
        "rate_events",
    }


def test_queue_transitions(db: Database) -> None:
    queue_id = db.enqueue_job(
        listing_url="https://example.com/jobs/1",
        board=Board.LINKEDIN,
    )
    item = db.get_queue_item(queue_id)
    assert item is not None
    assert item.state == QueueState.PENDING
    assert item.board == Board.LINKEDIN

    db.update_queue_state(queue_id, QueueState.RUNNING)
    running = db.get_queue_item(queue_id)
    assert running is not None
    assert running.state == QueueState.RUNNING

    db.update_queue_state(
        queue_id,
        QueueState.FAILED,
        error_reason="login wall",
    )
    failed = db.get_queue_item(queue_id)
    assert failed is not None
    assert failed.state == QueueState.FAILED
    assert failed.error_reason == "login wall"


def test_approval_persistence(db: Database) -> None:
    queue_id = db.enqueue_job(
        listing_url="https://example.com/jobs/2",
        board=Board.JOBRIGHT,
    )
    app_id = db.create_application(
        queue_id=queue_id,
        thread_id="thread-abc",
        status=ApplicationStatus.AWAITING_APPROVAL,
    )

    db.record_approval(
        application_id=app_id,
        decision=ApprovalDecision.APPROVED,
        actor="reviewer@example.com",
        note="Looks good",
    )

    approval = db.get_approval(app_id)
    assert approval is not None
    assert approval.decision == ApprovalDecision.APPROVED
    assert approval.actor == "reviewer@example.com"
    assert approval.note == "Looks good"
    assert approval.timestamp.tzinfo == timezone.utc


def test_rate_counts_use_utc_date(db: Database) -> None:
    board = Board.LINKEDIN
    today = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    yesterday = datetime(2026, 8, 17, 23, 59, tzinfo=timezone.utc)

    db.record_rate_event(board, "apply", yesterday)
    db.record_rate_event(board, "apply", today)
    db.record_rate_event(board, "apply", today)

    assert db.count_rate_events(board, today.date()) == 2
    assert db.count_rate_events(board, yesterday.date()) == 1


def test_rate_counts_utc_day_boundaries(db: Database) -> None:
    board = Board.LINKEDIN
    target_day = datetime(2026, 8, 18, tzinfo=timezone.utc).date()

    last_instant = datetime(2026, 8, 18, 23, 59, 59, 999999, tzinfo=timezone.utc)
    first_next_day = datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc)
    last_previous_day = datetime(2026, 8, 17, 23, 59, 59, 999999, tzinfo=timezone.utc)

    db.record_rate_event(board, "apply", last_instant)
    db.record_rate_event(board, "apply", first_next_day)
    db.record_rate_event(board, "apply", last_previous_day)

    assert db.count_rate_events(board, target_day) == 1


def test_rate_counts_handle_alternate_iso_timestamp_formats(db: Database) -> None:
    board = Board.LINKEDIN
    target_day = datetime(2026, 8, 18, tzinfo=timezone.utc).date()

    db.record_rate_event(
        board,
        "apply",
        datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    )

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO rate_events (board, action, timestamp) VALUES (?, ?, ?)",
            (board.value, "apply", "2026-08-18T15:30:00Z"),
        )
        conn.commit()

    assert db.count_rate_events(board, target_day) == 2


def test_field_values_redacted_when_logging_disabled(db: Database) -> None:
    settings = db.settings
    logger = ApplicationLogger(db, settings)

    queue_id = db.enqueue_job(
        listing_url="https://example.com/jobs/3",
        board=Board.WELLFOUND,
    )
    app_id = db.create_application(
        queue_id=queue_id,
        thread_id="thread-redact",
        status=ApplicationStatus.STAGING,
    )

    logger.log_field(
        application_id=app_id,
        stable_key="frame|input|email",
        source=FieldSource.LLM,
        required=True,
        filled=True,
        value="secret@example.com",
        metadata={"label": "Email"},
    )

    fields = db.get_application_fields(app_id)
    assert len(fields) == 1
    assert fields[0].value is None
    assert fields[0].filled is True
    assert fields[0].source == FieldSource.LLM


class TestClaimingWork:
    """`claim_next_pending` is how a worker picks up its next listing.

    It has to be a compare-and-set rather than a read followed by a write:
    two workers that both read the same pending row would both open the
    listing and click Apply in the applicant's name.
    """

    def test_nothing_pending_is_reported_as_nothing(self, db: Database) -> None:
        assert db.claim_next_pending() is None

    def test_a_claim_hands_back_the_oldest_pending_item_as_running(
        self, db: Database
    ) -> None:
        first = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.enqueue_job("https://example.com/jobs/2", Board.LINKEDIN)

        claimed = db.claim_next_pending()

        assert claimed is not None
        assert claimed.id == first
        assert claimed.state is QueueState.RUNNING
        stored = db.get_queue_item(first)
        assert stored is not None
        assert stored.state is QueueState.RUNNING

    def test_two_claims_never_return_the_same_item(self, db: Database) -> None:
        first = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        second = db.enqueue_job("https://example.com/jobs/2", Board.JOBRIGHT)

        claimed = [db.claim_next_pending(), db.claim_next_pending()]

        assert [item.id for item in claimed if item is not None] == [first, second]
        assert db.claim_next_pending() is None

    def test_claiming_skips_items_that_are_not_pending(self, db: Database) -> None:
        running = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.update_queue_state(running, QueueState.RUNNING)
        pending = db.enqueue_job("https://example.com/jobs/2", Board.LINKEDIN)

        claimed = db.claim_next_pending()

        assert claimed is not None
        assert claimed.id == pending

    def test_a_claim_is_exclusive_across_connections(self, db: Database) -> None:
        """A second process holding the row open must not also win it.

        Simulated by starting an immediate transaction on another
        connection and claiming from it, which is exactly the contention
        two worker processes produce.
        """
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        other = Database(db.settings)

        first = other.claim_next_pending()
        second = db.claim_next_pending()

        assert first is not None and first.id == queue_id
        assert second is None


class TestListingRecords:
    """Read-only listings, for the export CLI and the pending-approval view."""

    def test_queue_items_can_be_listed_by_state(self, db: Database) -> None:
        pending = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        done = db.enqueue_job("https://example.com/jobs/2", Board.LINKEDIN)
        db.update_queue_state(done, QueueState.COMPLETED)

        assert [item.id for item in db.list_queue_items()] == [pending, done]
        assert [
            item.id for item in db.list_queue_items(states=[QueueState.PENDING])
        ] == [pending]

    def test_applications_can_be_listed_by_status(self, db: Database) -> None:
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        staging = db.create_application(
            queue_id=queue_id, thread_id="t-1", status=ApplicationStatus.STAGING
        )
        waiting = db.create_application(
            queue_id=queue_id,
            thread_id="t-2",
            status=ApplicationStatus.AWAITING_APPROVAL,
        )

        assert [record.id for record in db.list_applications()] == [staging, waiting]
        assert [
            record.id
            for record in db.list_applications(
                statuses=[ApplicationStatus.AWAITING_APPROVAL]
            )
        ] == [waiting]

    def test_an_empty_status_filter_is_not_read_as_no_filter(
        self, db: Database
    ) -> None:
        """`statuses=[]` asks for nothing, and must not return everything.

        The distinction matters because a caller building a filter from
        user input can legitimately end up with an empty list, and reading
        that as "unfiltered" would dump every application into an export
        that was meant to be narrow.
        """
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.create_application(
            queue_id=queue_id, thread_id="t-1", status=ApplicationStatus.STAGING
        )

        assert db.list_applications(statuses=[]) == []
        assert db.list_queue_items(states=[]) == []


def test_field_values_persist_when_logging_enabled(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=tmp_path / "log-values.db",
        artifacts_path=tmp_path / "artifacts",
        log_field_values=True,
    )
    database = Database(settings)
    database.initialize()
    logger = ApplicationLogger(database, settings)

    queue_id = database.enqueue_job(
        listing_url="https://example.com/jobs/4",
        board=Board.HANDSHAKE,
    )
    app_id = database.create_application(
        queue_id=queue_id,
        thread_id="thread-log",
        status=ApplicationStatus.STAGING,
    )

    logger.log_field(
        application_id=app_id,
        stable_key="frame|input|name",
        source=FieldSource.JOBRIGHT,
        required=True,
        filled=True,
        value="Ada Lovelace",
    )

    fields = database.get_application_fields(app_id)
    assert len(fields) == 1
    assert fields[0].value == "Ada Lovelace"
