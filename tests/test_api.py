"""Tests for the FastAPI control plane and its lifespan worker.

Nothing here launches a browser, opens a socket, or submits anything. The
worker under test is the shipped `ApplicationWorker` with only its browser
side replaced: the page broker, board adapter, trigger, field writer, and
submitter are the fakes from `tests/agent/support.py`, while the database,
approval service, rate limiter, gap filler, and the LangGraph application
graph itself are the real ones. What these tests prove about queueing,
claiming, approval, and error mapping is therefore true of the shipped
code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent.graph import ApplicationRunner, RunStatus, thread_id_for
from app.config import Settings
from app.main import (
    ApplicationWorker,
    WorkerNotReady,
    create_app,
    insecure_binding_reason,
)
from app.storage.models import (
    ApplicationStatus,
    ApprovalDecision,
    Board,
    QueueState,
)
from tests.agent.support import build_world, cover_letter_gap, snapshot
from tests.api_support import (
    LISTING_URL,
    LOOPBACK,
    TOKEN,
    Harness,
    gap_world,
    queue_one,
    staged_application,
)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(gap_world(tmp_path))


@pytest.fixture
def client(harness: Harness) -> Iterator[TestClient]:
    with harness.client() as running:
        yield running


class TestLifespanOwnsOneWorker:
    """The browser is started once, by the lifespan, and closed once."""

    def test_one_worker_is_built_started_and_stopped(self, harness: Harness) -> None:
        with harness.client() as client:
            client.get("/health")
            client.get("/health")

        assert len(harness.workers) == 1
        assert len(harness.sessions) == 1
        assert harness.sessions[0].starts == 1
        assert harness.sessions[0].closes == 1

    def test_the_browser_is_not_started_before_the_app_runs(
        self, harness: Harness
    ) -> None:
        harness.app()
        assert harness.sessions == []

    def test_requests_share_the_one_runner(self, harness: Harness) -> None:
        with harness.client() as client:
            first = queue_one(client, "https://www.linkedin.com/jobs/view/1/")
            second = queue_one(client, "https://www.linkedin.com/jobs/view/2/")

        assert first["queue_id"] != second["queue_id"]
        assert len(harness.workers) == 1
        assert harness.workers[0].starts == 1

    def test_a_worker_that_cannot_start_stops_what_it_started(
        self, tmp_path: Path
    ) -> None:
        """A half-started worker must not leave a browser or a lock behind."""
        world = build_world(tmp_path)
        harness = Harness(world)

        class BrokenWorker(ApplicationWorker):
            async def start(self) -> None:
                await super().start()
                raise RuntimeError("the poll loop could not be started")

        def factory(settings: Settings) -> ApplicationWorker:
            worker = BrokenWorker(
                settings,
                db=world.db,
                session_factory=harness.session_factory,
                dependencies_factory=lambda _s, _d, _c: world.deps,
                checkpointer_path=world.checkpoint_path,
            )
            harness.workers.append(worker)
            return worker

        with pytest.raises(RuntimeError, match="poll loop"):
            with TestClient(create_app(harness.settings, factory), client=LOOPBACK):
                pass  # pragma: no cover - the lifespan raises before this runs

        assert harness.sessions[0].starts == 1
        assert harness.sessions[0].closes == 1

    def test_starting_a_worker_twice_is_refused(self, harness: Harness) -> None:
        """Two browser startups on one profile is the failure to prevent."""
        worker = harness.build_worker(run_loop=False)

        async def start_twice() -> None:
            await worker.start()
            try:
                with pytest.raises(RuntimeError, match="already been started"):
                    await worker.start()
            finally:
                await worker.stop()

        import asyncio

        asyncio.run(start_twice())
        assert harness.sessions[0].starts == 1
        assert harness.sessions[0].closes == 1


class TestQueueEndpoint:
    def test_a_listing_is_queued_and_reported_back(self, client: TestClient) -> None:
        body = queue_one(client)

        assert body["board"] == "linkedin"
        assert body["listing_url"] == LISTING_URL
        assert body["state"] == QueueState.PENDING.value
        assert body["thread_id"] == thread_id_for(body["queue_id"])

    def test_a_url_that_is_not_the_named_boards_is_refused(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/queue",
            json={"listing_url": "https://linkedin.com.evil.test/jobs/1", "board": "linkedin"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["kind"] == "untrusted_listing_url"

    def test_an_unknown_board_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/queue", json={"listing_url": LISTING_URL, "board": "monster"}
        )
        assert response.status_code == 422

    def test_a_missing_listing_url_is_refused(self, client: TestClient) -> None:
        response = client.post("/queue", json={"board": "linkedin"})
        assert response.status_code == 422

    def test_an_unexpected_body_field_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/queue",
            json={"listing_url": LISTING_URL, "board": "linkedin", "state": "completed"},
        )
        assert response.status_code == 422

    def test_a_non_http_scheme_never_reaches_the_queue(
        self, client: TestClient, harness: Harness
    ) -> None:
        response = client.post(
            "/queue", json={"listing_url": "file:///etc/passwd", "board": "linkedin"}
        )

        assert response.status_code == 422
        assert harness.db.list_queue_items() == []


class TestRunStatus:
    def test_an_unknown_run_is_not_found(self, client: TestClient) -> None:
        response = client.get("/runs/4242")
        assert response.status_code == 404
        assert response.json()["error"]["kind"] == "unknown_queue_item"

    def test_a_queued_run_reports_its_queue_row(self, client: TestClient) -> None:
        queued = queue_one(client)

        response = client.get(f"/runs/{queued['queue_id']}")

        assert response.status_code == 200
        body = response.json()
        assert body["queue_id"] == queued["queue_id"]
        assert body["listing_url"] == LISTING_URL
        assert body["thread_id"] == queued["thread_id"]

    def test_a_staged_run_reports_the_pending_decision(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        body = client.get(f"/runs/{queued['queue_id']}").json()

        assert body["application"]["id"] == application_id
        assert body["application"]["status"] == ApplicationStatus.AWAITING_APPROVAL.value
        assert body["awaiting_decision"] is True
        assert body["interrupt"] is not None
        assert body["decision"] is None

    def test_a_staged_run_never_reports_a_drafted_answer(
        self, tmp_path: Path
    ) -> None:
        """The gate payload names the question, never the answer.

        `LOG_FIELD_VALUES` is off, so the model's draft must not reach the
        response even though the reviewer is being asked about the very
        field it was drafted for.
        """
        world = build_world(
            tmp_path,
            with_router=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        harness = Harness(world)

        with harness.client() as client:
            queued = queue_one(client)
            staged_application(harness, queued["queue_id"])
            response = client.get(f"/runs/{queued['queue_id']}")

        gaps = response.json()["interrupt"]["gaps"]
        assert [gap["label"] for gap in gaps] == ["Why do you want to work here?"]
        assert all("answer" not in gap for gap in gaps)
        assert "Because the work matters." not in response.text

    def test_a_thread_another_worker_is_running_offers_no_decision(
        self, client: TestClient, harness: Harness
    ) -> None:
        """A leased thread reports that it is busy, and shows nothing.

        `awaiting_decision` going false is not enough on its own. If the
        gate payload were still rendered, a reviewer — or a UI polling this
        route — would be looking at a decision they cannot make and would
        be invited into a race the response gave them no way to see. The
        payload comes back as soon as the lease lapses, which is what makes
        a pending approval survive the death of the worker that asked for
        it.
        """
        from datetime import datetime, timedelta, timezone

        queued = queue_one(client)
        staged_application(harness, queued["queue_id"])
        thread_id = thread_id_for(queued["queue_id"])
        harness.db.acquire_lease(
            thread_id,
            "another-worker",
            ttl=timedelta(seconds=120),
            now=datetime.now(timezone.utc),
        )

        body = client.get(f"/runs/{queued['queue_id']}").json()

        assert body["executing"] is True
        assert body["awaiting_decision"] is False
        assert body["interrupt"] is None
        assert body["lease_expires_at"] is not None

        harness.db.release_lease(thread_id, "another-worker")
        after = client.get(f"/runs/{queued['queue_id']}").json()
        assert after["awaiting_decision"] is True
        assert after["interrupt"] is not None

    def test_a_drafted_answer_is_reported_when_logging_is_enabled(
        self, tmp_path: Path
    ) -> None:
        """The redaction is the flag's doing, not an absence of data."""
        world = build_world(
            tmp_path,
            with_router=True,
            log_field_values=True,
            before=snapshot(cover_letter_gap()),
            after=snapshot(cover_letter_gap()),
        )
        harness = Harness(world)

        with harness.client() as client:
            queued = queue_one(client)
            staged_application(harness, queued["queue_id"])
            response = client.get(f"/runs/{queued['queue_id']}")

        assert "Because the work matters." in response.text


