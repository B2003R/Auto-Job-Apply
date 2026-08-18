"""Application configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    auto_submit: bool = False
    log_field_values: bool = False

    linkedin_daily_cap: int = 40
    jobright_daily_cap: int = 40
    wellfound_daily_cap: int = 40
    handshake_daily_cap: int = 40

    sqlite_path: Path = Path("./data/jobs.db")
    artifacts_path: Path = Path("./data/artifacts")

    chrome_executable: Path = Path("/usr/bin/google-chrome")
    chrome_profile_path: Path = Path("/home/user/.config/job-apply-chrome")
    jobright_extension_id: str = "your-extension-id-here"
    # 0 is the "not calibrated yet" sentinel: the native toolbar-click tier
    # refuses to run rather than press an arbitrary screen pixel on a machine
    # whose toolbar has never been measured. Set both via
    # scripts/calibrate_toolbar.py.
    toolbar_x: int = 0
    toolbar_y: int = 0
    #: Window the native tier activates (and verifies) before clicking.
    chrome_window_name: str = "Google Chrome"
    #: Explicit X window id, for when several windows match the name.
    chrome_window_id: str = ""

    delay_min_ms: int = 500
    delay_max_ms: int = 1500

    routine_model_url: str = "https://api.openai.com/v1"
    routine_model_name: str = "gpt-4o-mini"
    escalation_model_url: str = "https://api.openai.com/v1"
    escalation_model_name: str = "gpt-4o"
    openai_api_key: str = ""

    routine_input_price: float = 0.15
    routine_output_price: float = 0.60
    escalation_input_price: float = 2.50
    escalation_output_price: float = 10.00
