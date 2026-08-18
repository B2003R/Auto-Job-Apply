"""Application configuration."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
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

    # ge=0, not gt=0: a zero cap is a real instruction ("apply to nothing on
    # this board today"), while a negative one is a typo that would otherwise
    # be silently clamped somewhere downstream.
    linkedin_daily_cap: int = Field(default=40, ge=0)
    jobright_daily_cap: int = Field(default=40, ge=0)
    wellfound_daily_cap: int = Field(default=40, ge=0)
    handshake_daily_cap: int = Field(default=40, ge=0)

    sqlite_path: Path = Path("./data/jobs.db")
    artifacts_path: Path = Path("./data/artifacts")

    chrome_executable: Path = Path("/usr/bin/google-chrome")
    chrome_profile_path: Path = Path("/home/user/.config/job-apply-chrome")
    jobright_extension_id: str = "your-extension-id-here"
    # 0 is the "not calibrated yet" sentinel: the native toolbar-click tier
    # refuses to run rather than press an arbitrary screen pixel on a machine
    # whose toolbar has never been measured. Set both via
    # scripts/calibrate_toolbar.py.
    toolbar_x: int = Field(default=0, ge=0)
    toolbar_y: int = Field(default=0, ge=0)
    #: Window the native tier activates (and verifies) before clicking.
    chrome_window_name: str = "Google Chrome"
    #: Explicit X window id, for when several windows match the name.
    chrome_window_id: str = ""

    delay_min_ms: int = Field(default=500, ge=0)
    delay_max_ms: int = Field(default=1500, ge=0)

    routine_model_url: str = "https://api.openai.com/v1"
    routine_model_name: str = "gpt-4o-mini"
    escalation_model_url: str = "https://api.openai.com/v1"
    escalation_model_name: str = "gpt-4o"
    # SecretStr, so the key cannot reach a log line, a repr, or a traceback
    # by accident; reading it requires an explicit get_secret_value() call.
    openai_api_key: SecretStr = SecretStr("")

    # Decimal, not float: prices are exact quantities of money, and
    # pydantic parses "0.15" into exactly Decimal("0.15") rather than the
    # nearest binary approximation, so token costs add up exactly.
    routine_input_price: Decimal = Field(default=Decimal("0.15"), ge=0)
    routine_output_price: Decimal = Field(default=Decimal("0.60"), ge=0)
    escalation_input_price: Decimal = Field(default=Decimal("2.50"), ge=0)
    escalation_output_price: Decimal = Field(default=Decimal("10.00"), ge=0)

    #: Canonical, human-authored answers consulted before any model.
    answers_path: Path = Path("./answers.yaml")
    # gt=0 for both: a zero timeout or a zero output budget does not mean
    # "unlimited", it means every call fails, which is a typo worth catching
    # at load rather than one failed request at a time.
    model_timeout_s: float = Field(default=30.0, gt=0)
    model_max_output_tokens: int = Field(default=400, gt=0)
