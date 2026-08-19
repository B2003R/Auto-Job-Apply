"""Tests for the batch runner and the log exporter.

The HTTP mode is exercised against the real control plane through
`TestClient`, so "the CLI talks to the API" is proven rather than mocked;
the local mode is exercised against the real `ApplicationWorker` with the
same faked browser side. No socket is opened, no browser is launched, and
nothing is submitted anywhere.
"""

from __future__ import annotations

import csv
import json
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from app.agent.graph import thread_id_for
from app.config import Settings
from app.main import ApplicationWorker
from app.storage.models import (
    ApplicationStatus,
    ApprovalDecision,
    Board,
    FieldSource,
    QueueState,
)
from scripts import export_log, run_batch
from tests.api_support import (
    LISTING_URL,
    TOKEN,
    Harness,
    gap_world,
    staged_application,
)

SECOND_LISTING = "https://www.linkedin.com/jobs/view/2/"


class Console:
    """Collects what a command printed, for asserting on the output."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.lines: list[str] = []
        self.answers = list(answers or [])

    def write(self, line: str) -> None:
        self.lines.append(str(line))

    def read(self) -> str:
        return self.answers.pop(0) if self.answers else ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def json(self) -> Any:
        return json.loads(self.text)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(gap_world(tmp_path))


@pytest.fixture
def served(harness: Harness) -> Iterator[Harness]:
    """The control plane, running, with a client factory pointed at it."""
    with harness.client() as client:
        harness.http = client  # type: ignore[attr-defined]
        yield harness


def run_cli(harness: Harness, *argv: str, console: Console | None = None) -> int:
    """Drive the batch runner against this harness's running API."""
    out = console or Console()

    def client_factory(
        api_url: str, token: str | None
    ) -> AbstractContextManager[httpx.Client]:
        # Borrowed, not owned: the running fixture closes this client, and
        # a command that closed it would shut the server down mid-test.
        return nullcontext(harness.http)  # type: ignore[attr-defined]

    return run_batch.main(
        list(argv),
        settings=harness.settings,
        client_factory=client_factory,
        writer=out.write,
        reader=out.read,
    )


