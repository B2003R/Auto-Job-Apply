"""SQLite persistence for queue, applications, approvals, and rate events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

from app.config import Settings
from app.storage.models import (
    TERMINAL_APPLICATION_STATUSES,
    TERMINAL_QUEUE_STATES,
    ApplicationField,
    ApplicationRecord,
    ApplicationStatus,
    ApprovalDecision,
    ApprovalRecord,
    Board,
    ExecutionLease,
    FieldSource,
    LeaseAttempt,
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
    # A thread id is the key an approval resumes through, so it must identify
    # exactly one application. Two rows sharing one thread would make
    # `get_application_by_thread` ambiguous, and a decision recorded against
    # one of them could release the other.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_thread_id
        ON applications(thread_id);
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
    # What a provenance row asserts is "this application had this control
    # filled from this source", which is either true or not: recording it
    # twice states nothing new. Graph nodes are re-executed after a crash or
    # a replayed checkpoint, so without this index the same attribution pass
    # would append a second row every time, and a reviewer counting required
    # fields would read a form as larger than it is.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_application_fields_identity
        ON application_fields(application_id, stable_key, source);
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
    # One row per thread that some process is currently running. The primary
    # key is what makes "one owner at a time" a schema fact rather than a
    # convention two code paths have to agree on.
    """
    CREATE TABLE IF NOT EXISTS execution_leases (
        thread_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
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


#: Shared UTC-day predicate for rate events. Counting and admission must use
#: exactly the same window, or a cap could be checked over one day's rows and
#: enforced against another's.
_RATE_DAY_PREDICATE = (
    "board = ? AND julianday(timestamp) >= julianday(?) "
    "AND julianday(timestamp) < julianday(?) + 1"
)

#: How long a connection waits for a write lock before giving up. Admission
#: serializes concurrent workers on a single immediate transaction, so a brief
#: wait under contention is expected and must not surface as an error.
_BUSY_TIMEOUT_MS = 5_000


#: Cost is stored in a REAL column but reasoned about as an exact decimal.
#: Quantising every total to this many places keeps the float round-trip
#: lossless (Python's shortest repr recovers the same decimal) so repeated
#: additions cannot drift.
_COST_PLACES = Decimal("0.000001")


class UnknownApplication(LookupError):
    """A write named an application row that is not there.

    Distinct from the agent layer's `UnknownApplicationError`, which is
    about a decision naming an application a human could have mistyped;
    this one means the storage layer was asked to charge or update a row
    that does not exist, which is a programming error.
    """

    def __init__(self, application_id: int) -> None:
        super().__init__(f"no application with id {application_id}")
        self.application_id = application_id


class SchemaMigrationRequired(RuntimeError):
    """A database written before an identity rule existed still violates it.

    Raised from `Database.initialize` instead of letting `CREATE UNIQUE
    INDEX` fail with a bare IntegrityError, which says only that some index
    could not be built. This names the table, the columns, and the offending
    values, so an operator can see what to merge or delete.
    """


#: Uniqueness rules added after the first release. Each is checked for
#: pre-existing violations before its index is created, so an old database
#: reports what is wrong with it rather than failing to open.
_IDENTITY_RULES = (
    ("applications", ("thread_id",)),
    ("application_fields", ("application_id", "stable_key", "source")),
)


def _assert_no_duplicates(conn: sqlite3.Connection) -> None:
    for table, columns in _IDENTITY_RULES:
        if not _table_exists(conn, table):
            continue
        grouped = ", ".join(columns)
        rows = conn.execute(
            f"""
            SELECT {grouped}, COUNT(*) AS copies FROM {table}
            GROUP BY {grouped} HAVING copies > 1 ORDER BY copies DESC LIMIT 5
            """
        ).fetchall()
        if not rows:
            continue
        offenders = "; ".join(
            ", ".join(f"{column}={row[column]!r}" for column in columns)
            + f" ({row['copies']} rows)"
            for row in rows
        )
        raise SchemaMigrationRequired(
            f"{table} holds rows that repeat ({grouped}), which this version "
            f"treats as one identity: {offenders}. Keep the row you want to "
            f"survive and delete the rest, then start again — for example "
            f"`DELETE FROM {table} WHERE id NOT IN (SELECT MAX(id) FROM "
            f"{table} GROUP BY {grouped})`."
        )


#: A status this project used, briefly, to mean "a worker is resuming this".
#: Ownership is an `execution_leases` row now, so an application still
#: carrying it is put back where it was: awaiting a decision, with no claim
#: on it. Leaving the value in place would make the row unreadable, since
#: `ApplicationStatus` no longer has a member for it.
_RETIRED_RESUMING_STATUS = "resuming"


def _retire_resuming_status(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "applications"):
        return
    conn.execute(
        "UPDATE applications SET status = ? WHERE status = ?",
        (ApplicationStatus.AWAITING_APPROVAL.value, _RETIRED_RESUMING_STATUS),
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(utc_day: date) -> str:
    return f"{utc_day.isoformat()}T00:00:00+00:00"


def _format_ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _application(row: sqlite3.Row | None) -> ApplicationRecord | None:
    if row is None:
        return None
    return ApplicationRecord(
        id=row["id"],
        queue_id=row["queue_id"],
        thread_id=row["thread_id"],
        ats=row["ats"],
        status=ApplicationStatus(row["status"]),
        trigger_tier=row["trigger_tier"],
        model_cost=row["model_cost"],
        screenshot_path=row["screenshot_path"],
        created_at=_parse_ts(row["created_at"]),
        updated_at=_parse_ts(row["updated_at"]),
    )


def _lease(row: sqlite3.Row | None) -> ExecutionLease | None:
    if row is None:
        return None
    return ExecutionLease(
        thread_id=row["thread_id"],
        owner=row["owner"],
        acquired_at=_parse_ts(row["acquired_at"]),
        renewed_at=_parse_ts(row["renewed_at"]),
        expires_at=_parse_ts(row["expires_at"]),
    )


def _approval(row: sqlite3.Row | None) -> ApprovalRecord | None:
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
            _assert_no_duplicates(conn)
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            _retire_resuming_status(conn)
            conn.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._settings.sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a write-locked transaction that commits or rolls back as one.

        `BEGIN IMMEDIATE` takes the write lock up front, so a read performed
        inside the block cannot be invalidated by another connection before
        the matching write lands. That is what makes a read-then-write
        decision (check a cap, then record an event) atomic rather than
        merely sequential.
        """
        with self.connect() as conn:
            conn.isolation_level = None  # explicit transaction control
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    # SQLite may have already aborted the transaction. The
                    # caller's exception is the real story, so it is never
                    # replaced by a rollback bookkeeping error.
                    pass
                raise
            conn.execute("COMMIT")

    def enqueue_job(self, listing_url: str, board: Board) -> int:
        now = _format_ts(_utc_now())
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO job_queue (listing_url, board, state, error_reason, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                RETURNING id
                """,
                (listing_url, board.value, QueueState.PENDING.value, now, now),
            ).fetchone()
            conn.commit()
            return int(row["id"])

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
        *,
        force: bool = False,
    ) -> bool:
        """Move a queue item, unless it has already finished.

        Returns whether the row was written. A finished item is left alone:
        a retried node or a replayed checkpoint arriving after the outcome
        was recorded must not put a completed application back in flight.
        `force` is for an operator deliberately requeueing something.
        """
        now = _format_ts(_utc_now())
        clause = ""
        guard: tuple[Any, ...] = ()
        if not force:
            terminal = sorted(item.value for item in TERMINAL_QUEUE_STATES)
            clause = f" AND state NOT IN ({', '.join('?' * len(terminal))})"
            guard = tuple(terminal)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE job_queue
                SET state = ?, error_reason = ?, updated_at = ?
                WHERE id = ?{clause}
                """,
                (state.value, error_reason, now, queue_id, *guard),
            )
            conn.commit()
            return cursor.rowcount == 1

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
            row = conn.execute(
                """
                INSERT INTO applications (
                    queue_id, thread_id, ats, status, trigger_tier,
                    model_cost, screenshot_path, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
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
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def get_application(self, application_id: int) -> ApplicationRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE id = ?",
                (application_id,),
            ).fetchone()
        return _application(row)

    def get_application_by_thread(self, thread_id: str) -> ApplicationRecord | None:
        """The single application owning `thread_id`, if any.

        A unique index makes "single" a schema fact rather than a
        convention, so this can never silently return the first of several
        rows an approval might otherwise resume the wrong one of.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return _application(row)

    def update_application(
        self,
        application_id: int,
        *,
        status: ApplicationStatus | None = None,
        ats: str | None = None,
        trigger_tier: int | None = None,
        model_cost: float | None = None,
        screenshot_path: str | None = None,
        force: bool = False,
    ) -> bool:
        """Update only the columns actually named by the caller.

        Every parameter defaults to `None` meaning "leave alone", so a node
        that learns one fact (the detected ATS) cannot blank out another it
        never looked at (the trigger tier recorded a step earlier).

        A *status* change is additionally refused once the application has
        finished, because that transition is the one with consequences: a
        node re-executed after a crash must not reopen a submitted
        application. Facts that merely describe the finished application (a
        screenshot taken just after submitting) are still accepted, and
        `force` exists for an operator who means it. Returns whether the row
        was written.
        """
        assignments: list[str] = []
        assigned: list[Any] = []
        for column, value in (
            ("status", status.value if status is not None else None),
            ("ats", ats),
            ("trigger_tier", trigger_tier),
            ("model_cost", model_cost),
            ("screenshot_path", screenshot_path),
        ):
            if value is None:
                continue
            assignments.append(f"{column} = ?")
            assigned.append(value)
        if not assignments:
            return False
        assignments.append("updated_at = ?")
        assigned.append(_format_ts(_utc_now()))

        clause = ""
        guard: tuple[Any, ...] = ()
        if status is not None and not force:
            terminal = sorted(item.value for item in TERMINAL_APPLICATION_STATUSES)
            clause = f" AND status NOT IN ({', '.join('?' * len(terminal))})"
            guard = tuple(terminal)

        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE applications SET {', '.join(assignments)} "
                f"WHERE id = ?{clause}",
                (*assigned, application_id, *guard),
            )
            conn.commit()
            return cursor.rowcount == 1

    def add_model_cost(self, application_id: int, amount: Decimal) -> Decimal:
        """Add spend to an application's running total, returning the total.

        Additive rather than assigned, and atomic rather than read-then-write,
        because a node that calls a model and then crashes before finishing
        has still spent the money. When the graph re-executes that node the
        model is called again, and the second charge is a second real charge:
        replacing the total would under-report the bill by exactly the
        amount the crashed attempt cost.
        """
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT model_cost FROM applications WHERE id = ?",
                (application_id,),
            ).fetchone()
            if row is None:
                raise UnknownApplication(application_id)
            current = Decimal(str(row["model_cost"] or "0"))
            total = (current + amount).quantize(_COST_PLACES)
            conn.execute(
                "UPDATE applications SET model_cost = ?, updated_at = ? WHERE id = ?",
                (float(total), _format_ts(_utc_now()), application_id),
            )
        return total

    def acquire_lease(
        self,
        thread_id: str,
        owner: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> LeaseAttempt:
        """Become the one process allowed to run this thread, if nobody is.

        Compare-and-set inside an immediate transaction, so exactly one
        caller wins whichever process or coroutine it runs in. Three ways to
        succeed: nobody holds the thread, the holder's lease has lapsed, or
        the holder is you — a worker retrying a thread it failed part-way
        through must not be locked out by its own abandoned lease.

        `now` is a parameter rather than a clock reading because expiry is
        the whole mechanism, and a mechanism whose behaviour depends on the
        wall clock is one that can only be tested by waiting.

        In-process locking cannot do this job. Two workers on one database
        each hold their own lock, and an application both of them start is
        applied for twice.
        """
        expires = now + ttl
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM execution_leases WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            held = _lease(row)
            if held is not None and held.active_at(now) and not held.held_by(owner):
                return LeaseAttempt(acquired=False, lease=held)

            acquired_at = (
                held.acquired_at if held is not None and held.held_by(owner) else now
            )
            taken = conn.execute(
                """
                INSERT INTO execution_leases
                    (thread_id, owner, acquired_at, renewed_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    owner = excluded.owner,
                    acquired_at = excluded.acquired_at,
                    renewed_at = excluded.renewed_at,
                    expires_at = excluded.expires_at
                RETURNING *
                """,
                (
                    thread_id,
                    owner,
                    _format_ts(acquired_at),
                    _format_ts(now),
                    _format_ts(expires),
                ),
            ).fetchone()
        mine = _lease(taken)
        if mine is None:  # pragma: no cover - the upsert above guarantees a row
            raise RuntimeError(f"lease for thread {thread_id!r} vanished on acquire")
        return LeaseAttempt(acquired=True, lease=mine)

    def renew_lease(
        self,
        thread_id: str,
        owner: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> ExecutionLease | None:
        """Push a held lease's expiry out, proving the holder is still alive.

        Guarded on the owner, not on the expiry: a worker whose lease lapsed
        while nobody wanted it is still the rightful holder and may carry
        on, but one whose lease has been *reclaimed* gets `None` and learns
        that it is no longer in charge.
        """
        with self.immediate_transaction() as conn:
            row = conn.execute(
                """
                UPDATE execution_leases SET renewed_at = ?, expires_at = ?
                WHERE thread_id = ? AND owner = ?
                RETURNING *
                """,
                (_format_ts(now), _format_ts(now + ttl), thread_id, owner),
            ).fetchone()
        return _lease(row)

    def release_lease(self, thread_id: str, owner: str) -> bool:
        """Give the thread back, if it is still yours to give.

        Owner-guarded because the dangerous release is the late one: a
        worker tidying up after a lease that has already passed to somebody
        else would leave that somebody holding nothing, and a third worker
        free to start on the same application.
        """
        with self.immediate_transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM execution_leases WHERE thread_id = ? AND owner = ?",
                (thread_id, owner),
            )
            return bool(cursor.rowcount == 1)

    def get_lease(self, thread_id: str) -> ExecutionLease | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_leases WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return _lease(row)

    def record_approval(
        self,
        application_id: int,
        decision: ApprovalDecision,
        actor: str,
        note: str | None = None,
        timestamp: datetime | None = None,
    ) -> ApprovalRecord:
        """Record a decision, or return the one already recorded.

        See `try_record_approval`: an approval is append-only, so this never
        overwrites an existing decision. Callers that need to know whether
        theirs was the one stored should use `try_record_approval` directly.
        """
        _recorded, record = self.try_record_approval(
            application_id=application_id,
            decision=decision,
            actor=actor,
            note=note,
            timestamp=timestamp,
        )
        return record

    def try_record_approval(
        self,
        application_id: int,
        decision: ApprovalDecision,
        actor: str,
        note: str | None = None,
        timestamp: datetime | None = None,
    ) -> tuple[bool, ApprovalRecord]:
        """Store a decision only if this application has none yet.

        Returns `(recorded, stored)` where `stored` is always the decision
        that is now in the table — this call's, or the one that was already
        there. The read-back happens inside the same immediate transaction
        as the insert, so two concurrent gates cannot both believe they were
        the one that decided.

        Deliberately insert-only. An approval is the audit record of a
        human authorising a submission made in their name; an upsert here
        would let a second request quietly replace who decided, when, and
        why, and no amount of validation in a caller could put that back.
        """
        ts = _format_ts(timestamp or _utc_now())
        with self.immediate_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO approvals (application_id, decision, actor, note, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO NOTHING
                """,
                (application_id, decision.value, actor, note, ts),
            )
            recorded = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM approvals WHERE application_id = ?",
                (application_id,),
            ).fetchone()
        stored = _approval(row)
        if stored is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError(
                f"approval for application {application_id} vanished during recording"
            )
        return recorded, stored

    def get_approval(self, application_id: int) -> ApprovalRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE application_id = ?",
                (application_id,),
            ).fetchone()
        return _approval(row)

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
        day_start = _day_start(utc_day)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM rate_events WHERE {_RATE_DAY_PREDICATE}",
                (board.value, day_start, day_start),
            ).fetchone()
        return int(row["count"])

    def try_record_rate_event(
        self,
        board: Board,
        action: str,
        timestamp: datetime,
        cap: int,
    ) -> tuple[bool, int]:
        """Record an event only if the board is still under `cap` that UTC day.

        Returns `(recorded, count)` where `count` is the board's event count
        for that day *after* the attempt. The count and the insert happen
        inside one immediate transaction, so two workers that both see 39 of
        40 cannot both be admitted: the second one's check runs after the
        first one's write is committed, not before it.
        """
        moment = timestamp if timestamp.tzinfo is not None else timestamp.replace(
            tzinfo=timezone.utc
        )
        moment = moment.astimezone(timezone.utc)
        day_start = _day_start(moment.date())
        window = (board.value, day_start, day_start)

        with self.immediate_transaction() as conn:
            count = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM rate_events WHERE {_RATE_DAY_PREDICATE}",
                    window,
                ).fetchone()["count"]
            )
            if count >= max(0, cap):
                return False, count
            conn.execute(
                "INSERT INTO rate_events (board, action, timestamp) VALUES (?, ?, ?)",
                (board.value, action, _format_ts(moment)),
            )
            return True, count + 1

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
        """Record where one control's content came from, once.

        Upserts on `(application_id, stable_key, source)`: replaying the
        attribution pass after a crash restates the same fact rather than
        appending a duplicate, and the latest observation of that fact wins,
        so a field seen empty and later seen filled ends up recorded as
        filled. Returns the row id either way.
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO application_fields (
                    application_id, stable_key, metadata_json, source,
                    required, filled, value
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id, stable_key, source) DO UPDATE SET
                    metadata_json = excluded.metadata_json,
                    required = excluded.required,
                    filled = excluded.filled,
                    value = excluded.value
                RETURNING id
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
            ).fetchone()
            conn.commit()
            return int(row["id"])

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
