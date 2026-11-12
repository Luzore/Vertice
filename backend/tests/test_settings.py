from __future__ import annotations

from datetime import date, time

import pytest

from core import settings
from core.settings import ConfigError, Secrets, load_config
from tests.conftest import SHIPPED_CONFIG


def test_shipped_config_is_valid():
    config = load_config(SHIPPED_CONFIG)

    assert config.system.name == "VERTICE"
    assert config.system.aggressiveness == 5
    assert config.capital.cash_usd == 5000
    assert config.plan.publish_time_et == time(9, 0)
    assert config.plan.recheck_time_et == time(9, 45)
    assert config.screener.atr_pct_sweet_spot == (2.5, 8.0)
    assert config.ai.max_usd_per_day is None
    assert date(2026, 12, 9) in config.fomc_dates


def _set(data: dict, dotted: str, value) -> dict:
    *parents, leaf = dotted.split(".")
    node = data
    for key in parents:
        node = node[key]
    node[leaf] = value
    return data


INVALID_VALUES = [
    ("system.aggressiveness", 0),
    ("system.aggressiveness", 11),
    ("system.aggressiveness", 5.5),
    ("system.mode", "trade"),
    ("system.timezone_market", "Europe/London"),
    ("system.timezone_user", "Mars/Olympus_Mons"),
    ("capital.cash_usd", -1),
    ("capital.base_currency", "MXN"),
    ("costs.commission_pct", 101),
    ("universe.exchanges", []),
    ("universe.exchanges", ["NYSE", "OTC"]),
    ("universe.exclude", ["spac", "warrant", "otc", "inverse_etf"]),
    ("universe.exclude", ["spac", "warrant", "otc", "leveraged_etf"]),
    ("universe.min_price", 0),
    ("screener.volatility_preference", 1.2),
    ("screener.atr_pct_sweet_spot", [8.0, 2.5]),
    ("screener.max_finalists", 30),
    ("regime.vix_bullish_max", 30),
    ("plan.min_ideas_listed", 4),
    ("plan.publish_time_et", 540),
    ("plan.publish_time_et", "9:00"),
    ("plan.recheck_time_et", "08:45"),
    ("entry.order_type", "market"),
    ("exits.target1_sell_pct", 0),
    ("controls.max_correlation", 1.5),
    ("controls.daily_loss_stop_pct", 6),
    ("ai.daily_call_budget", -1),
    ("learning.min_cases", 19),
    ("learning.min_forward_cases", 9),
    ("learning.min_confidence", 0.59),
    ("learning.retire_below", 0.60),
    ("notify.repeat_high_alerts_min", 0),
    ("fomc_dates", ["not-a-date"]),
    ("data.rate_limit_utilization", 0.95),
    ("data.rate_limit_utilization", 0),
    ("data.max_retries", -1),
    ("data.backoff_max_s", 0.5),
    ("data.fomc_time_et", 1400),
    ("data.fred_series", []),
]


@pytest.mark.parametrize(("field", "value"), INVALID_VALUES)
def test_invalid_value_is_rejected(config_dict, write_config, field, value):
    path = write_config(_set(config_dict, field, value))

    with pytest.raises(ConfigError) as error:
        load_config(path)

    # The message must point at the offending section so the user can fix it.
    assert field.split(".")[0] in str(error.value)


def test_unknown_key_is_rejected(config_dict, write_config):
    config_dict["system"]["agressiveness"] = 7  # typo of "aggressiveness"

    with pytest.raises(ConfigError, match="agressiveness"):
        load_config(write_config(config_dict))


def test_missing_section_is_rejected(config_dict, write_config):
    del config_dict["exits"]

    with pytest.raises(ConfigError, match="exits"):
        load_config(write_config(config_dict))


def test_learning_thresholds_can_be_stricter_than_the_database(config_dict, write_config):
    _set(config_dict, "learning.min_cases", 40)
    _set(config_dict, "learning.min_forward_cases", 25)
    _set(config_dict, "learning.min_confidence", 0.75)

    config = load_config(write_config(config_dict))

    assert config.learning.min_cases == 40


