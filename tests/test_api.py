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

from app.agent.browser_actions import (
    PlaywrightFieldWriter,
    PlaywrightPageGuard,
    PlaywrightSubmitter,
)
from app.agent.errors import SubmitNotAuthorized
from app.agent.form_scanner import FormField
from app.agent.graph import (
    ApplicationRunner,
    RunStatus,
    SubmitAuthorization,
    SubmitPermit,
    thread_id_for,
)
from app.config import Settings
from app.main import (
    ApplicationWorker,
    ArtifactScreenshotter,
    WorkerNotReady,
    build_dependencies,
    create_app,
    insecure_binding_reason,
)
from app.storage.db import Database
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


class TestApplicationListing:
    """`GET /applications` is what a reviewer's queue is built from."""

    def test_an_empty_installation_lists_nothing(self, client: TestClient) -> None:
        response = client.get("/applications")
        assert response.status_code == 200
        assert response.json() == []

    def test_every_application_is_listed_with_its_run(
        self, client: TestClient, harness: Harness
    ) -> None:
        first = queue_one(client, "https://www.linkedin.com/jobs/view/1/")
        second = queue_one(client, "https://www.linkedin.com/jobs/view/2/")
        staged_application(harness, first["queue_id"])
        staged_application(harness, second["queue_id"])

        body = client.get("/applications").json()

        assert [view["queue_id"] for view in body] == [
            first["queue_id"],
            second["queue_id"],
        ]
        assert all(view["listing_url"] for view in body)
        assert all(view["awaiting_decision"] for view in body)

    def test_the_status_filter_narrows_the_list(
        self, client: TestClient, harness: Harness
    ) -> None:
        queued = queue_one(client)
        application_id = staged_application(harness, queued["queue_id"])
        client.post(f"/applications/{application_id}/reject", json={})

        awaiting = client.get(
            "/applications", params={"status": ApplicationStatus.AWAITING_APPROVAL.value}
        ).json()
        rejected = client.get(
            "/applications", params={"status": ApplicationStatus.REJECTED.value}
        ).json()

        assert awaiting == []
        assert [view["application"]["id"] for view in rejected] == [application_id]

    def test_an_unknown_status_is_a_validation_error(
        self, client: TestClient
    ) -> None:
        """Not an empty list: a typo would otherwise read as "none of those"."""
        response = client.get("/applications", params={"status": "nearly_submitted"})

        assert response.status_code == 422
        assert response.json()["error"]["kind"] == "validation_error"


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

    @pytest.mark.parametrize(
        "presented",
        [
            "Bearer pässwörd-with-ümlauts".encode("utf-8"),
            "Bearer 🔑".encode("utf-8"),
            b"Bearer \xff\xfe\x00not-utf-8-at-all",
        ],
    )
    def test_a_token_that_is_not_ascii_is_refused_not_crashed(
        self, tmp_path: Path, presented: bytes
    ) -> None:
        """`hmac.compare_digest` rejects non-ASCII `str` by raising.

        Header bytes are attacker-controlled and Starlette hands them over
        latin-1 decoded, so comparing them as text handed anyone a 500 for
        the price of one accented byte. Compared as bytes there is nothing
        to raise about: it is simply the wrong token. The third case is not
        valid UTF-8 in any encoding, which is exactly what a hostile client
        would send to find out what happens.
        """
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(
            headers={"Authorization": presented}, raise_server_exceptions=False
        ) as client:
            response = client.get("/health")

        assert response.status_code == 401
        assert response.json()["error"]["kind"] == "invalid_credentials"

    def test_a_non_ascii_token_still_authenticates_its_holder(
        self, tmp_path: Path
    ) -> None:
        """Comparing bytes must not break tokens that are merely unusual."""
        unicode_token = "sésame-ouvre-toi-🔑"
        harness = Harness(build_world(tmp_path), token=unicode_token)

        with harness.client() as client:
            response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["auth"] == "token"

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer",
            "Bearer ",
            "Basic c2VjcmV0",
            "  ",
            "Bearer a b c",
            "\x00\x01",
            "Bearer " + "x" * 10_000,
        ],
    )
    def test_a_malformed_authorization_header_is_a_json_refusal(
        self, tmp_path: Path, header: str
    ) -> None:
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(
            headers={"Authorization": header}, raise_server_exceptions=False
        ) as client:
            response = client.get("/health")

        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/json")
        assert set(response.json()["error"]) == {"kind", "message"}

    def test_no_refusal_ever_repeats_the_configured_token(
        self, tmp_path: Path
    ) -> None:
        """A rejection that echoes the secret is a rejection that leaks it."""
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(
            headers={"Authorization": "Bearer wrong"}, raise_server_exceptions=False
        ) as client:
            responses = [
                client.get("/health"),
                client.post("/queue", json={"listing_url": LISTING_URL, "board": "linkedin"}),
                client.post("/applications/1/approve", json={}),
            ]

        for response in responses:
            assert response.status_code == 401
            assert TOKEN not in response.text
            assert TOKEN not in str(dict(response.headers))

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

    async def test_a_listing_abandoned_by_a_dead_worker_is_picked_up_again(
        self, harness: Harness
    ) -> None:
        """A row left `running` by a killed process is nobody's, forever.

        `claim_next_pending` claims pending rows only, so without a sweep
        at startup the listing is silently never applied to and the
        operator has no signal that anything went wrong. The sweep runs
        once, when the worker starts, which is the moment a previous
        process is known to be gone.
        """
        queue_id = harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        harness.db.claim_next_pending()  # the worker that then died

        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            results = await worker.drain()
        finally:
            await worker.stop()

        assert [result.queue_id for result in results] == [queue_id]
        assert results[0].awaiting_approval

    async def test_a_listing_whose_lease_has_lapsed_is_picked_up_again(
        self, harness: Harness
    ) -> None:
        """The commonest shape of the crash: killed while holding the lease.

        The lease outlives the process by design — that is what stops a
        successor from barging in on a slow submit — so the sweep has to
        read the expiry rather than the row's existence. Reading only
        "is there a lease" would strand exactly the listings this exists
        to recover, and would do it silently.
        """
        from datetime import datetime, timedelta, timezone

        queue_id = harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        harness.db.claim_next_pending()
        harness.db.acquire_lease(
            thread_id_for(queue_id),
            "dead-worker",
            ttl=timedelta(seconds=60),
            now=datetime.now(timezone.utc) - timedelta(seconds=120),
        )

        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            results = await worker.drain()
        finally:
            await worker.stop()

        assert [result.queue_id for result in results] == [queue_id]
        assert harness.world.adapter.started == 1

    async def test_a_recovered_listing_says_why_it_came_back(
        self, harness: Harness
    ) -> None:
        """The reason is the only trace the crash leaves on the queue row."""
        queue_id = harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        harness.db.claim_next_pending()

        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            item = harness.db.get_queue_item(queue_id)
            assert item is not None
            assert item.state is QueueState.PENDING
            assert item.error_reason == "worker_abandoned"
        finally:
            await worker.stop()

    async def test_a_listing_another_worker_is_running_is_left_alone(
        self, harness: Harness
    ) -> None:
        """A live lease means a live worker, whatever the queue row says."""
        from datetime import datetime, timedelta, timezone

        queue_id = harness.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        harness.db.claim_next_pending()
        harness.db.acquire_lease(
            thread_id_for(queue_id),
            "another-worker",
            ttl=timedelta(seconds=120),
            now=datetime.now(timezone.utc),
        )

        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            assert await worker.drain() == []
        finally:
            await worker.stop()

        item = harness.db.get_queue_item(queue_id)
        assert item is not None
        assert item.state is QueueState.RUNNING
        assert harness.world.adapter.started == 0

    async def test_a_running_worker_exposes_the_shipped_runner_type(
        self, harness: Harness
    ) -> None:
        worker = harness.build_worker(run_loop=False)
        await worker.start()
        try:
            assert isinstance(worker.runner, ApplicationRunner)
        finally:
            await worker.stop()


