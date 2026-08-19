"""Export the application log as JSON or CSV.

Reads storage directly — no server, no browser — so it works against a
copied database file and while the worker is running.

**Answer text is never exported by accident.** A value is included only
when `--include-values` is passed *and* `LOG_FIELD_VALUES` is enabled in
the configuration. Both, because they answer different questions: the
setting is the standing policy for this installation, and the flag is the
operator saying they mean it for this one export. A database written while
logging was enabled and exported after it was turned off therefore stays
redacted, which is the direction that cannot leak.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from app.config import Settings
from app.storage.db import Database
from app.storage.models import ApplicationStatus

Writer = Callable[[str], Any]

#: Columns of the application-level export, in order.
APPLICATION_COLUMNS = (
    "application_id",
    "queue_id",
    "thread_id",
    "listing_url",
    "board",
    "queue_state",
    "status",
    "ats",
    "trigger_tier",
    "model_cost",
    "screenshot_path",
    "error_reason",
    "decision",
    "actor",
    "note",
    "decided_at",
    "created_at",
    "updated_at",
)

#: The extra columns each field row adds to its application's columns.
FIELD_COLUMNS = (
    "stable_key",
    "source",
    "required",
    "filled",
    "value",
    "label",
)


#: Leading characters a spreadsheet reads as the start of a formula.
#: A tab or a carriage return counts because both are stripped before the
#: cell is parsed, so `\t=1+1` is `=1+1` by the time Excel looks at it.
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(cell: Any) -> Any:
    """Neutralise a cell a spreadsheet would otherwise execute.

    Field labels come from the listing's own page and answers come from a
    model or a human, so both are text this project did not write. Excel
    and LibreOffice execute a cell beginning with `=`, `+`, `-`, `@`, a
    tab, or a carriage return when the file is opened — and the person
    opening their own export is the one whose accounts are worth taking.

    Prefixing with an apostrophe is the spreadsheet convention for "this is
    text": it survives a round trip through the application that needs it
    and is visible to anything else. Only strings are touched, so the
    numeric columns — a model cost of `-0.25`, say — are still numbers.
    """
    if isinstance(cell, str) and cell.startswith(FORMULA_LEADERS):
        return "'" + cell
    return cell


def _unreadable(path: Path, exc: sqlite3.Error) -> str:
    """Turn a SQLite failure into a sentence with a next step in it.

    The three that actually happen are a path pointing at something that
    is not a database, a database that belongs to another program, and a
    file that cannot be opened at all. SQLite words each of them for
    someone debugging SQLite.
    """
    detail = str(exc)
    if isinstance(exc, sqlite3.DatabaseError) and "not a database" in detail:
        advice = (
            f"{path} is not a SQLite database. Check SQLITE_PATH — it may be "
            "pointing at a backup, an artefact, or a partly written file."
        )
    elif "no such table" in detail:
        advice = (
            f"{path} is a SQLite database but not this project's: {detail}. "
            "Check SQLITE_PATH, or run the control plane once against this "
            "path to create the schema."
        )
    elif "unable to open" in detail:
        advice = (
            f"{path} could not be opened: {detail}. Check that the file and "
            "its directory are readable by this user."
        )
    else:
        advice = f"{path} could not be read: {detail}."
    return f"the export failed. {advice}"


def _status(value: str) -> ApplicationStatus:
    try:
        return ApplicationStatus(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an application status; choose one of "
            f"{', '.join(status.value for status in ApplicationStatus)}"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_log", description="Export the application log"
    )
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument(
        "--status",
        type=_status,
        action="append",
        default=None,
        help="only export applications with this status (repeatable)",
    )
    parser.add_argument(
        "--fields",
        action="store_true",
        help="include per-field provenance (one CSV row per field)",
    )
    parser.add_argument(
        "--include-values",
        action="store_true",
        help="include answer text; only has an effect when LOG_FIELD_VALUES is on",
    )
    parser.add_argument("--output", type=Path, default=None, help="write to a file")
    return parser


def collect(
    db: Database,
    *,
    statuses: Sequence[ApplicationStatus] | None = None,
    include_fields: bool = False,
    include_values: bool = False,
) -> list[dict[str, Any]]:
    """One record per application, with the queue row and decision folded in."""
    records: list[dict[str, Any]] = []
    for application in db.list_applications(statuses=statuses):
        item = db.get_queue_item(application.queue_id)
        approval = db.get_approval(application.id)
        entry: dict[str, Any] = {
            "application_id": application.id,
            "queue_id": application.queue_id,
            "thread_id": application.thread_id,
            "listing_url": item.listing_url if item is not None else None,
            "board": item.board.value if item is not None else None,
            "queue_state": item.state.value if item is not None else None,
            "status": application.status.value,
            "ats": application.ats,
            "trigger_tier": application.trigger_tier,
            "model_cost": application.model_cost,
            "screenshot_path": application.screenshot_path,
            "error_reason": item.error_reason if item is not None else None,
            "decision": approval.decision.value if approval is not None else None,
            "actor": approval.actor if approval is not None else None,
            "note": approval.note if approval is not None else None,
            "decided_at": (
                approval.timestamp.isoformat() if approval is not None else None
            ),
            "created_at": application.created_at.isoformat(),
            "updated_at": application.updated_at.isoformat(),
        }
        if include_fields:
            entry["fields"] = [
                {
                    "stable_key": field.stable_key,
                    "source": field.source.value,
                    "required": field.required,
                    "filled": field.filled,
                    # Redaction happens here rather than in the query, so
                    # there is exactly one place to read to know whether an
                    # export can contain answer text.
                    "value": field.value if include_values else None,
                    "label": field.metadata.get("label"),
                }
                for field in db.get_application_fields(application.id)
            ]
        records.append(entry)
    return records


def render_json(records: Sequence[dict[str, Any]], *, values_included: bool) -> str:
    return json.dumps(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "field_values_included": values_included,
            "applications": list(records),
        },
        indent=2,
        default=str,
    )


def render_csv(records: Sequence[dict[str, Any]], *, include_fields: bool) -> str:
    """Flat rows: one per application, or one per field when asked.

    Two shapes rather than one wide one, because a CSV with a repeated
    application block per field is unreadable and a CSV with an embedded
    list is not a CSV.
    """
    buffer = io.StringIO()
    columns = list(APPLICATION_COLUMNS) + (
        list(FIELD_COLUMNS) if include_fields else []
    )
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()

    def write(row: dict[str, Any]) -> None:
        # Escaping happens here, at the one place a cell becomes CSV, so
        # there is no route into the file that bypasses it.
        writer.writerow({key: csv_safe(value) for key, value in row.items()})

    for record in records:
        base = {column: record.get(column) for column in APPLICATION_COLUMNS}
        fields = record.get("fields") or [] if include_fields else []
        if not fields:
            write(base)
            continue
        for field in fields:
            write({**base, **{key: field.get(key) for key in FIELD_COLUMNS}})
    return buffer.getvalue().rstrip("\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    writer: Writer | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out: Writer = writer if writer is not None else print
    resolved = settings if settings is not None else Settings()

    include_values = bool(args.include_values and resolved.log_field_values)
    if args.include_values and not include_values:
        # Said out of band, so JSON output stays parseable.
        print(
            "--include-values had no effect: LOG_FIELD_VALUES is disabled, so "
            "answer text stays redacted.",
            file=sys.stderr,
        )

    if not resolved.sqlite_path.exists():
        # Checked rather than created: `initialize()` would happily make an
        # empty database at a mistyped path, and an empty export is the one
        # answer an operator cannot tell apart from the truth.
        out(
            f"there is no database at {resolved.sqlite_path}. Check SQLITE_PATH, "
            "or point it at the file you meant to export."
        )
        return 1

    db = Database(resolved)
    try:
        # One read-only snapshot for the whole walk: an export that read
        # applications, then their fields, then their approvals over
        # separate connections could describe a combination that never
        # existed, and one that opened for writing could migrate the log
        # it was asked only to read.
        with db.read_only_snapshot():
            records = collect(
                db,
                statuses=args.status,
                include_fields=args.fields,
                include_values=include_values,
            )
    except sqlite3.Error as exc:
        out(_unreadable(resolved.sqlite_path, exc))
        return 1
    rendered = (
        render_json(records, values_included=include_values)
        if args.format == "json"
        else render_csv(records, include_fields=args.fields)
    )

    if args.output is None:
        out(rendered)
        return 0
    try:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    except OSError as exc:
        out(f"the export could not be written to {args.output}: {exc}")
        return 1
    out(f"wrote {len(records)} application(s) to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
