"""Application logging with optional field-value redaction."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.storage.db import Database
from app.storage.models import FieldSource


class ApplicationLogger:
    """Persists application field provenance with privacy-aware redaction."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    def log_field(
        self,
        application_id: int,
        stable_key: str,
        source: FieldSource,
        required: bool,
        filled: bool,
        value: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        stored_value = value if self._settings.log_field_values else None
        return self._db.save_application_field(
            application_id=application_id,
            stable_key=stable_key,
            source=source,
            required=required,
            filled=filled,
            value=stored_value,
            metadata=metadata,
        )
