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


class TestResumeClaim:
    def test_the_first_claim_wins_and_the_second_is_refused(
        self, db: Database
    ) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.AWAITING_APPROVAL)

        claimed, record = db.claim_resume(app_id)
        assert claimed is True
        assert record is not None and record.status is ApplicationStatus.RESUMING

        again, current = db.claim_resume(app_id)
        assert again is False
        assert current is not None and current.status is ApplicationStatus.RESUMING

    def test_a_submitted_application_cannot_be_claimed(self, db: Database) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.SUBMITTED)

        claimed, record = db.claim_resume(app_id)

        assert claimed is False
        assert record is not None and record.status is ApplicationStatus.SUBMITTED

    def test_an_unknown_application_cannot_be_claimed(self, db: Database) -> None:
        assert db.claim_resume(9999) == (False, None)

    def test_releasing_a_claim_reopens_the_gate(self, db: Database) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.AWAITING_APPROVAL)
        db.claim_resume(app_id)

        assert db.release_resume_claim(app_id) is True

        record = db.get_application(app_id)
        assert record is not None
        assert record.status is ApplicationStatus.AWAITING_APPROVAL

    def test_releasing_never_drags_a_finished_application_back(
        self, db: Database
    ) -> None:
        """A claim released after the winner submitted must be a no-op.

        The loser's cleanup runs after the winner's terminal write; if it
        reopened the gate, the same application could be submitted twice.
        """
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.AWAITING_APPROVAL)
        db.claim_resume(app_id)
        db.update_application(app_id, status=ApplicationStatus.SUBMITTED)

        assert db.release_resume_claim(app_id) is False

        record = db.get_application(app_id)
        assert record is not None
        assert record.status is ApplicationStatus.SUBMITTED

    def test_only_one_of_many_concurrent_claims_succeeds(self, db: Database) -> None:
        app_id = staged(db)
        db.update_application(app_id, status=ApplicationStatus.AWAITING_APPROVAL)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: db.claim_resume(app_id)[0], range(8)))

        assert sum(results) == 1


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