class TestTheShippedWiring:
    """What `build_dependencies` actually hands the graph in production.

    Every other test in this file replaces the browser-facing components
    with fakes, which is what makes them fast and safe — and also means none
    of them ever look at the wiring a real run would get. This one does,
    without a browser: `build_dependencies` takes the context as an opaque
    object, so a stand-in is enough to inspect what comes back.

    The writer and the submitter used to be refusing stubs. They are real
    now, which moves the burden of this test: instead of proving the
    dangerous part is absent, it proves the safety that makes shipping it
    defensible. `AUTO_SUBMIT` is off, the writer shares the trigger's
    scanner so the control it types into is the control the key was derived
    from, and the submitter refuses an application nobody released — checked
    against a page that raises if it is touched at all, so the refusal
    cannot be coming from somewhere further in.
    """

    def _dependencies(self, tmp_path: Path, **overrides: Any) -> Any:
        settings = Settings(
            _env_file=None,
            sqlite_path=tmp_path / "jobs.db",
            artifacts_path=tmp_path / "artifacts",
            **overrides,
        )
        db = Database(settings)
        db.initialize()
        return build_dependencies(settings, db, object())

    def test_the_field_writer_is_the_production_one(self, tmp_path: Path) -> None:
        assert isinstance(self._dependencies(tmp_path).writer, PlaywrightFieldWriter)

    def test_the_writer_types_into_the_control_the_scanner_found(
        self, tmp_path: Path
    ) -> None:
        """One scanner, so one derivation of a control's stable key.

        A writer with a scanner of its own would hold a different HMAC key
        and re-derive a different key for the same control, so its
        provenance check would refuse every write. Sharing the trigger's
        scanner is what makes the check meaningful rather than fatal.
        """
        deps = self._dependencies(tmp_path)

        assert deps.writer.scanner is deps.trigger.scanner

    def test_the_page_guard_is_the_production_one(self, tmp_path: Path) -> None:
        assert isinstance(self._dependencies(tmp_path).guard, PlaywrightPageGuard)

    def test_the_submitter_is_the_production_one(self, tmp_path: Path) -> None:
        assert isinstance(self._dependencies(tmp_path).submitter, PlaywrightSubmitter)

    async def test_the_submitter_refuses_an_application_nobody_released(
        self, tmp_path: Path
    ) -> None:
        deps = self._dependencies(tmp_path)
        unreleased = SubmitAuthorization(
            application_id=1,
            thread_id="application-1",
            decision="",
            decided_at="",
            gate="",
        )

        with pytest.raises(SubmitNotAuthorized):
            await deps.submitter.submit(
                _UntouchablePage(), unreleased, _unspendable_permit()
            )

    async def test_the_submitter_refuses_a_rejected_application(
        self, tmp_path: Path
    ) -> None:
        deps = self._dependencies(tmp_path)
        rejected = SubmitAuthorization(
            application_id=1,
            thread_id="application-1",
            decision=ApprovalDecision.REJECTED.value,
            decided_at="2026-08-19T00:00:00+00:00",
            gate="api",
        )

        with pytest.raises(SubmitNotAuthorized):
            await deps.submitter.submit(
                _UntouchablePage(), rejected, _unspendable_permit()
            )

    async def test_the_writer_refuses_a_control_the_applicant_must_operate(
        self, tmp_path: Path
    ) -> None:
        """A file input, refused before the page is looked at at all."""
        deps = self._dependencies(tmp_path)

        assert await deps.writer.write(_UntouchablePage(), _resume_upload(), "cv.pdf") is False

    def test_auto_submit_is_still_off_in_the_shipped_settings(
        self, tmp_path: Path
    ) -> None:
        """Wiring a submitter did not turn on submitting without a human."""
        assert self._dependencies(tmp_path).settings.auto_submit is False

    def test_every_component_the_graph_touches_is_wired(self, tmp_path: Path) -> None:
        """Otherwise this test would pass on a build that does nothing at all."""
        deps = self._dependencies(tmp_path)

        assert deps.logger is not None
        assert deps.rate_limiter is not None
        assert deps.pages is not None
        assert deps.trigger is not None
        assert deps.gap_filler is not None
        assert deps.writer is not None
        assert deps.guard is not None
        assert deps.submitter is not None
        assert deps.screenshots is not None

    def test_the_submitter_can_save_the_screenshot_it_promises(
        self, tmp_path: Path
    ) -> None:
        """An unconfirmed submission is only checkable if it left an image."""
        deps = self._dependencies(tmp_path)

        assert deps.submitter.screenshots is deps.screenshots

    def test_the_wiring_needs_no_browser_to_be_inspected(self, tmp_path: Path) -> None:
        """Guards the tests above: they would be vacuous if this raised."""
        assert self._dependencies(tmp_path) is not None