class TestApprovalEndpoints:
    def test_approving_submits_the_application(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        response = client.post(f"/applications/{application_id}/approve", json={})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["decision"] == ApprovalDecision.APPROVED.value
        assert body["status"] == RunStatus.SUBMITTED.value
        assert harness.world.submitter.calls == 1
        stored = harness.db.get_application(application_id)
        assert stored is not None
        assert stored.status is ApplicationStatus.SUBMITTED

    def test_rejecting_completes_without_submitting(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        response = client.post(
            f"/applications/{application_id}/reject", json={"note": "wrong seniority"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == RunStatus.REJECTED.value
        assert harness.world.submitter.calls == 0
        approval = harness.db.get_approval(application_id)
        assert approval is not None
        assert approval.note == "wrong seniority"

    def test_the_same_decision_twice_is_idempotent(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        first = client.post(f"/applications/{application_id}/approve", json={})
        second = client.post(f"/applications/{application_id}/approve", json={})

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["decided_at"] == second.json()["decided_at"]
        assert first.json()["actor"] == second.json()["actor"]
        assert harness.world.submitter.calls == 1

    def test_reversing_a_decision_is_a_conflict(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        client.post(f"/applications/{application_id}/reject", json={})
        response = client.post(f"/applications/{application_id}/approve", json={})

        assert response.status_code == 409
        assert response.json()["error"]["kind"] == "approval_conflict"
        assert harness.world.submitter.calls == 0

    def test_a_conflict_never_reveals_the_stored_note(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])
        client.post(
            f"/applications/{application_id}/reject",
            json={"note": "salary discussed off the record"},
        )

        response = client.post(f"/applications/{application_id}/approve", json={})

        assert "salary discussed" not in response.text

    def test_deciding_an_unknown_application_is_not_found(
        self, client: TestClient
    ) -> None:
        response = client.post("/applications/999/approve", json={})
        assert response.status_code == 404
        assert response.json()["error"]["kind"] == "unknown_application"

    def test_deciding_an_application_that_never_reached_the_gate_is_a_conflict(
        self, client: TestClient, harness: Harness
    ) -> None:
        queue_id = harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        application_id = harness.db.create_application(
            queue_id=queue_id,
            thread_id=thread_id_for(queue_id),
            status=ApplicationStatus.STAGING,
        )

        response = client.post(f"/applications/{application_id}/approve", json={})

        assert response.status_code == 409
        assert response.json()["error"]["kind"] == "not_awaiting_approval"

    def test_an_over_long_note_is_refused_as_invalid(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        response = client.post(
            f"/applications/{application_id}/approve", json={"note": "x" * 5000}
        )

        assert response.status_code == 422
        assert harness.db.get_approval(application_id) is None

    def test_an_application_another_worker_is_running_is_reported_as_locked(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])
        # A second worker on the same database holds the thread. Nothing in
        # this process can resume it until that lease lapses or is released.
        from datetime import datetime, timedelta, timezone

        harness.db.acquire_lease(
            thread_id_for(queued["queue_id"]),
            "another-worker",
            ttl=timedelta(seconds=120),
            now=datetime.now(timezone.utc),
        )

        response = client.post(f"/applications/{application_id}/approve", json={})

        assert response.status_code == 423
        assert response.json()["error"]["kind"] == "execution_in_progress"
        assert int(response.headers["Retry-After"]) >= 1
        assert harness.world.submitter.calls == 0


class TestActorIdentity:
    """The actor is the authenticated caller, never a value from the body."""

    def test_the_recorded_actor_names_the_loopback_caller(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        client.post(f"/applications/{application_id}/approve", json={})

        approval = harness.db.get_approval(application_id)
        assert approval is not None
        assert approval.actor == "loopback:127.0.0.1"

    def test_an_actor_in_the_body_is_refused_rather_than_ignored(
        self, client: TestClient, harness: Harness
    ) -> None:
        """Silently ignoring it would leave the sender believing it was used."""
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        response = client.post(
            f"/applications/{application_id}/approve",
            json={"actor": "someone.else@example.com"},
        )

        assert response.status_code == 422
        assert harness.db.get_approval(application_id) is None

    def test_a_decision_in_the_body_cannot_override_the_route(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])

        response = client.post(
            f"/applications/{application_id}/reject", json={"decision": "approve"}
        )

        assert response.status_code == 422
        assert harness.world.submitter.calls == 0

    def test_a_token_caller_is_recorded_by_its_fingerprint_not_its_secret(
        self, tmp_path: Path
    ) -> None:
        world = build_world(tmp_path)
        harness = Harness(world, token=TOKEN)

        with harness.client() as client:
            queued = queue_one(client)
            application_id = staged_application(harness, queued["queue_id"])
            client.post(f"/applications/{application_id}/approve", json={})

        approval = world.db.get_approval(application_id)
        assert approval is not None
        assert approval.actor.startswith("api-token:")
        assert TOKEN not in approval.actor

    def test_a_configured_actor_name_is_used_verbatim(self, tmp_path: Path) -> None:
        world = build_world(tmp_path)
        harness = Harness(world, token=TOKEN)
        harness.settings = Settings(
            _env_file=None,
            sqlite_path=world.settings.sqlite_path,
            artifacts_path=world.settings.artifacts_path,
            api_token=TOKEN,
            api_actor="ada@example.com",
        )

        with harness.client() as client:
            queued = queue_one(client)
            application_id = staged_application(harness, queued["queue_id"])
            client.post(f"/applications/{application_id}/approve", json={})

        approval = world.db.get_approval(application_id)
        assert approval is not None
        assert approval.actor == "ada@example.com"


class TestAuthentication:
    def test_without_a_token_only_loopback_callers_are_served(
        self, harness: Harness
    ) -> None:
        with harness.client(client=("203.0.113.7", 40000)) as client:
            response = client.post(
                "/queue", json={"listing_url": LISTING_URL, "board": "linkedin"}
            )

        assert response.status_code == 403
        assert response.json()["error"]["kind"] == "not_loopback"

    def test_the_default_test_client_address_is_not_treated_as_loopback(
        self, harness: Harness
    ) -> None:
        """Anything that is not a loopback IP is refused, name or not."""
        with TestClient(harness.app()) as client:
            assert client.get("/health").status_code == 403

    def test_a_bearer_token_is_required_when_one_is_configured(
        self, tmp_path: Path
    ) -> None:
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(headers={}) as client:
            response = client.get("/health")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_wrong_token_is_refused(self, tmp_path: Path) -> None:
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(headers={"Authorization": "Bearer nope"}) as client:
            assert client.get("/health").status_code == 401

    def test_a_token_holder_need_not_be_on_loopback(self, tmp_path: Path) -> None:
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(client=("203.0.113.7", 40000)) as client:
            assert client.get("/health").status_code == 200

    def test_presenting_a_token_to_a_server_that_has_none_is_refused(
        self, harness: Harness
    ) -> None:
        """Otherwise the sender believes it authenticated when it did not."""
        with harness.client(headers={"Authorization": "Bearer anything"}) as client:
            assert client.get("/health").status_code == 401

    def test_a_non_loopback_bind_without_a_token_is_named_as_unsafe(self) -> None:
        wide = Settings(_env_file=None, api_host="0.0.0.0")
        loopback = Settings(_env_file=None)
        tokened = Settings(_env_file=None, api_host="0.0.0.0", api_token=TOKEN)

        assert insecure_binding_reason(wide) is not None
        assert "API_TOKEN" in str(insecure_binding_reason(wide))
        assert insecure_binding_reason(loopback) is None
        assert insecure_binding_reason(tokened) is None


class TestWorkerLoop:
    """Claiming and containment, driven directly rather than through HTTP."""

    async def test_the_worker_claims_and_runs_each_pending_item(
        self, harness: Harness
    ) -> None:
        first = harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        second = harness.db.enqueue_job(
            "https://www.linkedin.com/jobs/view/2/", Board.LINKEDIN
        )
        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            results = await worker.drain()
        finally:
            await worker.stop()

        assert [result.queue_id for result in results] == [first, second]
        assert all(result.awaiting_approval for result in results)

    async def test_two_workers_never_process_the_same_item(
        self, harness: Harness
    ) -> None:
        harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        first = harness.build_worker(run_loop=False)
        second = harness.build_worker(run_loop=False)
        await first.start()
        await second.start()
        try:
            done = await first.drain()
            duplicate = await second.drain()
        finally:
            await first.stop()
            await second.stop()

        assert len(done) == 1
        assert duplicate == []
        assert harness.world.adapter.started == 1

    async def test_a_skipped_listing_does_not_stop_the_batch(
        self, tmp_path: Path
    ) -> None:
        from app.agent.errors import CaptchaEncountered

        world = build_world(tmp_path, guard_error=CaptchaEncountered("hcaptcha"))
        harness = Harness(world)
        world.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        second = world.db.enqueue_job(
            "https://www.linkedin.com/jobs/view/2/", Board.LINKEDIN
        )
        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            results = await worker.drain()
        finally:
            await worker.stop()

        assert [result.status for result in results] == [
            RunStatus.SKIPPED,
            RunStatus.SKIPPED,
        ]
        item = world.db.get_queue_item(second)
        assert item is not None
        assert item.state is QueueState.SKIPPED

    async def test_an_unexpected_error_is_contained_and_reported(
        self, tmp_path: Path
    ) -> None:
        """A run that raises out of the graph must not end the worker.

        The queue item is left failed with the reason recorded, and the
        next item still runs.
        """
        world = build_world(tmp_path)
        harness = Harness(world)
        failing = world.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        following = world.db.enqueue_job(
            "https://www.linkedin.com/jobs/view/2/", Board.LINKEDIN
        )
        worker = harness.build_worker(run_loop=False)
        await worker.start()

        original = worker.runner.run_application
        calls: list[int] = []

        async def explode(queue_id: int) -> Any:
            calls.append(queue_id)
            if queue_id == failing:
                raise RuntimeError("the runner itself fell over")
            return await original(queue_id)

        worker.runner.run_application = explode  # type: ignore[method-assign]
        try:
            results = await worker.drain()
        finally:
            await worker.stop()

        assert calls == [failing, following]
        assert [result.queue_id for result in results] == [failing, following]
        assert results[0].status is RunStatus.FAILED
        item = world.db.get_queue_item(failing)
        assert item is not None
        assert item.state is QueueState.FAILED
        assert item.error_reason == "worker_error"

    async def test_the_runner_is_unavailable_before_the_worker_starts(
        self, harness: Harness
    ) -> None:
        worker = harness.build_worker(run_loop=False)
        with pytest.raises(WorkerNotReady):
            _ = worker.runner

    async def test_stopping_twice_closes_the_browser_once(
        self, harness: Harness
    ) -> None:
        worker = harness.build_worker(run_loop=False)
        await worker.start()
        await worker.stop()
        await worker.stop()

        assert harness.sessions[0].closes == 1

    async def test_a_running_worker_exposes_the_shipped_runner_type(
        self, harness: Harness
    ) -> None:
        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            assert isinstance(worker.runner, ApplicationRunner)
        finally:
            await worker.stop()


class TestServiceUnavailable:
    def test_a_request_before_the_worker_is_ready_is_refused(
        self, harness: Harness
    ) -> None:
        class NeverReady(ApplicationWorker):
            @property
            def runner(self) -> ApplicationRunner:
                raise WorkerNotReady("the browser is still starting")

        def factory(settings: Settings) -> ApplicationWorker:
            worker = NeverReady(
                settings,
                db=harness.world.db,
                session_factory=harness.session_factory,
                dependencies_factory=lambda _s, _d, _c: harness.world.deps,
                checkpointer_path=harness.world.checkpoint_path,
                run_loop=False,
            )
            harness.workers.append(worker)
            return worker

        with TestClient(create_app(harness.settings, factory), client=LOOPBACK) as client:
            response = client.post("/applications/1/approve", json={})

        assert response.status_code == 503
        assert response.json()["error"]["kind"] == "worker_unavailable"
