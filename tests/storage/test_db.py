"""Tests for SQLite persistence."""

from __future__ import annotations

import sqlite3
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


class TestClaimingTheFinalClick:
    """`try_claim_submit` is what makes one approval one submission.

    The execution lease stops two live workers pressing Submit together,
    but it expires by design so that a killed worker's thread can be picked
    up. That leaves the one case a lease cannot cover: a worker that pressed
    Submit and died before recording the outcome. Its successor sees a
    thread indistinguishable from one whose click never happened.

    So the attempt is claimed in the database before the press, and the
    claim has no expiry — unlike a lease, it is not a statement about who is
    working but about what has already been done to somebody's application.
    """

    def _application(self, db: Database) -> int:
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        return db.create_application(
            queue_id=queue_id,
            thread_id="thread-submit",
            status=ApplicationStatus.AWAITING_APPROVAL,
        )

    def test_the_first_claim_is_granted(self, db: Database) -> None:
        application_id = self._application(db)

        claimed, attempt = db.try_claim_submit(application_id, owner="worker-1")

        assert claimed is True
        assert attempt.application_id == application_id
        assert attempt.owner == "worker-1"
        assert attempt.attempted_at.tzinfo == timezone.utc

    def test_a_second_claim_is_refused_and_names_the_first(
        self, db: Database
    ) -> None:
        application_id = self._application(db)
        db.try_claim_submit(application_id, owner="worker-that-died")

        claimed, attempt = db.try_claim_submit(application_id, owner="successor")

        assert claimed is False
        assert attempt.owner == "worker-that-died"

    def test_a_claim_is_exclusive_across_connections(self, db: Database) -> None:
        """The successor is usually a different process, not a retry loop."""
        application_id = self._application(db)
        other = Database(db.settings)

        first, _ = other.try_claim_submit(application_id, owner="worker-1")
        second, held = db.try_claim_submit(application_id, owner="worker-2")

        assert first is True
        assert second is False
        assert held.owner == "worker-1"

    def test_the_claim_does_not_expire(self, db: Database) -> None:
        """A lease lapses so work can be recovered; this must not.

        Whatever happened to the process, the click still happened to the
        page, and time passing does not unhappen it.
        """
        application_id = self._application(db)
        db.try_claim_submit(
            application_id,
            owner="worker-1",
            now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

        claimed, _ = db.try_claim_submit(application_id, owner="successor")

        assert claimed is False

    def test_an_unclaimed_application_reports_no_attempt(self, db: Database) -> None:
        application_id = self._application(db)

        assert db.get_submit_attempt(application_id) is None

    def test_two_applications_are_claimed_independently(self, db: Database) -> None:
        first = self._application(db)
        queue_id = db.enqueue_job("https://example.com/jobs/2", Board.LINKEDIN)
        second = db.create_application(
            queue_id=queue_id,
            thread_id="thread-submit-2",
            status=ApplicationStatus.AWAITING_APPROVAL,
        )
        db.try_claim_submit(first, owner="worker-1")

        claimed, _ = db.try_claim_submit(second, owner="worker-1")

        assert claimed is True


class TestReturningWorkToTheQueue:
    """A killed worker must not strand its listing forever.

    `claim_next_pending` only ever claims a *pending* row, so a row left
    `running` by a process that died is invisible to every worker that
    follows. The application row and the execution lease both recover on
    their own; the queue row is the one piece that would sit there silently
    for good.

    Whether a given row is genuinely abandoned is a question about the
    execution lease, which is keyed by thread id — something this layer has
    no business deriving. So storage offers only the guarded move, and the
    worker decides which rows deserve it.
    """

    def test_a_running_row_is_returned_to_pending(self, db: Database) -> None:
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.claim_next_pending()

        assert db.requeue_running(queue_id, "worker_abandoned") is True

        stored = db.get_queue_item(queue_id)
        assert stored is not None
        assert stored.state is QueueState.PENDING
        assert stored.error_reason == "worker_abandoned"
        claimed = db.claim_next_pending()
        assert claimed is not None and claimed.id == queue_id

    @pytest.mark.parametrize(
        "state", [QueueState.PENDING, QueueState.COMPLETED, QueueState.FAILED]
    )
    def test_no_other_state_is_moved(self, db: Database, state: QueueState) -> None:
        """Terminal rows especially: requeueing one re-applies to the job.

        The guard is on `running` rather than on "not terminal", because a
        row that is already pending is one another worker may be about to
        claim, and rewriting it would put a stale reason on live work.
        """
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.update_queue_state(queue_id, state)

        assert db.requeue_running(queue_id, "worker_abandoned") is False

        stored = db.get_queue_item(queue_id)
        assert stored is not None
        assert stored.state is state

    def test_claiming_a_requeued_row_clears_the_stale_reason(
        self, db: Database
    ) -> None:
        """A reason on a `pending` row must describe why it came back, not
        why the run *before that* did.

        `error_reason` is read by an operator as "what went wrong last
        time"; once a fresh claim has genuinely started a new attempt, a
        stale `worker_abandoned` left over from the requeue would misreport
        the run that is now actually in flight — and if that new attempt
        succeeds outright, the row would keep blaming a crash that has
        nothing to do with its outcome.
        """
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.claim_next_pending()
        db.requeue_running(queue_id, "worker_abandoned")

        claimed = db.claim_next_pending()

        assert claimed is not None
        assert claimed.id == queue_id
        assert claimed.error_reason is None
        stored = db.get_queue_item(queue_id)
        assert stored is not None
        assert stored.error_reason is None

    def test_only_one_of_two_racing_sweeps_wins(self, db: Database) -> None:
        """Two workers starting at once must not both requeue one row."""
        queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
        db.claim_next_pending()
        other = Database(db.settings)

        outcomes = [
            other.requeue_running(queue_id, "worker_abandoned"),
            db.requeue_running(queue_id, "worker_abandoned"),
        ]

        assert outcomes == [True, False]


class TestReadingASnapshot:
    """A reader that wants one answer, not several taken at different times.

    An export walks applications, then each one's queue row, fields, and
    approval. Each of those was its own connection, so a worker submitting
    an application half way through produced a file describing an
    application that never existed in that combination at any instant.
    """

    def test_every_read_in_a_snapshot_uses_one_connection(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queue_id = db.enqueue_job("https://example.test/1", Board.LINKEDIN)
        db.create_application(
            queue_id=queue_id,
            thread_id="thread-1",
            status=ApplicationStatus.SUBMITTED,
        )
        opened: list[int] = []
        original = sqlite3.connect

        def counting(*args: object, **kwargs: object) -> sqlite3.Connection:
            opened.append(1)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(sqlite3, "connect", counting)

        with db.read_only_snapshot():
            db.list_applications()
            db.get_queue_item(queue_id)
            db.get_approval(1)

        assert len(opened) == 1

    def test_a_snapshot_cannot_write(self, db: Database) -> None:
        """Read-only at the connection, not by convention.

        An export has no business changing the log it is reading, and
        `mode=ro` also means a mistyped path is never turned into a new
        empty database.
        """
        with db.read_only_snapshot():
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.enqueue_job("https://example.test/2", Board.LINKEDIN)

    def test_a_snapshot_does_not_see_a_write_that_lands_during_it(
        self, db: Database, settings: Settings
    ) -> None:
        """The point of the transaction, stated as an outcome.

        The competing writer here is given a short busy timeout, so this
        also pins the cost: while a snapshot is open, a writer's commit
        waits. Exports are small and the worker retries for five seconds,
        but a torn export would be a wrong answer rather than a slow one.
        """
        db.enqueue_job("https://example.test/1", Board.LINKEDIN)

        with db.read_only_snapshot():
            before = db.list_queue_items()

            writer = sqlite3.connect(settings.sqlite_path, timeout=0.05)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute(
                        "INSERT INTO job_queue (listing_url, board, state, "
                        "created_at, updated_at) VALUES ('u', 'linkedin', "
                        "'pending', 'now', 'now')"
                    )
                    writer.commit()
            finally:
                writer.close()

            assert db.list_queue_items() == before

    def test_the_snapshot_is_released_when_the_block_ends(
        self, db: Database
    ) -> None:
        """A held read lock would block the worker indefinitely."""
        with db.read_only_snapshot():
            db.list_queue_items()

        db.enqueue_job("https://example.test/3", Board.LINKEDIN)
        assert len(db.list_queue_items()) == 1

    def test_the_snapshot_is_released_even_when_the_reader_raises(
        self, db: Database
    ) -> None:
        with pytest.raises(ValueError):
            with db.read_only_snapshot():
                db.list_queue_items()
                raise ValueError("the caller gave up")

        db.enqueue_job("https://example.test/4", Board.LINKEDIN)
        assert len(db.list_queue_items()) == 1

    def test_a_database_that_is_not_there_is_not_created(
        self, tmp_path: Path
    ) -> None:
        absent = tmp_path / "nowhere" / "jobs.db"
        database = Database(Settings(sqlite_path=absent, artifacts_path=tmp_path))

        with pytest.raises(sqlite3.OperationalError):
            with database.read_only_snapshot():
                pass

        assert not absent.exists()


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