class TestTheScreenshotsAnOperatorHasToLookAt:
    """Two applications, two images. This used to be one image.

    Every diagnostic was written to `{name}.png`, and the names are the
    handful of outcomes the graph and the submitter photograph — so the
    second application to be refused overwrote the evidence for the first,
    and the row telling an operator to check a screenshot pointed at
    somebody else's page.
    """

    class Photographable:
        """A page that records where it was asked to write."""

        def __init__(self) -> None:
            self.paths: list[str] = []

        async def screenshot(self, path: str) -> None:
            self.paths.append(path)
            Path(path).write_bytes(b"png")

    async def test_two_captures_of_one_name_are_two_files(
        self, tmp_path: Path
    ) -> None:
        shots = ArtifactScreenshotter(tmp_path / "artifacts")
        page = self.Photographable()

        first = await shots.capture(page, "application-1-unconfirmed")
        second = await shots.capture(page, "application-2-unconfirmed")

        assert first != second
        assert len(list((tmp_path / "artifacts").glob("*.png"))) == 2

    async def test_the_same_application_photographed_twice_keeps_both(
        self, tmp_path: Path
    ) -> None:
        """A retried application's first attempt is the interesting one."""
        shots = ArtifactScreenshotter(tmp_path / "artifacts")
        page = self.Photographable()

        first = await shots.capture(page, "application-1-refused")
        second = await shots.capture(page, "application-1-refused")

        assert first != second
        assert Path(first).exists()
        assert Path(second).exists()

    async def test_the_file_still_says_which_application_and_outcome(
        self, tmp_path: Path
    ) -> None:
        shots = ArtifactScreenshotter(tmp_path / "artifacts")

        saved = await shots.capture(self.Photographable(), "application-7-refused")

        assert saved is not None
        assert Path(saved).name.startswith("application-7-refused")
        assert Path(saved).suffix == ".png"

    async def test_a_name_that_looks_like_a_path_stays_in_the_directory(
        self, tmp_path: Path
    ) -> None:
        """The name comes from a thread id, which comes out of the database."""
        artifacts = tmp_path / "artifacts"
        shots = ArtifactScreenshotter(artifacts)

        saved = await shots.capture(self.Photographable(), "../../etc/passwd")

        assert saved is not None
        assert Path(saved).parent == artifacts