def test_fomc_dates_are_parsed(config_dict, write_config):
    _set(config_dict, "fomc_dates", ["2026-12-09", date(2027, 1, 27)])

    config = load_config(write_config(config_dict))

    assert config.fomc_dates == [date(2026, 12, 9), date(2027, 1, 27)]


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_is_reported(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("system: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_get_config_reads_the_path_from_the_environment(config_dict, write_config, monkeypatch):
    _set(config_dict, "system.aggressiveness", 8)
    monkeypatch.setenv("VERTICE_CONFIG", str(write_config(config_dict)))
    settings.reset_caches()

    assert settings.get_config().system.aggressiveness == 8


# --- secrets -----------------------------------------------------------------


def test_system_starts_without_any_key():
    secrets = settings.get_secrets()

    assert secrets.secret_values() == []
    # Only the sources that need no credentials are ready.
    ready = {source for source, ok in secrets.source_status().items() if ok}
    assert ready == {"rss_news", "yahoo"}
    assert secrets.missing_for("alpaca") == ["ALPACA_API_KEY", "ALPACA_API_SECRET"]
    assert secrets.missing_for("rss_news") == []


def test_blank_values_count_as_missing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FINNHUB_API_KEY=\nFRED_API_KEY=   \nMASSIVE_API_KEY=real-key\n")

    secrets = Secrets(_env_file=env_file)

    assert secrets.finnhub_api_key is None
    assert secrets.fred_api_key is None
    assert secrets.source_status()["massive"] is True


def test_source_needs_all_of_its_credentials(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALPACA_API_KEY=PKTESTKEY000\n"
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=42\n"
        'SEC_USER_AGENT="VERTICE Test test@example.com"\n'
    )

    status = Secrets(_env_file=env_file).source_status()

    assert status["alpaca"] is False  # the secret is missing
    assert status["telegram"] is True
    assert status["sec_edgar"] is True
    assert status["fred"] is False


def test_secrets_never_appear_in_repr_or_dump(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FRED_API_KEY=super-secret-value-123\n")

    secrets = Secrets(_env_file=env_file)

    assert "super-secret-value-123" not in repr(secrets)
    assert "super-secret-value-123" not in str(secrets.model_dump())
    assert "super-secret-value-123" not in secrets.model_dump_json()
    assert secrets.secret_values() == ["super-secret-value-123"]


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FRED_API_KEY=from-file\n")
    monkeypatch.setenv("FRED_API_KEY", "from-environment")

    assert Secrets(_env_file=env_file).fred_api_key.get_secret_value() == "from-environment"


def test_unrelated_variables_in_env_file_are_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("VPS_HOST=203.0.113.10\nVPS_USER=deploy\n")

    assert Secrets(_env_file=env_file).secret_values() == []


def test_database_url_default_and_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////somewhere/else.db")
    assert settings.database_url() == "sqlite:////somewhere/else.db"

    monkeypatch.delenv("DATABASE_URL")
    assert settings.database_url().endswith("/data/vertice.db")


def test_data_section_of_the_shipped_config():
    data = load_config(SHIPPED_CONFIG).data

    assert data.rate_limit_utilization == 0.8
    assert data.raw_retention_days == 30
    assert data.stale.quote_max_age_s == 120
    assert (data.sources["alpaca"].limit_calls, data.sources["alpaca"].limit_period_s) == (200, 60)
    assert (data.sources["finnhub"].limit_calls, data.sources["fred"].limit_calls) == (60, 120)
    assert (data.sources["sec_edgar"].limit_calls, data.sources["sec_edgar"].limit_period_s) == (
        10,
        1,
    )
    assert data.sources["massive"].limit_calls == 5
    assert data.sources["rss_news"].cache_ttl_s == {"google_news": 900, "yahoo_finance": 900}
    assert [s for s, c in data.sources.items() if c.critical] == ["alpaca", "finnhub", "fred"]
    assert "VIXCLS" in data.fred_series and "DEXMXUS" in data.fred_series
    assert [(r.name, r.fred_release_id) for r in data.econ_releases] == [
        ("CPI", 10),
        ("Empleo", 50),
        ("PIB", 53),
    ]
    assert data.fomc_time_et == time(14, 0)


def test_fomc_dates_of_the_shipped_config_are_wednesdays_in_order():
    dates = load_config(SHIPPED_CONFIG).fomc_dates

    assert dates == sorted(dates)
    assert len(dates) >= 8
    assert {d.weekday() for d in dates} == {2}  # decisions are announced on Wednesdays


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data["sources"].pop("fred"), "sources is missing"),
        (lambda data: data["sources"].update(reddit=data["sources"]["yahoo"]), "unknown names"),
        (lambda data: data["sources"]["alpaca"]["cache_ttl_s"].update(news=-5), "negative"),
        (lambda data: data["sources"]["alpaca"].update(limit_calls=0), "limit_calls"),
        (lambda data: data["econ_releases"][0].update(time_et="8:30"), "HH:MM"),
    ],
)
def test_invalid_data_sources_are_rejected(config_dict, write_config, mutate, expected):
    mutate(config_dict["data"])

    with pytest.raises(ConfigError, match=expected):
        load_config(write_config(config_dict))
