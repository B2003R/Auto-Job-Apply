"""Tests for application configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Settings


def test_settings_safe_defaults() -> None:
    settings = Settings()
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

    settings = Settings()

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
    settings = Settings()
    assert settings.auto_submit is False
    assert os.environ.get("UNRELATED_ENV_VAR") == "should-not-affect-settings"
