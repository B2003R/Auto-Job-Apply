"""Shared harness for the control-plane tests.

The worker under test is always the shipped `ApplicationWorker` with only
its browser side replaced: the page broker, board adapter, trigger, field
writer, and submitter are the fakes from `tests/agent/support.py`, while
the database, approval service, rate limiter, gap filler, and the
LangGraph application graph itself are the real ones. Nothing here
launches a browser, opens a socket, or submits anything.

Used by `tests/test_api.py` and `tests/scripts/test_cli.py`, so the CLI is
exercised against the same control plane the API tests pin down rather
than against a second, more forgiving imitation of it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.agent.graph import thread_id_for
from app.config import Settings
from app.main import ApplicationWorker, create_app
from app.storage.db import Database
from app.storage.models import ApplicationStatus
from tests.agent.support import (
    LISTING_URL,
    World,
    build_world,
    cover_letter_gap,
    make_field,
    snapshot,
)

#: A client address the loopback rule accepts. `TestClient` otherwise
#: presents itself as "testclient", which is deliberately refused.
LOOPBACK = ("127.0.0.1", 51234)

TOKEN = "s3cret-operator-token"


class FakeSession:
    """A browser session that counts its own lifecycle, and opens nothing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.starts = 0
        self.closes = 0
        self.context = object()

    async def start(self) -> Any:
        self.starts += 1
        return self.context

    async def close(self) -> None:
        self.closes += 1


class Harness:
    """One world, one worker factory, and the settings the app is built on."""

    def __init__(self, world: World, *, token: str = "") -> None:
        self.world = world
        self.sessions: list[FakeSession] = []
        self.workers: list[ApplicationWorker] = []
        self.settings = Settings(
            _env_file=None,
            sqlite_path=world.settings.sqlite_path,
            artifacts_path=world.settings.artifacts_path,
            api_token=token,
        )

    def session_factory(self, settings: Settings) -> FakeSession:
        session = FakeSession(settings)
        self.sessions.append(session)
        return session

    def build_worker(self, *, run_loop: bool = True) -> ApplicationWorker:
        """A worker over this world.

        `run_loop=False` is what a caller driving `drain()` by hand needs:
        a running loop is draining the same queue, so the two would race
        for the claims — which is also why `drain()` is only ever called by
        the loop, or by a caller that asked for no loop.
        """
        worker = ApplicationWorker(
            self.settings,
            db=self.world.db,
            session_factory=self.session_factory,
            dependencies_factory=lambda _settings, _db, _context: self.world.deps,
            checkpointer_path=self.world.checkpoint_path,
            poll_interval_s=0.01,
            run_loop=run_loop,
        )
        self.workers.append(worker)
        return worker

    def worker_factory(self, settings: Settings) -> ApplicationWorker:
        return self.build_worker()

    def app(self) -> Any:
        return create_app(self.settings, self.worker_factory)

    def client(self, **kwargs: Any) -> TestClient:
        headers = {}
        if self.settings.api_token.get_secret_value():
            headers["Authorization"] = f"Bearer {TOKEN}"
        kwargs.setdefault("client", LOOPBACK)
        kwargs.setdefault("headers", headers)
        return TestClient(self.app(), **kwargs)

    @property
    def db(self) -> Database:
        return self.world.db


def gap_world(tmp_path: Path, **kwargs: Any) -> World:
    """A world whose one listing always leaves a gap for a human.

    The unanswered cover letter is what parks every application in these
    tests at the approval gate, which is where the control plane's
    interesting behaviour lives.
    """
    kwargs.setdefault(
        "before",
        snapshot(make_field("name", label="Full name", required=True), cover_letter_gap()),
    )
    kwargs.setdefault(
        "after",
        snapshot(
            make_field("name", label="Full name", required=True, filled=True, value="Ada"),
            cover_letter_gap(),
        ),
    )
    return build_world(tmp_path, **kwargs)


def wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Give the worker's own task time to reach a state, without sleeping blind.

    The worker runs in the test client's event loop thread, so the test
    thread has to yield. A deadline rather than a fixed sleep keeps the
    suite fast when the worker is quick and honest when it is not.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the worker never reached the expected state in time")


def queue_one(client: TestClient, url: str = LISTING_URL) -> dict[str, Any]:
    response = client.post("/queue", json={"listing_url": url, "board": "linkedin"})
    assert response.status_code == 201, response.text
    return dict(response.json())


def staged_application(harness: Harness, queue_id: int) -> int:
    """Wait until the worker has parked one application at the gate.

    Both halves matter. The application row says a decision is wanted; the
    released lease says the worker has actually let the thread go. A
    decision sent in between is correctly refused as in progress, so a
    caller that waited only for the row would be racing the worker.
    """
    thread_id = thread_id_for(queue_id)

    def parked() -> bool:
        record = harness.db.get_application_by_thread(thread_id)
        return (
            record is not None
            and record.status is ApplicationStatus.AWAITING_APPROVAL
            and harness.db.get_lease(thread_id) is None
        )

    wait_for(parked)
    record = harness.db.get_application_by_thread(thread_id)
    assert record is not None
    return record.id


__all__ = [
    "FakeSession",
    "Harness",
    "LISTING_URL",
    "LOOPBACK",
    "TOKEN",
    "Database",
    "gap_world",
    "queue_one",
    "staged_application",
    "wait_for",
]