class TestBatchParser:
    """The parser refuses ambiguous or unsafe invocations before acting."""

    def test_a_board_is_required_to_queue(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run_batch.build_parser().parse_args(["queue", LISTING_URL])
        assert excinfo.value.code == 2

    def test_an_unknown_board_is_refused_by_the_parser(self) -> None:
        with pytest.raises(SystemExit):
            run_batch.build_parser().parse_args(
                ["queue", LISTING_URL, "--board", "monster"]
            )

    def test_a_board_is_parsed_into_the_typed_enum(self) -> None:
        args = run_batch.build_parser().parse_args(
            ["queue", LISTING_URL, "--board", "linkedin"]
        )
        assert args.board is Board.LINKEDIN
        assert args.urls == [LISTING_URL]

    def test_local_and_an_explicit_api_are_mutually_exclusive(self) -> None:
        """Naming both hides which one the command actually used."""
        with pytest.raises(SystemExit):
            run_batch.build_parser().parse_args(
                ["--local", "--api", "http://127.0.0.1:9", "status", "1"]
            )

    def test_the_default_api_url_comes_from_settings(self) -> None:
        settings = Settings(_env_file=None, api_host="127.0.0.1", api_port=9123)
        assert run_batch.default_api_url(settings) == "http://127.0.0.1:9123"

    def test_an_unspecified_host_is_addressed_as_loopback(self) -> None:
        """0.0.0.0 is a bind address, not somewhere a client can connect."""
        settings = Settings(_env_file=None, api_host="0.0.0.0", api_port=9123)
        assert run_batch.default_api_url(settings) == "http://127.0.0.1:9123"

    def test_no_subcommand_is_a_usage_error(self) -> None:
        assert run_batch.main([], settings=Settings(_env_file=None)) == 2


class TestHttpBatch:
    def test_queueing_prints_the_accepted_listing(self, served: Harness) -> None:
        console = Console()

        code = run_cli(
            served, "queue", LISTING_URL, "--board", "linkedin", console=console
        )

        assert code == 0
        assert "queued" in console.text
        assert [item.listing_url for item in served.db.list_queue_items()] == [
            LISTING_URL
        ]

    def test_queueing_several_listings_queues_all_of_them(
        self, served: Harness
    ) -> None:
        code = run_cli(
            served, "queue", LISTING_URL, SECOND_LISTING, "--board", "linkedin"
        )

        assert code == 0
        assert len(served.db.list_queue_items()) == 2

    def test_json_output_is_machine_readable(self, served: Harness) -> None:
        console = Console()

        run_cli(
            served,
            "--json",
            "queue",
            LISTING_URL,
            "--board",
            "linkedin",
            console=console,
        )

        payload = console.json()
        assert payload[0]["listing_url"] == LISTING_URL
        assert payload[0]["thread_id"] == thread_id_for(payload[0]["queue_id"])

    def test_an_untrusted_url_is_reported_and_not_queued(
        self, served: Harness
    ) -> None:
        console = Console()

        code = run_cli(
            served,
            "queue",
            "https://linkedin.com.evil.test/jobs/1",
            "--board",
            "linkedin",
            console=console,
        )

        assert code == 1
        assert "untrusted_listing_url" in console.text
        assert served.db.list_queue_items() == []

    def test_status_reports_a_pending_decision(self, served: Harness) -> None:
        queue_id = served.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        served.workers[0].wake()
        application_id = staged_application(served, queue_id)
        console = Console()

        code = run_cli(served, "--json", "status", str(queue_id), console=console)

        assert code == 0
        body = console.json()
        assert body["application"]["id"] == application_id
        assert body["awaiting_decision"] is True

    def test_status_for_an_unknown_run_is_reported_not_raised(
        self, served: Harness
    ) -> None:
        console = Console()

        code = run_cli(served, "status", "999", console=console)

        assert code == 1
        assert "unknown_queue_item" in console.text

    def test_approving_over_http_submits(self, served: Harness) -> None:
        queue_id = served.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        served.workers[0].wake()
        application_id = staged_application(served, queue_id)
        console = Console()

        code = run_cli(
            served, "approve", str(application_id), "--note", "good fit", console=console
        )

        assert code == 0
        assert "submitted" in console.text
        approval = served.db.get_approval(application_id)
        assert approval is not None
        assert approval.decision is ApprovalDecision.APPROVED
        assert approval.note == "good fit"
        assert served.world.submitter.calls == 1

    def test_rejecting_over_http_does_not_submit(self, served: Harness) -> None:
        queue_id = served.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        served.workers[0].wake()
        application_id = staged_application(served, queue_id)

        code = run_cli(served, "reject", str(application_id))

        assert code == 0
        assert served.world.submitter.calls == 0

    def test_reversing_a_decision_is_reported_as_a_conflict(
        self, served: Harness
    ) -> None:
        queue_id = served.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        served.workers[0].wake()
        application_id = staged_application(served, queue_id)
        run_cli(served, "reject", str(application_id))
        console = Console()

        code = run_cli(served, "approve", str(application_id), console=console)

        assert code == 1
        assert "approval_conflict" in console.text

    def test_pending_lists_only_applications_awaiting_a_decision(
        self, served: Harness
    ) -> None:
        queue_id = served.db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        served.workers[0].wake()
        application_id = staged_application(served, queue_id)
        console = Console()

        code = run_cli(served, "--json", "pending", console=console)

        assert code == 0
        assert [entry["application"]["id"] for entry in console.json()] == [
            application_id
        ]

    def test_run_waits_for_each_listing_to_reach_a_decision_point(
        self, served: Harness
    ) -> None:
        console = Console()

        code = run_cli(
            served,
            "run",
            LISTING_URL,
            "--board",
            "linkedin",
            "--timeout",
            "10",
            console=console,
        )

        assert code == 0
        assert "awaiting" in console.text
        item = served.db.list_queue_items()[0]
        record = served.db.get_application_by_thread(thread_id_for(item.id))
        assert record is not None
        assert record.status is ApplicationStatus.AWAITING_APPROVAL

    def test_a_token_is_sent_when_one_is_configured(self, tmp_path: Path) -> None:
        """The CLI must authenticate, or the server will refuse it."""
        harness = Harness(gap_world(tmp_path), token=TOKEN)
        seen: list[str | None] = []

        with harness.client(headers={}) as raw:

            def client_factory(
                api_url: str, token: str | None
            ) -> AbstractContextManager[httpx.Client]:
                seen.append(token)
                raw.headers["Authorization"] = f"Bearer {token}"
                return nullcontext(raw)

            code = run_batch.main(
                ["queue", LISTING_URL, "--board", "linkedin"],
                settings=harness.settings,
                client_factory=client_factory,
                writer=lambda line: None,
            )

        assert seen == [TOKEN]
        assert code == 0

    def test_a_refused_token_is_reported_rather_than_raised(
        self, tmp_path: Path
    ) -> None:
        harness = Harness(gap_world(tmp_path), token=TOKEN)
        console = Console()

        with harness.client(headers={"Authorization": "Bearer wrong"}) as raw:
            code = run_batch.main(
                ["queue", LISTING_URL, "--board", "linkedin"],
                settings=harness.settings,
                client_factory=lambda api_url, token: nullcontext(raw),
                writer=console.write,
            )

        assert code == 1
        assert "invalid_credentials" in console.text

    def test_an_unreachable_api_is_reported_as_a_connection_problem(
        self, harness: Harness
    ) -> None:
        """No server is started here; the CLI must not traceback."""
        console = Console()

        def refusing(
            api_url: str, token: str | None
        ) -> AbstractContextManager[httpx.Client]:
            def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused", request=request)

            return httpx.Client(
                base_url=api_url, transport=httpx.MockTransport(handler)
            )

        code = run_batch.main(
            ["status", "1"],
            settings=harness.settings,
            client_factory=refusing,
            writer=console.write,
        )

        assert code == 1
        assert "could not reach" in console.text


class TestLocalBatch:
    """`--local` runs an in-process worker instead of talking to a server."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--local", "approve", "1"],
            ["--local", "reject", "1"],
            ["--local", "status", "1"],
            ["--local", "pending"],
        ],
    )
    def test_only_a_batch_can_be_run_locally(
        self, harness: Harness, argv: list[str]
    ) -> None:
        """A decision needs the process holding the staged tab.

        `--local` starts a worker for one batch and stops it again, so by
        the time a second command could run there is no tab to submit and
        no worker to submit it. Accepting these would mean approving an
        application against a browser that closed — and the honest failure
        for that is `PageUnavailable` a long way from the mistake.
        """
        console = Console()

        code = run_batch.main(
            argv,
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=console.write,
        )

        assert code == 2
        assert "needs the control plane" in console.text
        assert harness.sessions == []

    def test_prompting_over_http_is_refused_rather_than_ignored(
        self, harness: Harness
    ) -> None:
        """A console prompt cannot decide a tab in the server's process.

        Ignoring `--prompt` here would be worse than refusing it: the
        operator asked to be consulted about each application, would be
        shown nothing, and would reasonably read the silent success as
        "there was nothing to decide".
        """
        console = Console()

        code = run_batch.main(
            ["run", LISTING_URL, "--board", "linkedin", "--prompt"],
            settings=harness.settings,
            client_factory=_no_http,
            writer=console.write,
        )

        assert code == 2
        assert "--prompt needs --local" in console.text
        assert harness.db.list_queue_items() == []

    def test_local_queues_and_stages_without_a_server(
        self, harness: Harness
    ) -> None:
        console = Console()

        code = run_batch.main(
            ["--local", "run", LISTING_URL, "--board", "linkedin"],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=console.write,
        )

        assert code == 0
        assert "awaiting" in console.text
        item = harness.db.list_queue_items()[0]
        record = harness.db.get_application_by_thread(thread_id_for(item.id))
        assert record is not None
        assert record.status is ApplicationStatus.AWAITING_APPROVAL
        assert harness.sessions[0].starts == 1
        assert harness.sessions[0].closes == 1

    def test_local_processes_every_queued_listing(self, harness: Harness) -> None:
        code = run_batch.main(
            ["--local", "run", LISTING_URL, SECOND_LISTING, "--board", "linkedin"],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=lambda line: None,
        )

        assert code == 0
        assert [item.state for item in harness.db.list_queue_items()] == [
            QueueState.RUNNING,
            QueueState.RUNNING,
        ]

    def test_local_also_drains_what_was_already_queued(
        self, harness: Harness
    ) -> None:
        harness.db.enqueue_job(SECOND_LISTING, Board.LINKEDIN)

        run_batch.main(
            ["--local", "run"],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=lambda line: None,
        )

        record = harness.db.get_application_by_thread(thread_id_for(1))
        assert record is not None
        assert record.status is ApplicationStatus.AWAITING_APPROVAL

    def test_local_prompting_approves_through_the_shared_gate(
        self, harness: Harness
    ) -> None:
        console = Console(answers=["approve", "looks right"])

        code = run_batch.main(
            ["--local", "run", LISTING_URL, "--board", "linkedin", "--prompt"],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=console.write,
            reader=console.read,
        )

        assert code == 0
        assert harness.world.submitter.calls == 1
        record = harness.db.get_application_by_thread(thread_id_for(1))
        assert record is not None
        assert record.status is ApplicationStatus.SUBMITTED
        approval = harness.db.get_approval(record.id)
        assert approval is not None
        assert approval.note == "looks right"

    def test_local_prompting_can_reject(self, harness: Harness) -> None:
        console = Console(answers=["reject", ""])

        run_batch.main(
            ["--local", "run", LISTING_URL, "--board", "linkedin", "--prompt"],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=console.write,
            reader=console.read,
        )

        assert harness.world.submitter.calls == 0
        record = harness.db.get_application_by_thread(thread_id_for(1))
        assert record is not None
        assert record.status is ApplicationStatus.REJECTED

    def test_without_prompting_nothing_is_decided(self, harness: Harness) -> None:
        """The default local run stops at the gate, like every other path."""
        run_batch.main(
            ["--local", "run", LISTING_URL, "--board", "linkedin"],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=lambda line: None,
        )

        record = harness.db.get_application_by_thread(thread_id_for(1))
        assert record is not None
        assert harness.db.get_approval(record.id) is None

    def test_the_local_actor_names_the_operator_account(
        self, harness: Harness
    ) -> None:
        console = Console(answers=["approve", ""])

        run_batch.main(
            [
                "--local",
                "run",
                LISTING_URL,
                "--board",
                "linkedin",
                "--prompt",
                "--actor",
                "ada@example.com",
            ],
            settings=harness.settings,
            worker_factory=lambda settings: harness.build_worker(run_loop=False),
            client_factory=_no_http,
            writer=console.write,
            reader=console.read,
        )

        record = harness.db.get_application_by_thread(thread_id_for(1))
        assert record is not None
        approval = harness.db.get_approval(record.id)
        assert approval is not None
        assert approval.actor == "ada@example.com"

    def test_the_browser_is_closed_even_when_a_run_fails(
        self, harness: Harness
    ) -> None:
        class Exploding(ApplicationWorker):
            async def drain(self) -> Any:
                raise RuntimeError("the queue could not be drained")

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

        console = Console()
        code = run_batch.main(
            ["--local", "run", LISTING_URL, "--board", "linkedin"],
            settings=harness.settings,
            worker_factory=factory,
            client_factory=_no_http,
            writer=console.write,
        )

        assert code == 1
        assert harness.sessions[0].closes == 1


def _no_http(api_url: str, token: str | None) -> AbstractContextManager[httpx.Client]:
    raise AssertionError("a local run must not open an HTTP client")


class TestExportLog:
    """The exporter reads storage directly; values stay redacted by default."""

    @pytest.fixture
    def exported(self, tmp_path: Path) -> Settings:
        settings = Settings(
            _env_file=None,
            sqlite_path=tmp_path / "jobs.db",
            artifacts_path=tmp_path / "artifacts",
        )
        from app.storage.db import Database

        db = Database(settings)
        db.initialize()
        queue_id = db.enqueue_job(LISTING_URL, Board.LINKEDIN)
        application_id = db.create_application(
            queue_id=queue_id,
            thread_id=thread_id_for(queue_id),
            status=ApplicationStatus.SUBMITTED,
            ats="greenhouse",
            trigger_tier=1,
            model_cost=0.25,
        )
        db.save_application_field(
            application_id=application_id,
            stable_key="frame|input|email",
            source=FieldSource.JOBRIGHT,
            required=True,
            filled=True,
            value="ada@example.com",
            metadata={"label": "Email"},
        )
        db.record_approval(
            application_id=application_id,
            decision=ApprovalDecision.APPROVED,
            actor="ada@example.com",
            note="applied",
        )
        return settings

    def test_json_export_describes_each_application(
        self, exported: Settings
    ) -> None:
        console = Console()

        code = export_log.main(
            ["--format", "json"], settings=exported, writer=console.write
        )

        assert code == 0
        payload = console.json()
        entry = payload["applications"][0]
        assert entry["listing_url"] == LISTING_URL
        assert entry["board"] == "linkedin"
        assert entry["status"] == "submitted"
        assert entry["ats"] == "greenhouse"
        assert entry["decision"] == "approved"
        assert entry["actor"] == "ada@example.com"

    def test_csv_export_has_one_row_per_application(
        self, exported: Settings
    ) -> None:
        console = Console()

        export_log.main(["--format", "csv"], settings=exported, writer=console.write)

        rows = list(csv.DictReader(console.text.splitlines()))
        assert len(rows) == 1
        assert rows[0]["listing_url"] == LISTING_URL
        assert rows[0]["model_cost"] == "0.25"

    def test_field_rows_carry_provenance(self, exported: Settings) -> None:
        console = Console()

        export_log.main(
            ["--format", "csv", "--fields"], settings=exported, writer=console.write
        )

        rows = list(csv.DictReader(console.text.splitlines()))
        assert rows[0]["stable_key"] == "frame|input|email"
        assert rows[0]["source"] == "jobright"

    def test_a_stored_value_is_redacted_by_default(
        self, exported: Settings
    ) -> None:
        """The row was written while logging was on; the export refuses it."""
        console = Console()

        export_log.main(
            ["--format", "json", "--fields"], settings=exported, writer=console.write
        )

        assert console.json()["applications"][0]["fields"][0]["value"] is None
        assert "ada@example.com" not in console.text.replace(
            '"actor": "ada@example.com"', ""
        )

    def test_values_need_both_the_flag_and_the_setting(
        self, exported: Settings
    ) -> None:
        console = Console()

        code = export_log.main(
            ["--format", "json", "--fields", "--include-values"],
            settings=exported,
            writer=console.write,
        )

        assert code == 0
        assert console.json()["applications"][0]["fields"][0]["value"] is None

    def test_a_flag_that_did_nothing_says_so_without_spoiling_the_output(
        self, exported: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Silence here reads as "there were no values", which is a lie.

        The notice goes to stderr rather than through the writer so that a
        JSON export stays a parseable JSON document when it is piped.
        """
        console = Console()

        export_log.main(
            ["--format", "json", "--fields", "--include-values"],
            settings=exported,
            writer=console.write,
        )

        assert "had no effect" in capsys.readouterr().err
        assert console.json()["applications"] != []

    def test_values_are_exported_when_logging_is_enabled(
        self, exported: Settings
    ) -> None:
        enabled = exported.model_copy(update={"log_field_values": True})
        console = Console()

        export_log.main(
            ["--format", "json", "--fields", "--include-values"],
            settings=enabled,
            writer=console.write,
        )

        assert (
            console.json()["applications"][0]["fields"][0]["value"]
            == "ada@example.com"
        )

    def test_an_export_can_be_narrowed_by_status(self, exported: Settings) -> None:
        console = Console()

        export_log.main(
            ["--format", "json", "--status", "failed"],
            settings=exported,
            writer=console.write,
        )

        assert console.json()["applications"] == []

    def test_an_export_can_be_written_to_a_file(
        self, exported: Settings, tmp_path: Path
    ) -> None:
        target = tmp_path / "export.json"

        code = export_log.main(
            ["--format", "json", "--output", str(target)], settings=exported
        )

        assert code == 0
        assert json.loads(target.read_text())["applications"][0]["board"] == "linkedin"

    def test_a_database_that_is_not_there_is_reported_not_created(
        self, tmp_path: Path
    ) -> None:
        """A mistyped path must not read as "you have no applications".

        `Database.initialize()` creates what it opens, so exporting from a
        path with a typo in it used to print a valid, empty export — the
        one answer an operator has no way to tell from the truth. It also
        left a stray empty database behind.
        """
        missing = tmp_path / "not-here.db"
        console = Console()

        code = export_log.main(
            ["--format", "json"],
            settings=Settings(
                _env_file=None,
                sqlite_path=missing,
                artifacts_path=tmp_path / "artifacts",
            ),
            writer=console.write,
        )

        assert code == 1
        assert str(missing) in console.text
        assert not missing.exists()

    def test_an_unwritable_output_path_is_reported_not_raised(
        self, exported: Settings, tmp_path: Path
    ) -> None:
        console = Console()

        code = export_log.main(
            ["--format", "json", "--output", str(tmp_path / "missing" / "x.json")],
            settings=exported,
            writer=console.write,
        )

        assert code == 1
        assert "could not be written" in console.text

    def test_an_unknown_status_is_a_usage_error(self, exported: Settings) -> None:
        with pytest.raises(SystemExit):
            export_log.build_parser().parse_args(["--status", "nonsense"])
