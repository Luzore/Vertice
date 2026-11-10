"""Configuration and secrets.

Two sources, kept apart on purpose:

* ``config/config.yaml`` holds every tunable threshold. It is validated here and a
  bad value stops the system at startup instead of surfacing later as a bad trade idea.
* ``.env`` holds secrets only (principle 12). They are wrapped in ``SecretStr`` so
  they never appear in logs, reprs or API responses.
"""

from __future__ import annotations

import os
import re
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# The database enforces these floors with a CHECK constraint (principle 9).
# config.yaml may ask for more evidence, never for less.
DB_MIN_LESSON_CASES = 20
DB_MIN_LESSON_FORWARD_CASES = 10
DB_MIN_LESSON_CONFIDENCE = 0.60

# Principle 2: these can never be removed from the universe exclusions.
MANDATORY_EXCLUSIONS = frozenset({"leveraged_etf", "inverse_etf"})

Percent = Annotated[float, Field(ge=0, le=100)]
PositivePercent = Annotated[float, Field(gt=0, le=100)]


class ConfigError(Exception):
    """config.yaml is missing, unreadable or fails validation."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SystemConfig(_Section):
    name: str = Field(min_length=1)
    mode: Literal["recommend", "watch_only", "paused"]
    aggressiveness: int = Field(ge=1, le=10)
    # Principle 11: the market calendar always follows New York.
    timezone_market: Literal["America/New_York"]
    timezone_user: str

    @field_validator("timezone_user")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value


class CapitalConfig(_Section):
    base_currency: Literal["USD"]
    cash_usd: float = Field(ge=0)
    show_mxn: bool
    fractional_shares: bool


class CostsConfig(_Section):
    commission_pct: Percent
    vat_on_commission_pct: Percent
    slippage_pct: Percent


class UniverseConfig(_Section):
    exchanges: list[Literal["NYSE", "NASDAQ"]] = Field(min_length=1)
    sic_only: bool
    sic_list_path: str = Field(min_length=1)
    min_price: float = Field(gt=0)
    min_dollar_volume_20d: float = Field(ge=0)
    min_market_cap: float = Field(ge=0)
    exclude: list[Literal["leveraged_etf", "inverse_etf", "spac", "warrant", "otc"]]

    @field_validator("exclude")
    @classmethod
    def _keeps_mandatory_exclusions(cls, value: list[str]) -> list[str]:
        missing = MANDATORY_EXCLUSIONS - set(value)
        if missing:
            raise ValueError(
                f"{sorted(missing)} must stay excluded: the system is long only and "
                "never trades leveraged or inverse ETFs"
            )
        return value


class ScreenerConfig(_Section):
    top_n_to_controls: int = Field(ge=1)
    max_finalists: int = Field(ge=1)
    volatility_preference: float = Field(ge=0, le=1)
    atr_pct_sweet_spot: tuple[float, float]

    @model_validator(mode="after")
    def _consistent(self) -> ScreenerConfig:
        low, high = self.atr_pct_sweet_spot
        if not 0 < low < high:
            raise ValueError("atr_pct_sweet_spot must be [low, high] with 0 < low < high")
        if self.max_finalists > self.top_n_to_controls:
            raise ValueError("max_finalists cannot exceed top_n_to_controls")
        return self


class RegimeConfig(_Section):
    vix_bullish_max: float = Field(gt=0)
    vix_high_risk_min: float = Field(gt=0)
    breadth_bullish_min: Percent

    @model_validator(mode="after")
    def _ordered(self) -> RegimeConfig:
        if self.vix_bullish_max >= self.vix_high_risk_min:
            raise ValueError("vix_bullish_max must be below vix_high_risk_min")
        return self


class PlanConfig(_Section):
    # The daily plan always lists at least 5 ideas; this can be raised, not lowered.
    min_ideas_listed: int = Field(ge=5)
    publish_time_et: time
    recheck_time_et: time

    @field_validator("publish_time_et", "recheck_time_et", mode="before")
    @classmethod
    def _quoted_hhmm(cls, value: Any) -> Any:
        # Unquoted YAML times such as 10:30 are read as the integer 630; refuse
        # anything that is not an explicit "HH:MM" string so that cannot slip through.
        if not isinstance(value, str) or not _HHMM.match(value):
            raise ValueError('must be a quoted "HH:MM" string, for example "09:00"')
        return value

    @model_validator(mode="after")
    def _recheck_after_publish(self) -> PlanConfig:
        if self.recheck_time_et <= self.publish_time_et:
            raise ValueError("recheck_time_et must be later than publish_time_et")
        return self


class EntryConfig(_Section):
    skip_first_minutes: int = Field(ge=0, le=390)
    # Never "market": the plan only recommends limit orders.
    order_type: Literal["limit", "stop_limit"]


class ExitsConfig(_Section):
    target1_sell_pct: PositivePercent
    move_stop_to_breakeven_after_t1: bool
    trailing_start_r: float = Field(gt=0)
    trailing_atr_mult: float = Field(gt=0)
    time_stop_days: int = Field(ge=1)
    time_stop_min_r: float
    hold_through_earnings: bool


class ControlsConfig(_Section):
    earnings_block_days: int = Field(ge=0)
    macro_event_block_hours: float = Field(ge=0)
    max_gap_atr: float = Field(gt=0)
    max_extension_atr: float = Field(gt=0)
    min_reward_r: float = Field(gt=0)
    max_sector_positions: int = Field(ge=1)
    max_sector_pct: PositivePercent
    max_correlation: float = Field(gt=0, le=1)
    cooldown_days_after_stop: int = Field(ge=0)
    daily_loss_stop_pct: PositivePercent
    weekly_loss_stop_pct: PositivePercent
    max_drawdown_pause_pct: PositivePercent

    @model_validator(mode="after")
    def _loss_limits_escalate(self) -> ControlsConfig:
        if not (
            self.daily_loss_stop_pct <= self.weekly_loss_stop_pct <= self.max_drawdown_pause_pct
        ):
            raise ValueError(
                "loss limits must escalate: daily_loss_stop_pct <= weekly_loss_stop_pct "
                "<= max_drawdown_pause_pct"
            )
        return self


class AIConfig(_Section):
    enabled: bool
    analyst_model: str = Field(min_length=1)
    news_model: str = Field(min_length=1)
    lessons_model: str = Field(min_length=1)
    daily_call_budget: int = Field(ge=0)
    reserve_for_positions_pct: Percent
    max_usd_per_day: float | None = Field(ge=0)
    timeout_s: int = Field(gt=0)
    allow_web_search: bool
    cache_minutes: int = Field(ge=0)


class LearningConfig(_Section):
    min_cases: int = Field(ge=DB_MIN_LESSON_CASES)
    min_forward_cases: int = Field(ge=DB_MIN_LESSON_FORWARD_CASES)
    min_confidence: float = Field(ge=DB_MIN_LESSON_CONFIDENCE, le=1)
    retire_below: float = Field(ge=0, le=1)
    max_active_lessons: int = Field(ge=1)
    max_new_per_day: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> LearningConfig:
        if self.min_forward_cases > self.min_cases:
            raise ValueError("min_forward_cases cannot exceed min_cases")
        if self.retire_below >= self.min_confidence:
            raise ValueError("retire_below must be lower than min_confidence")
        return self


class NotifyConfig(_Section):
    telegram: bool
    repeat_high_alerts_min: int = Field(ge=1)
    repeat_high_alerts_max: int = Field(ge=0)


class AppConfig(_Section):
    system: SystemConfig
    capital: CapitalConfig
    costs: CostsConfig
    universe: UniverseConfig
    screener: ScreenerConfig
    regime: RegimeConfig
    plan: PlanConfig
    entry: EntryConfig
    exits: ExitsConfig
    controls: ControlsConfig
    ai: AIConfig
    learning: LearningConfig
    notify: NotifyConfig
    fomc_dates: list[date]


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def load_config(path: Path | str) -> AppConfig:
    """Read and validate a config.yaml file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{_format_validation_error(exc)}") from exc


