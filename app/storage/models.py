"""Storage enums and dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class Board(str, Enum):
    LINKEDIN = "linkedin"
    JOBRIGHT = "jobright"
    WELLFOUND = "wellfound"
    HANDSHAKE = "handshake"


class QueueState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApplicationStatus(str, Enum):
    STAGING = "staging"
    AWAITING_APPROVAL = "awaiting_approval"
    # A decision has been claimed and the submit path is in flight. Held only
    # between the claim and the terminal write, it is what stops a second
    # worker acting on the same approval.
    RESUMING = "resuming"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    # Distinct from FAILED: the application was abandoned on purpose (an
    # unrecognised ATS, a captcha, a login wall, a reached cap), not broken.
    # Conflating the two would make a log of genuine malfunctions unreadable.
    SKIPPED = "skipped"
    FAILED = "failed"


#: Statuses that mean this application's story is over. Reaching one is a
#: one-way door: a late writer from a retried node or a replayed checkpoint
#: must not walk a submitted application back to staging and apply again.
TERMINAL_APPLICATION_STATUSES = frozenset(
    {
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.SKIPPED,
        ApplicationStatus.FAILED,
    }
)

#: The queue-side equivalent, for the same reason.
TERMINAL_QUEUE_STATES = frozenset(
    {QueueState.COMPLETED, QueueState.FAILED, QueueState.SKIPPED}
)


class FieldSource(str, Enum):
    JOBRIGHT = "jobright"
    LLM = "llm"
    USER = "user"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class QueueItem:
    id: int
    listing_url: str
    board: Board
    state: QueueState
    error_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ApplicationRecord:
    id: int
    queue_id: int
    thread_id: str
    ats: str | None
    status: ApplicationStatus
    trigger_tier: int | None
    model_cost: float | None
    screenshot_path: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ApplicationField:
    id: int
    application_id: int
    stable_key: str
    metadata: dict[str, Any]
    source: FieldSource
    required: bool
    filled: bool
    value: str | None


@dataclass(frozen=True)
class ApprovalRecord:
    id: int
    application_id: int
    decision: ApprovalDecision
    actor: str
    note: str | None
    timestamp: datetime