def _unspendable_permit() -> SubmitPermit:
    """The one press, which neither refusal above may spend.

    An unreleased application is still submittable once somebody releases
    it, so a refusal that claimed the press would leave it permanently
    unsendable.
    """

    def claim() -> None:
        raise AssertionError("the press was claimed, so a refusal spent it")

    return SubmitPermit(1, claim)


class _UntouchablePage:
    """A page that fails the test if anything is asked of it.

    Both refusals above happen before any page work, and a page that
    answered questions would let a future change move the refusal later
    without this test noticing.
    """

    def __getattr__(self, attribute: str) -> Any:
        raise AssertionError(
            f"the page was asked for {attribute!r}, so the refusal came too late"
        )


def _resume_upload() -> FormField:
    return FormField(
        key="resume",
        frame_url="https://boards.greenhouse.io/acme/jobs/1",
        form="form#application",
        control_id="resume",
        name="resume",
        field_type="file",
        label="Resume",
        tag="input",
        required=True,
        disabled=False,
        visible=True,
        filled=False,
        free_text=False,
        value_digest="digest:resume:empty",
    )


class TestUnexpectedFailures:
    """An unhandled error is still an answer, and still says nothing.

    Without a catch-all the client gets Starlette's bare
    `Internal Server Error` — not the envelope every other refusal uses —
    and in a debug configuration a traceback naming files, versions, and
    whatever happened to be in the exception's message.
    """

    def _exploding_client(self, harness: Harness, message: str) -> TestClient:
        class Exploding(ApplicationWorker):
            @property
            def runner(self) -> ApplicationRunner:
                raise ValueError(message)

        def factory(settings: Settings) -> ApplicationWorker:
            worker = Exploding(
                settings,
                db=harness.world.db,
                session_factory=harness.session_factory,
                dependencies_factory=lambda _s, _d, _c: harness.world.deps,
                checkpointer_path=harness.world.checkpoint_path,
                run_loop=False,
            )
            harness.workers.append(worker)
            return worker

        return TestClient(
            create_app(harness.settings, factory),
            client=LOOPBACK,
            raise_server_exceptions=False,
        )

    def test_an_unexpected_error_answers_in_the_usual_envelope(
        self, harness: Harness
    ) -> None:
        with self._exploding_client(harness, "boom") as client:
            response = client.get("/health")

        assert response.status_code == 500
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {
            "error": {
                "kind": "internal_error",
                "message": (
                    "the control plane could not complete this request. The "
                    "reason has been logged."
                ),
            }
        }

    def test_an_unexpected_error_reveals_nothing_about_itself(
        self, harness: Harness
    ) -> None:
        """Exception text carries connection strings and paths often enough."""
        secret = "psycopg://ada:hunter2@db.internal/records"

        with self._exploding_client(harness, secret) as client:
            response = client.get("/health")

        assert "hunter2" not in response.text
        assert "ValueError" not in response.text
        assert "Traceback" not in response.text

    def test_the_reason_is_logged_where_an_operator_can_read_it(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silent to the caller is not the same as silent."""
        with caplog.at_level("ERROR", logger="app.main"):
            with self._exploding_client(harness, "hunter2") as client:
                client.get("/health")

        assert "hunter2" in caplog.text


class TestDocumentationRoutes:
    """The schema is a route like any other, and is not exempt from auth.

    FastAPI mounts `/docs`, `/redoc`, and `/openapi.json` as plain Starlette
    routes, so an app-level dependency does not touch them: they were served
    to anyone who could reach the port, describing every route and body
    shape. With a token configured they are not served at all, because the
    browser fetching the schema cannot present a bearer token and a docs
    page that cannot load its own schema is worse than an absent one.
    """

    def test_without_a_token_the_docs_are_served_to_loopback(
        self, client: TestClient
    ) -> None:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert "/applications/{application_id}/approve" in schema.json()["paths"]

    def test_without_a_token_the_schema_is_still_loopback_only(
        self, harness: Harness
    ) -> None:
        with harness.client(client=("203.0.113.7", 40000)) as client:
            response = client.get("/openapi.json")

        assert response.status_code == 403
        assert response.json()["error"]["kind"] == "not_loopback"

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_with_a_token_the_docs_are_not_served_at_all(
        self, tmp_path: Path, path: str
    ) -> None:
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client() as client:
            response = client.get(path)

        assert response.status_code == 404
        assert response.json()["error"]["kind"] == "http_404"

    def test_an_unauthenticated_caller_learns_nothing_from_the_docs(
        self, tmp_path: Path
    ) -> None:
        harness = Harness(build_world(tmp_path), token=TOKEN)

        with harness.client(headers={}) as client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                response = client.get(path)
                assert response.status_code in {401, 404}
                assert "approve" not in response.text


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