class Secrets(BaseSettings):
    """Credentials read from the environment or ``.env``. Every field is optional:
    the system must start without keys and report that source as not configured."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    sec_user_agent: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    massive_api_key: SecretStr | None = None
    banxico_token: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_missing(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def source_status(self) -> dict[str, bool]:
        """Which sources have the credentials they need. Never exposes a value."""

        def present(*fields: SecretStr | None) -> bool:
            return all(field is not None for field in fields)

        return {
            "alpaca": present(self.alpaca_api_key, self.alpaca_api_secret),
            "finnhub": present(self.finnhub_api_key),
            "fred": present(self.fred_api_key),
            "sec_edgar": present(self.sec_user_agent),
            "telegram": present(self.telegram_bot_token, self.telegram_chat_id),
            "massive": present(self.massive_api_key),
            "banxico": present(self.banxico_token),
            "anthropic_api": present(self.anthropic_api_key),
        }

    def secret_values(self) -> list[str]:
        """Raw values, only for the log redactor to scrub them from output."""
        values = []
        for name in type(self).model_fields:
            field = getattr(self, name)
            if field is not None:
                values.append(field.get_secret_value())
        return values


def repo_root() -> Path:
    override = os.environ.get("VERTICE_ROOT")
    return Path(override).resolve() if override else Path(__file__).resolve().parents[2]


def config_path() -> Path:
    override = os.environ.get("VERTICE_CONFIG")
    return Path(override).resolve() if override else repo_root() / "config" / "config.yaml"


def env_path() -> Path:
    return repo_root() / ".env"


def database_url() -> str:
    override = os.environ.get("DATABASE_URL")
    if override and override.strip():
        return override.strip()
    return f"sqlite:///{repo_root() / 'data' / 'vertice.db'}"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config(config_path())


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets(_env_file=env_path(), _env_file_encoding="utf-8")


def reset_caches() -> None:
    """Forget cached config and secrets (tests, and after the config file is edited)."""
    get_config.cache_clear()
    get_secrets.cache_clear()
