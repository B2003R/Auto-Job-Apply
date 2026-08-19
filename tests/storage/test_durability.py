"""Tests for the storage guarantees the durable graph leans on.

A LangGraph node is not run-once. A crash, a retry, or a replayed
checkpoint can execute the same node body again against a database that
already holds the first attempt's writes. These tests pin down what the
storage layer promises under exactly that treatment:

* provenance is keyed by identity, so a replay updates a row rather than
  growing a second one;
* model cost is added, never assigned, so a second pass that really did
  spend money is counted rather than overwriting the first pass's total;
* a terminal application or queue row is not walked backwards by a late
  writer, so a submitted application cannot be restaged;
* a resume is claimed by exactly one caller, across processes;
* and a database written before those rules existed refuses to start with
  an error that says what to do about it, rather than an IntegrityError
  from whichever index happened to be created first.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.storage.db import Database, SchemaMigrationRequired
from app.storage.models import (
    ApplicationStatus,
    Board,
    FieldSource,
    QueueState,
)

#: Lease times are supplied by the caller rather than read from a clock, so
#: expiry is an ordinary argument and every test below is instant.
T0 = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(seconds=120)
STEP = timedelta(seconds=1)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        sqlite_path=tmp_path / "test.db",
        artifacts_path=tmp_path / "artifacts",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings)
    database.initialize()
    return database


def staged(db: Database, *, thread_id: str = "application-1") -> int:
    queue_id = db.enqueue_job("https://example.com/jobs/1", Board.LINKEDIN)
    return db.create_application(
        queue_id=queue_id,
        thread_id=thread_id,
        status=ApplicationStatus.STAGING,
    )


class TestProvenanceIsReplayIdempotent:
    def test_saving_the_same_field_twice_updates_one_row(self, db: Database) -> None:
        app_id = staged(db)

        first = db.save_application_field(
            application_id=app_id,
            stable_key="form|input|name",
            source=FieldSource.JOBRIGHT,
            required=True,
            filled=False,
            metadata={"attempt": 1},
        )
        second = db.save_application_field(
            application_id=app_id,
            stable_key="form|input|name",
            source=FieldSource.JOBRIGHT,
            required=True,
            filled=True,
            metadata={"attempt": 2},
        )

        assert first == second
        fields = db.get_application_fields(app_id)
        assert len(fields) == 1
        assert fields[0].filled is True
        assert fields[0].metadata == {"attempt": 2}

    def test_one_key_still_records_each_distinct_source(self, db: Database) -> None:
        """Identity is the triple, not the key.

        A field the extension filled and a human later corrected are two
        genuine provenance facts about one control; collapsing them would
        lose the audit trail the whole table exists for.
        """
        app_id = staged(db)

        db.save_application_field(
            application_id=app_id,
            stable_key="form|textarea|cover_letter",
            source=FieldSource.JOBRIGHT,
            required=True,
            filled=True,
        )
        db.save_application_field(
            application_id=app_id,
            stable_key="form|textarea|cover_letter",
            source=FieldSource.USER,
            required=True,
            filled=True,
        )

        sources = {field.source for field in db.get_application_fields(app_id)}
        assert sources == {FieldSource.JOBRIGHT, FieldSource.USER}

    def test_two_applications_keep_separate_rows_for_one_key(
        self, db: Database
    ) -> None:
        first = staged(db, thread_id="application-1")
        second = staged(db, thread_id="application-2")

        for app_id in (first, second):
            db.save_application_field(
                application_id=app_id,
                stable_key="form|input|name",
                source=FieldSource.JOBRIGHT,
                required=True,
                filled=True,
            )

        assert len(db.get_application_fields(first)) == 1
        assert len(db.get_application_fields(second)) == 1


class TestModelCostAccumulates:
    def test_cost_is_added_to_whatever_is_already_recorded(
        self, db: Database
    ) -> None:
        app_id = staged(db)

        assert db.add_model_cost(app_id, Decimal("0.25")) == Decimal("0.25")
        assert db.add_model_cost(app_id, Decimal("0.10")) == Decimal("0.35")

        record = db.get_application(app_id)
        assert record is not None
        assert Decimal(str(record.model_cost)) == Decimal("0.35")

    def test_a_never_charged_application_starts_from_zero(
        self, db: Database
    ) -> None:
        app_id = staged(db)
        record = db.get_application(app_id)
        assert record is not None and record.model_cost is None

        assert db.add_model_cost(app_id, Decimal("0.02")) == Decimal("0.02")

    def test_adding_nothing_leaves_the_total_alone(self, db: Database) -> None:
        app_id = staged(db)
        db.add_model_cost(app_id, Decimal("0.25"))

        assert db.add_model_cost(app_id, Decimal("0")) == Decimal("0.25")

    def test_concurrent_charges_are_all_counted(self, db: Database) -> None:
        """The read-add-write must be one transaction, not three steps.

        Sixteen threads charging a cent each is the cheapest way to catch a
        lost update: with a plain read-then-write, some pass reads a total
        another pass is about to replace, and the money quietly vanishes.
        """
        app_id = staged(db)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: db.add_model_cost(app_id, Decimal("0.01")), range(16)))

        record = db.get_application(app_id)
        assert record is not None
        assert Decimal(str(record.model_cost)) == Decimal("0.16")


class TestTerminalRecordsAreNotWalkedBackwards:
    @pytest.mark.parametrize(
        "terminal",
        [
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        ],
    )
    def test_a_finished_application_refuses_a_new_status(
        self, db: Database, terminal: ApplicationStatus
    ) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=terminal)

        assert db.update_application(app_id, status=ApplicationStatus.STAGING) is False

        record = db.get_application(app_id)
        assert record is not None and record.status is terminal

    def test_a_submitted_application_is_not_overwritten_by_a_failure(
        self, db: Database
    ) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.SUBMITTED)

        assert db.update_application(app_id, status=ApplicationStatus.FAILED) is False

        record = db.get_application(app_id)
        assert record is not None
        assert record.status is ApplicationStatus.SUBMITTED

    def test_a_finished_application_still_accepts_non_status_facts(
        self, db: Database
    ) -> None:
        """The guard is about the lifecycle, not about the whole row.

        A screenshot captured just after a submission is evidence of the
        submission; refusing it would lose the artefact for no safety gain.
        """
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.SUBMITTED)

        assert db.update_application(app_id, screenshot_path="/shots/a.png") is True

        record = db.get_application(app_id)
        assert record is not None
        assert record.screenshot_path == "/shots/a.png"
        assert record.status is ApplicationStatus.SUBMITTED

    def test_an_operator_can_force_a_status_back(self, db: Database) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.FAILED)

        assert (
            db.update_application(
                app_id, status=ApplicationStatus.STAGING, force=True
            )
            is True
        )

        record = db.get_application(app_id)
        assert record is not None and record.status is ApplicationStatus.STAGING

    @pytest.mark.parametrize(
        "terminal",
        [QueueState.COMPLETED, QueueState.SKIPPED, QueueState.FAILED],
    )
    def test_a_finished_queue_item_refuses_a_new_state(
        self, db: Database, terminal: QueueState
    ) -> None:
        queue_id = db.enqueue_job("https://example.com/jobs/2", Board.LINKEDIN)
        db.update_queue_state(queue_id, terminal, "the first word")

        assert db.update_queue_state(queue_id, QueueState.RUNNING) is False

        item = db.get_queue_item(queue_id)
        assert item is not None
        assert item.state is terminal
        assert item.error_reason == "the first word"


class TestExecutionLease:
    """The single answer to "who is allowed to run this thread right now".

    A lease, rather than a status on the application row, because the
    question is about a *process* and not about the application's place in
    its lifecycle. Overloading the status made two mechanisms answer the
    same question, and left an application that had merely been abandoned
    mid-flight looking as though a human had done something to it.
    """

    def test_the_first_owner_wins_and_the_second_is_told_who_holds_it(
        self, db: Database
    ) -> None:
        first = db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)
        assert first.acquired is True
        assert first.lease.owner == "worker-a"
        assert first.lease.expires_at == T0 + TTL

        second = db.acquire_lease("application-1", "worker-b", ttl=TTL, now=T0)

        assert second.acquired is False
        assert second.lease.owner == "worker-a"

    def test_leases_are_per_thread(self, db: Database) -> None:
        assert db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0).acquired
        assert db.acquire_lease("application-2", "worker-b", ttl=TTL, now=T0).acquired

    def test_the_holder_may_take_its_own_lease_again(self, db: Database) -> None:
        """Re-acquiring is a renewal, not a deadlock against yourself.

        A worker that failed part-way and is retrying the same thread holds
        a lease it never released; refusing it would leave the only party
        entitled to continue locked out until the lease expired.
        """
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        again = db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0 + STEP)

        assert again.acquired is True
        assert again.lease.expires_at == T0 + STEP + TTL

    def test_an_expired_lease_is_reclaimed(self, db: Database) -> None:
        """This is what stops a SIGKILL stranding an application forever.

        Nobody releases a lease on the way out of a process that was killed
        outright, so the only thing that can free it is time.
        """
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        taken = db.acquire_lease("application-1", "worker-b", ttl=TTL, now=T0 + TTL)

        assert taken.acquired is True
        assert taken.lease.owner == "worker-b"

    def test_a_lease_a_moment_from_expiry_is_still_held(self, db: Database) -> None:
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        early = db.acquire_lease(
            "application-1", "worker-b", ttl=TTL, now=T0 + TTL - STEP
        )

        assert early.acquired is False
        assert early.lease.owner == "worker-a"

    def test_a_renewed_lease_is_never_taken_from_under_its_owner(
        self, db: Database
    ) -> None:
        """A heartbeat is what tells the difference between slow and dead."""
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        moment = T0
        for _ in range(10):
            moment += TTL // 2
            assert db.renew_lease("application-1", "worker-a", ttl=TTL, now=moment)
            assert not db.acquire_lease(
                "application-1", "worker-b", ttl=TTL, now=moment
            ).acquired

        assert moment > T0 + TTL * 4

    def test_renewing_extends_the_expiry(self, db: Database) -> None:
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        renewed = db.renew_lease("application-1", "worker-a", ttl=TTL, now=T0 + STEP)

        assert renewed is not None
        assert renewed.expires_at == T0 + STEP + TTL
        assert renewed.acquired_at == T0

    def test_a_stranger_cannot_renew(self, db: Database) -> None:
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        assert db.renew_lease("application-1", "worker-b", ttl=TTL, now=T0) is None

    def test_renewing_fails_once_someone_else_has_reclaimed(
        self, db: Database
    ) -> None:
        """How a revived worker learns it is no longer in charge.

        A process paused long enough for its lease to lapse — a stopped
        container, a machine asleep — must not carry on as if nothing
        happened once someone else has picked the thread up.
        """
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)
        db.acquire_lease("application-1", "worker-b", ttl=TTL, now=T0 + TTL)

        assert db.renew_lease("application-1", "worker-a", ttl=TTL, now=T0 + TTL) is None

    def test_renewing_a_lease_nobody_holds_fails(self, db: Database) -> None:
        assert db.renew_lease("application-1", "worker-a", ttl=TTL, now=T0) is None

    def test_only_the_owner_releases(self, db: Database) -> None:
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)

        assert db.release_lease("application-1", "worker-b") is False

        held = db.get_lease("application-1")
        assert held is not None and held.owner == "worker-a"

        assert db.release_lease("application-1", "worker-a") is True
        assert db.get_lease("application-1") is None

    def test_releasing_a_lease_someone_else_reclaimed_is_a_no_op(
        self, db: Database
    ) -> None:
        """The dangerous cleanup: a revived worker tidying up after itself.

        Its lease has already gone to another worker that is running the
        thread right now. Deleting the row would leave that worker holding
        nothing, and a third could start on the same application.
        """
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)
        db.acquire_lease("application-1", "worker-b", ttl=TTL, now=T0 + TTL)

        assert db.release_lease("application-1", "worker-a") is False

        held = db.get_lease("application-1")
        assert held is not None and held.owner == "worker-b"

    def test_releasing_a_lease_that_is_not_there_is_false(self, db: Database) -> None:
        assert db.release_lease("application-1", "worker-a") is False

    def test_a_released_lease_can_be_taken_immediately(self, db: Database) -> None:
        db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0)
        db.release_lease("application-1", "worker-a")

        retry = db.acquire_lease("application-1", "worker-b", ttl=TTL, now=T0)

        assert retry.acquired is True

    def test_only_one_of_many_concurrent_acquirers_wins(self, db: Database) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda n: db.acquire_lease(
                        "application-1", f"worker-{n}", ttl=TTL, now=T0
                    ).acquired,
                    range(8),
                )
            )

        assert sum(results) == 1

    def test_an_active_lease_knows_it_is_active(self, db: Database) -> None:
        lease = db.acquire_lease("application-1", "worker-a", ttl=TTL, now=T0).lease

        assert lease.active_at(T0 + STEP) is True
        assert lease.active_at(T0 + TTL) is False


class TestLegacyDatabasesFailLoudly:
    def _legacy(self, settings: Settings) -> sqlite3.Connection:
        """A database with the tables but none of the identity indices."""
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.sqlite_path)
        conn.executescript(
            """
            CREATE TABLE job_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_url TEXT NOT NULL, board TEXT NOT NULL, state TEXT NOT NULL,
                error_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER NOT NULL REFERENCES job_queue(id),
                thread_id TEXT NOT NULL, ats TEXT, status TEXT NOT NULL,
                trigger_tier INTEGER, model_cost REAL, screenshot_path TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE application_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                stable_key TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL, required INTEGER NOT NULL,
                filled INTEGER NOT NULL, value TEXT
            );
            INSERT INTO job_queue VALUES
                (1, 'https://example.com/1', 'linkedin', 'running', NULL, 'x', 'x');
            """
        )
        return conn

    def test_duplicate_thread_ids_name_themselves(self, settings: Settings) -> None:
        conn = self._legacy(settings)
        conn.executescript(
            """
            INSERT INTO applications VALUES
                (1, 1, 'application-1', NULL, 'staging', NULL, NULL, NULL, 'x', 'x'),
                (2, 1, 'application-1', NULL, 'staging', NULL, NULL, NULL, 'x', 'x');
            """
        )
        conn.commit()
        conn.close()

        with pytest.raises(SchemaMigrationRequired) as caught:
            Database(settings).initialize()

        message = str(caught.value)
        assert "application-1" in message
        assert "applications" in message
        assert "thread_id" in message

    def test_duplicate_provenance_rows_name_themselves(
        self, settings: Settings
    ) -> None:
        conn = self._legacy(settings)
        conn.executescript(
            """
            INSERT INTO applications VALUES
                (1, 1, 'application-1', NULL, 'staging', NULL, NULL, NULL, 'x', 'x');
            INSERT INTO application_fields VALUES
                (1, 1, 'form|input|name', '{}', 'jobright', 1, 1, NULL),
                (2, 1, 'form|input|name', '{}', 'jobright', 1, 1, NULL);
            """
        )
        conn.commit()
        conn.close()

        with pytest.raises(SchemaMigrationRequired) as caught:
            Database(settings).initialize()

        message = str(caught.value)
        assert "form|input|name" in message
        assert "application_fields" in message

    def test_an_application_left_mid_resume_is_returned_to_the_gate(
        self, settings: Settings
    ) -> None:
        """`resuming` was briefly a status; ownership is a lease now.

        An application stuck in it would be readable by no version of this
        code and decidable by none either, so startup puts it back where it
        was — awaiting a decision — and lets the lease decide who may act.
        """
        conn = self._legacy(settings)
        stamp = T0.isoformat()
        conn.execute(
            "INSERT INTO applications VALUES"
            " (1, 1, 'application-1', NULL, 'resuming', NULL, NULL, NULL, ?, ?)",
            (stamp, stamp),
        )
        conn.commit()
        conn.close()

        db = Database(settings)
        db.initialize()

        record = db.get_application(1)
        assert record is not None
        assert record.status is ApplicationStatus.AWAITING_APPROVAL

    def test_a_clean_legacy_database_migrates_without_complaint(
        self, settings: Settings
    ) -> None:
        conn = self._legacy(settings)
        conn.executescript(
            """
            INSERT INTO applications VALUES
                (1, 1, 'application-1', NULL, 'staging', NULL, NULL, NULL, 'x', 'x');
            INSERT INTO application_fields VALUES
                (1, 1, 'form|input|name', '{}', 'jobright', 1, 1, NULL);
            """
        )
        conn.commit()
        conn.close()

        db = Database(settings)
        db.initialize()

        assert len(db.get_application_fields(1)) == 1
