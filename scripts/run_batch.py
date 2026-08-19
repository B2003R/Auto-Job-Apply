"""Queue listings, watch them, and decide them — over HTTP or in process.

Two modes, chosen explicitly rather than guessed:

* **HTTP (the default).** The control plane already owns the browser, so
  the CLI is a thin client: it queues listings, reads status, and posts
  decisions. The worker in that process is what actually stages and
  submits. This is the mode that can approve something, because the staged
  tab lives there.
* **`--local`.** No server. The CLI starts its own `ApplicationWorker`,
  drains the queue in this process, and stops it again. Useful for a
  one-shot batch and for running the whole pipeline offline against
  fixtures.

The two are mutually exclusive on the command line — naming both would
hide which one a run actually used.

Nothing here decides anything by itself. A local run stops at the approval
gate unless `--prompt` is given, and then the decision comes from a human
at the console through the same `ApprovalService` the API uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

from app.agent.approval import ApprovalError, CliApprovalGate
from app.agent.graph import RunResult, RunStatus, thread_id_for
from app.config import Settings
from app.main import ApplicationWorker, is_loopback
from app.storage.models import Board

Writer = Callable[[str], Any]
Reader = Callable[[], str]
#: Anything that can be entered to get an HTTP client. `httpx.Client` is
#: one, which is what the default factory returns; a caller sharing a
#: client it owns hands back a `nullcontext` around it instead, so entering
#: and leaving here does not close somebody else's connection pool.
ClientFactory = Callable[[str, str | None], AbstractContextManager[httpx.Client]]
WorkerFactory = Callable[[Settings], ApplicationWorker]

#: How long `run` waits for one listing to reach a decision point before
#: giving up on watching it. The listing keeps running server-side; only
#: the watching stops.
DEFAULT_WATCH_TIMEOUT_S = 300.0
DEFAULT_POLL_S = 0.5
DEFAULT_HTTP_TIMEOUT_S = 60.0

#: Queue states and run statuses that mean "nothing more will happen here".
_SETTLED_STATES = {"completed", "failed", "skipped"}


class ControlPlaneError(Exception):
    """A refusal from the API, rendered the way the CLI reports it."""

    def __init__(self, status_code: int, kind: str, message: str) -> None:
        self.status_code = status_code
        self.kind = kind
        super().__init__(f"[{status_code} {kind}] {message}")


class ControlPlaneUnreachable(Exception):
    """The API could not be contacted at all."""

    def __init__(self, api_url: str, reason: str) -> None:
        self.api_url = api_url
        super().__init__(
            f"could not reach the control plane at {api_url}: {reason}. Start it "
            "with `python -m app.main`, or use --local to run a worker here."
        )


@dataclass
class ControlPlaneClient:
    """Typed calls onto the control plane, with typed refusals."""

    http: httpx.Client
    api_url: str

    def queue(self, listing_url: str, board: Board) -> dict[str, Any]:
        return self._call(
            "POST", "/queue", json={"listing_url": listing_url, "board": board.value}
        )

    def status(self, queue_id: int) -> dict[str, Any]:
        return self._call("GET", f"/runs/{queue_id}")

    def pending(self) -> list[dict[str, Any]]:
        payload = self._call("GET", "/applications", params={"status": "awaiting_approval"})
        return list(payload)  # type: ignore[arg-type]

    def decide(
        self, application_id: int, decision: str, note: str | None
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/applications/{application_id}/{decision}",
            json={"note": note} if note else {},
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ControlPlaneUnreachable(self.api_url, str(exc)) from exc
        if response.status_code >= 400:
            raise _refusal(response)
        return response.json()


def _refusal(response: httpx.Response) -> ControlPlaneError:
    """Turn an error response into the CLI's own error, kind included.

    The kind is what an operator greps for and what a script branches on,
    so it survives even when the body is not the shape this client expects
    (a proxy's HTML error page, say).
    """
    kind = f"http_{response.status_code}"
    message = response.text.strip()
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        kind = str(body["error"].get("kind", kind))
        message = str(body["error"].get("message", message))
    return ControlPlaneError(response.status_code, kind, message)


def default_api_url(settings: Settings) -> str:
    """Where to find the control plane, given how it was told to bind.

    `0.0.0.0` and `::` are bind addresses, not destinations: a client that
    dialled them would be relying on the operating system's goodwill, so
    they are read as loopback here.
    """
    host = settings.api_host
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host and not is_loopback(host):  # pragma: no cover - IPv6 literal
        host = f"[{host}]"
    return f"http://{host}:{settings.api_port}"


def _board(value: str) -> Board:
    try:
        return Board(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a supported board; choose one of "
            f"{', '.join(board.value for board in Board)}"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_batch",
        description="Queue, watch, and decide job applications",
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--api", default=None, help="control plane base URL (default: from settings)"
    )
    destination.add_argument(
        "--local",
        action="store_true",
        help="run a worker in this process instead of calling a server",
    )
    parser.add_argument(
        "--token", default=None, help="bearer token (default: API_TOKEN)"
    )
    parser.add_argument("--json", action="store_true", help="print JSON, not prose")
    commands = parser.add_subparsers(dest="command")

    queue = commands.add_parser("queue", help="add listings to the queue")
    queue.add_argument("urls", nargs="+")
    queue.add_argument("--board", type=_board, required=True)

    status = commands.add_parser("status", help="show one run")
    status.add_argument("queue_id", type=int)

    commands.add_parser("pending", help="list applications awaiting a decision")

    run = commands.add_parser(
        "run", help="queue listings and wait for each to reach a decision point"
    )
    run.add_argument("urls", nargs="*")
    run.add_argument("--board", type=_board, default=None)
    run.add_argument("--timeout", type=float, default=DEFAULT_WATCH_TIMEOUT_S)
    run.add_argument(
        "--prompt",
        action="store_true",
        help="ask at the console for a decision on each staged application "
        "(--local only)",
    )
    run.add_argument(
        "--actor",
        default=None,
        help="who a --prompt decision is recorded as (default: the local user)",
    )

    for name, help_text in (
        ("approve", "approve one staged application"),
        ("reject", "reject one staged application"),
    ):
        decide = commands.add_parser(name, help=help_text)
        decide.add_argument("application_id", type=int)
        decide.add_argument("--note", default=None)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    worker_factory: WorkerFactory | None = None,
    writer: Writer | None = None,
    reader: Reader | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out: Writer = writer if writer is not None else print
    resolved = settings if settings is not None else Settings()

    if args.command is None:
        parser.print_usage(file=sys.stderr)
        out("say what to do: queue, status, pending, run, approve, or reject")
        return 2

    if args.local:
        return _local(args, resolved, worker_factory, out, reader)

    api_url = args.api or default_api_url(resolved)
    token = args.token if args.token is not None else (
        resolved.api_token.get_secret_value() or None
    )
    factory = client_factory or _default_client_factory
    try:
        with factory(api_url, token) as http:
            return _remote(args, ControlPlaneClient(http, api_url), out)
    except ControlPlaneUnreachable as exc:
        out(str(exc))
        return 1
    except ControlPlaneError as exc:
        out(str(exc))
        return 1


def _default_client_factory(api_url: str, token: str | None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(
        base_url=api_url, headers=headers, timeout=DEFAULT_HTTP_TIMEOUT_S
    )


def _remote(
    args: argparse.Namespace, client: ControlPlaneClient, out: Writer
) -> int:
    if args.command == "queue":
        accepted = [client.queue(url, args.board) for url in args.urls]
        return _report(
            out,
            args.json,
            accepted,
            lambda item: f"queued {item['listing_url']} as run {item['queue_id']}",
        )

    if args.command == "status":
        return _report(out, args.json, client.status(args.queue_id), _describe_run)

    if args.command == "pending":
        return _report(out, args.json, client.pending(), _describe_run)

    if args.command in {"approve", "reject"}:
        decided = client.decide(args.application_id, args.command, args.note)
        return _report(out, args.json, decided, _describe_decision)

    if args.command == "run":
        if args.prompt:
            out("--prompt needs --local: a decision has to be made where the "
                "staged tab is, and over HTTP that is the server's process. "
                "Use `approve`/`reject` against the API instead.")
            return 2
        if args.urls and args.board is None:
            out("--board is required when `run` is given listing URLs")
            return 2
        queued = [client.queue(url, args.board) for url in args.urls]
        watched = [
            _watch(client, item["queue_id"], args.timeout, DEFAULT_POLL_S)
            for item in queued
        ]
        return _report(out, args.json, watched, _describe_run)

    return 2  # pragma: no cover - argparse rejects anything else


def _watch(
    client: ControlPlaneClient, queue_id: int, timeout: float, poll: float
) -> dict[str, Any]:
    """Poll one run until a human could act on it, or it is over.

    Returns the last status seen either way, including on timeout: the
    listing is still being worked on server-side, and reporting the truth
    about a slow run is more useful than raising about it.
    """
    deadline = time.monotonic() + timeout
    view = client.status(queue_id)
    while time.monotonic() < deadline:
        if view["awaiting_decision"] or view["state"] in _SETTLED_STATES:
            return view
        time.sleep(poll)
        view = client.status(queue_id)
    return view


def _local(
    args: argparse.Namespace,
    settings: Settings,
    worker_factory: WorkerFactory | None,
    out: Writer,
    reader: Reader | None,
) -> int:
    if args.command != "run":
        out(
            f"`{args.command}` needs the control plane: --local starts a worker "
            "for one batch and stops it again, so there is nothing for a "
            "second command to talk to."
        )
        return 2
    if args.urls and args.board is None:
        out("--board is required when `run` is given listing URLs")
        return 2
    return asyncio.run(_local_run(args, settings, worker_factory, out, reader))


async def _local_run(
    args: argparse.Namespace,
    settings: Settings,
    worker_factory: WorkerFactory | None,
    out: Writer,
    reader: Reader | None,
) -> int:
    factory = worker_factory or _default_worker_factory
    worker = factory(settings)
    await worker.start()
    try:
        for url in args.urls:
            queue_id = worker.db.enqueue_job(url, args.board)
            out(f"queued {url} as run {queue_id}")
        results = await worker.drain()
        if args.prompt:
            results = await _prompt_each(worker, results, args.actor, out, reader)
        for result in results:
            out(_describe_result(result))
    except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not traceback
        out(f"the local batch could not be completed: {exc}")
        return 1
    finally:
        # Always: a worker left running holds the profile lock, and the next
        # run — local or served — could not start at all.
        await worker.stop()
    return 0


async def _prompt_each(
    worker: ApplicationWorker,
    results: Sequence[RunResult],
    actor: str | None,
    out: Writer,
    reader: Reader | None,
) -> list[RunResult]:
    """Ask about each staged application, through the shared approval gate.

    The gate is the same object the API uses, so a decision made here is
    validated, recorded, and applied exactly as one made over HTTP.
    """
    gate = CliApprovalGate(
        worker.runner.approvals,
        worker.runner,
        reader=reader or input,
        writer=out,
    )
    decided: list[RunResult] = []
    for result in results:
        if not result.awaiting_approval or result.application_id is None:
            decided.append(result)
            continue
        try:
            decided.append(await gate.prompt(result.application_id, actor=_actor(actor)))
        except ApprovalError as exc:
            out(f"application {result.application_id} was not decided: {exc}")
            decided.append(result)
    return decided


def _actor(actor: str | None) -> str:
    """Who a console decision is recorded as.

    Falls back to the operating-system account, which is the only identity
    a local console can actually assert.
    """
    if actor and actor.strip():
        return actor.strip()
    import getpass

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - a nameless account is still an account
        user = "unknown"
    return f"console:{user}"


def _default_worker_factory(settings: Settings) -> ApplicationWorker:
    # No loop: a local batch drains explicitly, and a background loop would
    # race it for the same claims.
    return ApplicationWorker(settings, run_loop=False)


def _report(
    out: Writer, as_json: bool, payload: Any, describe: Callable[[Any], str]
) -> int:
    if as_json:
        out(json.dumps(payload, indent=2, default=str))
        return 0
    if isinstance(payload, list):
        for entry in payload:
            out(describe(entry))
    else:
        out(describe(payload))
    return 0


def _describe_run(view: dict[str, Any]) -> str:
    application = view.get("application") or {}
    parts = [
        f"run {view['queue_id']} [{view['state']}] {view['listing_url']}",
    ]
    if application:
        parts.append(
            f"  application {application['id']}: {application['status']}"
            + (f" on {application['ats']}" if application.get("ats") else "")
        )
    if view.get("awaiting_decision"):
        reasons = ", ".join((view.get("interrupt") or {}).get("blocking_reasons", []))
        parts.append(f"  awaiting a decision{f' ({reasons})' if reasons else ''}")
    elif view.get("executing"):
        parts.append("  a worker is running this thread right now")
    if view.get("error_reason"):
        parts.append(f"  reason: {view['error_reason']}")
    decision = view.get("decision")
    if decision:
        parts.append(
            f"  decided: {decision['decision']} by {decision['actor']} "
            f"at {decision['decided_at']}"
        )
    return "\n".join(parts)


def _describe_decision(view: dict[str, Any]) -> str:
    return (
        f"application {view['application_id']}: {view['decision']} -> "
        f"{view['status']} ({view['reason']})"
    )


def _describe_result(result: RunResult) -> str:
    line = (
        f"run {result.queue_id} [{result.status.value}] "
        f"application {result.application_id}: {result.reason}"
    )
    if result.status is RunStatus.AWAITING_APPROVAL:
        blocking = ", ".join(result.blocking_reasons)
        line += f" ({blocking})" if blocking else ""
    if result.detail:
        line += f"\n  {result.detail}"
    return line


def thread_of(queue_id: int) -> str:
    """The graph thread a queue id maps to, for anyone reading logs."""
    return thread_id_for(queue_id)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
