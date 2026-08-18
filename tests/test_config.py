"""Tests for application configuration."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_safe_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AUTO_SUBMIT=true\nLOG_FIELD_VALUES=true\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert settings.auto_submit is False
    assert settings.log_field_values is False
    assert settings.linkedin_daily_cap == 40


def test_settings_env_parsing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    monkeypatch.setenv("LOG_FIELD_VALUES", "true")
    monkeypatch.setenv("LINKEDIN_DAILY_CAP", "25")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    monkeypatch.setenv("ARTIFACTS_PATH", str(artifacts))
    monkeypatch.setenv("CHROME_EXECUTABLE", "/usr/bin/google-chrome")
    monkeypatch.setenv("CHROME_PROFILE_PATH", "/home/user/.config/job-apply-chrome")
    monkeypatch.setenv("JOBRIGHT_EXTENSION_ID", "abcdefghijklmnop")
    monkeypatch.setenv("TOOLBAR_X", "1200")
    monkeypatch.setenv("TOOLBAR_Y", "80")

    settings = Settings(_env_file=None)

    assert settings.auto_submit is True
    assert settings.log_field_values is True
    assert settings.linkedin_daily_cap == 25
    assert settings.sqlite_path == db_path
    assert settings.artifacts_path == artifacts
    assert settings.chrome_executable == Path("/usr/bin/google-chrome")
    assert settings.chrome_profile_path == Path("/home/user/.config/job-apply-chrome")
    assert settings.jobright_extension_id == "abcdefghijklmnop"
    assert settings.toolbar_x == 1200
    assert settings.toolbar_y == 80


def test_settings_ignores_unrelated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_ENV_VAR", "should-not-affect-settings")
    settings = Settings(_env_file=None)
    assert settings.auto_submit is False
    assert os.environ.get("UNRELATED_ENV_VAR") == "should-not-affect-settings"


class TestNumericBounds:
    """Nonsense numbers are rejected at load, not absorbed at the call site.

    A negative cap, a negative price, or a zero timeout is a typo in a `.env`
    file. Loading it and coping later means the typo survives to whatever
    code forgot to cope; refusing it means the run stops with the variable's
    name in the error.
    """

    @pytest.mark.parametrize(
        "variable",
        [
            "LINKEDIN_DAILY_CAP",
            "JOBRIGHT_DAILY_CAP",
            "WELLFOUND_DAILY_CAP",
            "HANDSHAKE_DAILY_CAP",
            "DELAY_MIN_MS",
            "DELAY_MAX_MS",
            "TOOLBAR_X",
            "TOOLBAR_Y",
            "ROUTINE_INPUT_PRICE",
            "ROUTINE_OUTPUT_PRICE",
            "ESCALATION_INPUT_PRICE",
            "ESCALATION_OUTPUT_PRICE",
        ],
    )
    def test_negative_values_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.setenv(variable, "-1")
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        assert variable.lower() in str(excinfo.value).lower()

    @pytest.mark.parametrize(
        "variable", ["MODEL_TIMEOUT_S", "MODEL_MAX_OUTPUT_TOKENS"]
    )
    def test_zero_is_rejected_where_it_would_disable_the_call(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.setenv(variable, "0")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_zero_is_accepted_where_it_means_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero cap is a real instruction: apply to nothing on that board."""
        monkeypatch.setenv("LINKEDIN_DAILY_CAP", "0")
        monkeypatch.setenv("ROUTINE_INPUT_PRICE", "0")
        settings = Settings(_env_file=None)
        assert settings.linkedin_daily_cap == 0
        assert settings.routine_input_price == Decimal("0")
