"""SQLite persistence for queue, applications, approvals, and rate events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator

from app.config import Settings
from app.storage.models import (
    ApplicationField,
    ApplicationStatus,
    ApprovalDecision,
    ApprovalRecord,
    Board,
    FieldSource,
    QueueItem,
    QueueState,
)

_SCHEMA_STATEMENTS = (
    "PRAGMA foreign_keys = ON;",
    """
    CREATE TABLE IF NOT EXISTS job_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_url TEXT NOT NULL,
        board TEXT NOT NULL,
        state TEXT NOT NULL,
        error_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        queue_id INTEGER NOT NULL REFERENCES job_queue(id),
        thread_id TEXT NOT NULL,
        ats TEXT,
        status TEXT NOT NULL,
        trigger_tier INTEGER,
        model_cost REAL,
        screenshot_path TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS application_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id),
        stable_key TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL,
        required INTEGER NOT NULL,
        filled INTEGER NOT NULL,
        value TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL UNIQUE REFERENCES applications(id),
        decision TEXT NOT NULL,
        actor TEXT NOT NULL,
        note TEXT,
        timestamp TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        board TEXT NOT NULL,
        action TEXT NOT NULL,
        timestamp TEXT NOT NULL
    );
    """,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Database:
    """Synchronous SQLite access layer."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def settings(self) -> Settings:
        return self._settings

    def initialize(self) -> None:
        self._settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._settings.sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def enqueue_job(self, listing_url: str, board: Board) -> int:
        now = _format_ts(_utc_now())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_queue (listing_url, board, state, error_reason, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (listing_url, board.value, QueueState.PENDING.value, now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_queue_item(self, queue_id: int) -> QueueItem | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
        if row is None:
            return None
        return QueueItem(
            id=row["id"],
            listing_url=row["listing_url"],
            board=Board(row["board"]),
            state=QueueState(row["state"]),
            error_reason=row["error_reason"],
            created_at=_parse_ts(row["created_at"]),
            updated_at=_parse_ts(row["updated_at"]),
        )

    def update_queue_state(
        self,
        queue_id: int,
        state: QueueState,
        error_reason: str | None = None,
    ) -> None:
        now = _format_ts(_utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE job_queue
                SET state = ?, error_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (state.value, error_reason, now, queue_id),
            )
            conn.commit()

    def create_application(
        self,
        queue_id: int,
        thread_id: str,
        status: ApplicationStatus,
        ats: str | None = None,
        trigger_tier: int | None = None,
        model_cost: float | None = None,
        screenshot_path: str | None = None,
    ) -> int:
        now = _format_ts(_utc_now())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO applications (
                    queue_id, thread_id, ats, status, trigger_tier,
                    model_cost, screenshot_path, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_id,
                    thread_id,
                    ats,
                    status.value,
                    trigger_tier,
                    model_cost,
                    screenshot_path,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def record_approval(
        self,
        application_id: int,
        decision: ApprovalDecision,
        actor: str,
        note: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        ts = _format_ts(timestamp or _utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (application_id, decision, actor, note, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    decision = excluded.decision,
                    actor = excluded.actor,
                    note = excluded.note,
                    timestamp = excluded.timestamp
                """,
                (application_id, decision.value, actor, note, ts),
            )
            conn.commit()

    def get_approval(self, application_id: int) -> ApprovalRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE application_id = ?",
                (application_id,),
            ).fetchone()
        if row is None:
            return None
        return ApprovalRecord(
            id=row["id"],
            application_id=row["application_id"],
            decision=ApprovalDecision(row["decision"]),
            actor=row["actor"],
            note=row["note"],
            timestamp=_parse_ts(row["timestamp"]),
        )

    def record_rate_event(
        self,
        board: Board,
        action: str,
        timestamp: datetime,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO rate_events (board, action, timestamp)
                VALUES (?, ?, ?)
                """,
                (board.value, action, _format_ts(timestamp)),
            )
            conn.commit()

    def count_rate_events(self, board: Board, utc_day: date) -> int:
        day_start = f"{utc_day.isoformat()}T00:00:00+00:00"
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM rate_events
                WHERE board = ?
                  AND julianday(timestamp) >= julianday(?)
                  AND julianday(timestamp) < julianday(?) + 1
                """,
                (board.value, day_start, day_start),
            ).fetchone()
        return int(row["count"])

    def save_application_field(
        self,
        application_id: int,
        stable_key: str,
        source: FieldSource,
        required: bool,
        filled: bool,
        value: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO application_fields (
                    application_id, stable_key, metadata_json, source,
                    required, filled, value
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    stable_key,
                    json.dumps(metadata or {}),
                    source.value,
                    int(required),
                    int(filled),
                    value,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_application_fields(self, application_id: int) -> list[ApplicationField]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM application_fields
                WHERE application_id = ?
                ORDER BY id
                """,
                (application_id,),
            ).fetchall()
        return [
            ApplicationField(
                id=row["id"],
                application_id=row["application_id"],
                stable_key=row["stable_key"],
                metadata=json.loads(row["metadata_json"]),
                source=FieldSource(row["source"]),
                required=bool(row["required"]),
                filled=bool(row["filled"]),
                value=row["value"],
            )
            for row in rows
        ]
