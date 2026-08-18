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
