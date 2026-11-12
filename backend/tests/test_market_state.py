"""The market state end to end: built from the database, never from the future."""

from __future__ import annotations

import math
import time as wall_clock
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from core import clock, market_calendar, settings
from db import migrate, models
from db import session as db_session
from db.session import session_scope
from engine import data_engine as de
from engine import indicators as ind
from engine.schemas import Quote
from engine.universe import CONTEXT_TICKERS, SECTOR_ETFS
from tests.conftest import SHIPPED_CONFIG

LAST_DAY = date(2026, 11, 13)  # a Friday; the seeded data runs up to here
FIVE_MINUTES = timedelta(minutes=5)


@pytest.fixture(scope="module")
def config():
    return settings.load_config(SHIPPED_CONFIG)


def _sessions(count: int, until: date = LAST_DAY) -> list[date]:
    return market_calendar.trading_days_between(
        market_calendar.add_trading_days(until, -(count - 1)), until
    )


def seed(
    engine,
    tickers: list[str],
    sessions: list[date],
    *,
    known_at: datetime | None = None,
    seed_value=11,
):
    """Fill the database with synthetic data for every layer.

    With ``known_at`` only what was knowable at that instant is written. That twin
    database is the oracle for the point-in-time tests: its rules are spelled out
    here, independently of the readers under test.
    """
    rng = np.random.default_rng(seed_value)
    now = datetime(2026, 11, 14, 12, 0, tzinfo=clock.UTC)

    def session_closed(day: date) -> bool:
        return known_at is None or market_calendar.get_session(day).close <= known_at

    with session_scope(engine) as session:
        session.add_all(
            models.Universe(ticker=t, name=f"{t} Inc.", exchange="NYSE", active=True, updated_at=now,
                            sector_etf=SECTOR_ETFS[i % len(SECTOR_ETFS)])
            for i, t in enumerate(tickers)
        )  # fmt: skip

        daily = []
        for ticker in [*tickers, *CONTEXT_TICKERS]:
            close = 40 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(sessions))))
            spread = close * rng.uniform(0.004, 0.02, len(sessions))
            volume = rng.uniform(2e5, 4e6, len(sessions)).round()
            for index, day in enumerate(sessions):
                if not session_closed(day):
                    continue
                daily.append({"ticker": ticker, "session_date": day, "open": close[index] * 1.001,
                              "high": close[index] + spread[index], "low": close[index] - spread[index],
                              "close": close[index], "volume": volume[index], "vwap": None,
                              "source": "seed", "updated_at": now})  # fmt: skip
        de._upsert(session, models.BarDaily, daily, ("ticker", "session_date"))

        intraday = []
        for day in sessions[-12:]:
            opening = market_calendar.get_session(day).open
            for step in range(78):
                ts = opening + step * FIVE_MINUTES
                # Drawn before the filter so both databases get the same numbers.
                price, volume_ = 50 + rng.normal(), float(rng.integers(1000, 9000))
                if known_at is not None and ts + FIVE_MINUTES > known_at:
                    continue
                intraday.append({"ticker": tickers[0], "timeframe": "5Min", "ts": ts, "open": price,
                                 "high": price + 0.2, "low": price - 0.2, "close": price,
                                 "volume": volume_, "vwap": None,
                                 "source": "seed", "updated_at": now})  # fmt: skip
        de._upsert(session, models.BarIntraday, intraday, ("ticker", "timeframe", "ts"))

        macro = []
        for series, level in (("VIXCLS", 16.0), ("DGS10", 4.5), ("DGS2", 4.1), ("T10Y2Y", 0.4),
                              ("BAMLH0A0HYM2", 3.2), ("DEXMXUS", 18.6)):  # fmt: skip
            values = level + np.cumsum(rng.normal(0, 0.05, len(sessions)))
            for index, day in enumerate(sessions):
                if known_at is not None and day >= clock.market_date(known_at):
                    continue
                macro.append({"series_id": series, "obs_date": day, "value": float(values[index]),
                              "source": "seed", "fetched_at": now})  # fmt: skip
        de._upsert(session, models.MacroPoint, macro, ("series_id", "obs_date"))

        for index, day in enumerate(sessions[-12:]):
            for hour, tone in ((7, 0.5), (12, -0.4), (18, 0.9)):
                published = clock.market_time(day, time(hour, 0))
                if known_at is not None and published > known_at:
                    continue
                session.add(models.News(tickers=[tickers[index % len(tickers)]], published_at=published,
                                        source="seed", headline=f"story {day} {hour}", sentiment=tone,
                                        severe=False, fetched_at=now))  # fmt: skip

        for index, day in enumerate(sessions[-6:]):
            ticker = tickers[index % len(tickers)]
            filed_at = clock.market_time(day, time(16, 5))
            if known_at is None or filed_at <= known_at:
                session.add(models.CorporateEvent(ticker=ticker, event_type="filing_8k", event_date=day,
                                                  event_at=filed_at, payload={"id": f"f{index}"},
                                                  source="seed", fetched_at=now))  # fmt: skip
            if known_at is None or day < clock.market_date(known_at):
                session.add(models.CorporateEvent(ticker=ticker, event_type="form_4", event_date=day,
                                                  payload={"id": f"i{index}", "code": "P", "shares": 100,
                                                           "price": 40.0},
                                                  source="seed", fetched_at=now))  # fmt: skip
        # Schedules are known in advance, so they are in both databases.
        session.add(models.CorporateEvent(ticker=tickers[0], event_type="earnings",
                                          event_date=date(2026, 11, 19), payload={"kind": "scheduled"},
                                          source="seed", fetched_at=now))  # fmt: skip
        session.add(models.EconEvent(name="CPI", country="US", impact="high", source="seed", fetched_at=now,
                                     scheduled_at=clock.market_time(date(2026, 11, 12), time(8, 30))))  # fmt: skip


@pytest.fixture
def seeded(engine):
    tickers = [f"T{i:02d}" for i in range(24)]
    seed(engine, tickers, _sessions(300))
    return engine, tickers


def _state(engine, config, moment, **kwargs):
    with session_scope(engine) as session:
        return de.build_market_state(session, config, as_of=moment, **kwargs)


# --- market state and indicators together ---------------------------------------------------


def test_market_state_has_every_layer(seeded, config):
    engine, tickers = seeded
    moment = clock.market_time(date(2026, 11, 10), time(11, 0))

    state = _state(engine, config, moment, intraday_tickers=[tickers[0]])

    assert state.as_of == moment
    assert state.session_date == date(2026, 11, 9)
    assert state.tickers == tickers
    assert set(state.daily) == {*tickers, *CONTEXT_TICKERS}
    assert set(state.intraday) == {tickers[0]}
    assert set(state.macro) == {"VIXCLS", "DGS10", "DGS2", "T10Y2Y", "BAMLH0A0HYM2", "DEXMXUS"}
    assert [e.name for e in state.econ_events] == ["CPI"]
    assert state.news and state.corporate_events
    assert set(state.quality) == set(tickers)
    assert all(q.score == 1.0 for q in state.quality.values())
    assert state.health.state in ("NORMAL", "PRECAUCION")
    assert list(state.universe.columns) == ["name", "exchange", "sector", "sector_etf",
                                            "market_cap_usd", "sic_listed"]  # fmt: skip


def test_indicators_for_the_whole_universe(seeded, config):
    engine, tickers = seeded
    moment = clock.market_time(date(2026, 11, 10), time(11, 0))

    result = ind.compute_indicators(
        _state(engine, config, moment, intraday_tickers=[tickers[0]]), config.regime
    )

    assert list(result.metrics.index) == tickers
    assert len(result.metrics.columns) >= 45
    for name in ("sma_200", "rsi_14", "adx_14", "atr_pct", "rvol_20d", "beta_60d", "bb_percent_b",
                 "rs_20d_vs_spy", "rs_20d_vs_sector", "dist_52w_high_pct", "data_quality",
                 "sector_trend", "news_count_anomaly"):  # fmt: skip
        assert result.metrics[name].notna().all(), name
    assert not math.isnan(result.metrics.at[tickers[0], "price_vs_vwap_pct"])
    assert not math.isnan(result.metrics.at[tickers[0], "rvol_time_of_day"])
    assert math.isnan(result.metrics.at[tickers[1], "price_vs_vwap_pct"])  # no intraday bars
    assert result.metrics.at[tickers[0], "days_to_earnings"] == 7.0  # 10 Nov -> 19 Nov

    context = result.context
    assert 0 <= context["breadth_pct_above_sma50"] <= 100
    assert context["vix_level"] > 0 and not math.isnan(context["vix_change_5d_pct"])
    assert context["spy_above_sma200"] in (0.0, 1.0)
    assert context["new_highs_minus_lows_52w"] == context["new_highs_52w"] - context["new_lows_52w"]
    assert {f"sector_trend_{etf}" for etf in SECTOR_ETFS} <= set(context)
    assert result.regime in ("ALCISTA", "NEUTRAL", "RIESGO_ALTO") and result.regime_reasons
    assert set(result.levels) == set(tickers)


def test_ticker_without_bars_gets_quality_zero_and_no_metrics(seeded, config):
    engine, tickers = seeded
    with session_scope(engine) as session:
        session.add(models.Universe(ticker="NEW", name="New Listing", exchange="NASDAQ", active=True,
                                    updated_at=datetime(2026, 11, 14, tzinfo=clock.UTC)))  # fmt: skip

    state = _state(engine, config, clock.market_time(date(2026, 11, 10), time(11, 0)))
    result = ind.compute_indicators(state, config.regime)

    assert state.quality["NEW"].score == 0.0
    assert not state.quality["NEW"].can_buy
    assert "NEW" not in result.metrics.index


def test_inactive_tickers_are_not_part_of_the_state(seeded, config):
    engine, tickers = seeded
    with session_scope(engine) as session:
        session.get(models.Universe, tickers[3]).active = False

    state = _state(engine, config, clock.market_time(date(2026, 11, 10), time(11, 0)))

    assert tickers[3] not in state.tickers and len(state.tickers) == len(tickers) - 1


def test_indicators_are_stored_in_long_form(seeded, config):
    engine, tickers = seeded
    result = ind.compute_indicators(
        _state(engine, config, clock.market_time(date(2026, 11, 10), time(11, 0))), config.regime
    )

    with session_scope(engine) as session:
        written = ind.store_indicators(session, result, tickers[:2])
    with session_scope(engine) as session:
        stored = session.scalar(select(func.count()).select_from(models.IndicatorValue))
        owners = set(session.scalars(select(models.IndicatorValue.ticker).distinct()).all())
        rsi = session.scalar(
            select(models.IndicatorValue.value).where(
                models.IndicatorValue.ticker == tickers[0], models.IndicatorValue.metric == "rsi_14"
            )
        )

    assert written == stored == 2 * len(result.metrics.columns) + len(result.context)
    assert owners == {tickers[0], tickers[1], ind.MARKET_ROW}
    assert rsi == pytest.approx(result.metrics.at[tickers[0], "rsi_14"])


# --- acceptance: in replay, no data is later than the clock ---------------------------------

REPLAY_MOMENTS = [
    pytest.param(clock.market_time(date(2026, 11, 10), time(7, 30)), id="premarket"),
    pytest.param(clock.market_time(date(2026, 11, 10), time(9, 45)), id="just-after-the-open"),
    pytest.param(clock.market_time(date(2026, 11, 10), time(12, 32)), id="mid-session"),
    pytest.param(clock.market_time(date(2026, 11, 10), time(16, 0)), id="at-the-close"),
    pytest.param(clock.market_time(date(2026, 11, 10), time(17, 0)), id="after-the-close"),
    pytest.param(clock.market_time(date(2026, 11, 8), time(10, 0)), id="sunday"),
    pytest.param(clock.market_time(date(2026, 10, 29), time(8, 45)), id="two-weeks-earlier"),
]


@pytest.mark.parametrize("moment", REPLAY_MOMENTS)
def test_replay_never_sees_data_later_than_the_clock(seeded, config, moment):
    engine, tickers = seeded
    late_quote = Quote(
        ticker=tickers[0], ts=moment + timedelta(seconds=1), bid=1, ask=1.1, source="t"
    )
    past_quote = Quote(
        ticker=tickers[1], ts=moment - timedelta(seconds=30), bid=1, ask=1.1, source="t"
    )

    with clock.simulated(moment), session_scope(engine) as session:
        # No as_of passed: everything follows the simulated clock.
        state = de.build_market_state(
            session, config, intraday_tickers=[tickers[0]], quotes=[late_quote, past_quote]
        )

    assert state.as_of == moment
    for ticker, bars in state.daily.items():
        last_session = bars.index[-1].date()
        assert market_calendar.get_session(last_session).close <= moment, ticker
    for bars in state.intraday.values():
        assert bars.index.max().to_pydatetime() + FIVE_MINUTES <= moment
    for series in state.macro.values():
        assert series.index.max().date() < clock.market_date(moment)
    assert all(item.published_at <= moment for item in state.news)
    assert set(state.quotes) == {tickers[1]}
    for events in state.corporate_events.values():
        for event in events:
            scheduled = event.event_type == "earnings" and event.payload.get("kind") == "scheduled"
            if event.event_at is not None:
                assert event.event_at <= moment
            elif not scheduled:
                assert event.event_date < clock.market_date(moment)


@pytest.mark.parametrize("moment", REPLAY_MOMENTS)
def test_replay_gives_the_same_numbers_as_a_database_without_the_future(
    seeded, config, moment, tmp_path
):
    """The strongest check: indicators computed in replay equal the ones computed on
    a database that physically contains nothing later than the clock."""
    engine, tickers = seeded
    twin_url = f"sqlite:///{tmp_path / 'twin.db'}"
    migrate.upgrade(twin_url)
    twin = db_session.make_engine(twin_url)
    seed(twin, tickers, _sessions(300), known_at=moment)

    with clock.simulated(moment):
        replayed = ind.compute_indicators(
            _state(engine, config, clock.now(), intraday_tickers=[tickers[0]]), config.regime
        )
        truth = ind.compute_indicators(
            _state(twin, config, clock.now(), intraday_tickers=[tickers[0]]), config.regime
        )
    twin.dispose()

    pd.testing.assert_frame_equal(replayed.metrics, truth.metrics)
    assert replayed.context == pytest.approx(truth.context, nan_ok=True)
    assert (replayed.regime, replayed.regime_reasons) == (truth.regime, truth.regime_reasons)
    assert replayed.levels == truth.levels


def test_the_future_exists_in_the_database_under_test(seeded):
    """Guards the two tests above: they only prove something if later data is there."""
    engine, _ = seeded
    with session_scope(engine) as session:
        assert session.scalar(select(func.max(models.BarDaily.session_date))) == LAST_DAY
        assert (
            session.scalar(select(func.max(models.News.published_at))) > REPLAY_MOMENTS[4].values[0]
        )
        assert session.scalar(select(func.max(models.MacroPoint.obs_date))) == LAST_DAY


# --- acceptance: 500 tickers in under 60 seconds --------------------------------------------


def test_500_tickers_are_computed_in_under_60_seconds(engine, config, record_property):
    tickers = [f"S{i:03d}" for i in range(500)]
    seed(engine, tickers, _sessions(520))
    moment = clock.market_time(LAST_DAY, time(17, 0))

    started = wall_clock.perf_counter()
    state = _state(engine, config, moment)
    loaded = wall_clock.perf_counter()
    result = ind.compute_indicators(state, config.regime)
    finished = wall_clock.perf_counter()

    record_property("load_seconds", round(loaded - started, 2))
    record_property("compute_seconds", round(finished - loaded, 2))
    print(
        f"\n500 tickers x 520 sessions: load {loaded - started:.2f} s, "
        f"indicators {finished - loaded:.2f} s, total {finished - started:.2f} s, "
        f"{len(result.metrics.columns)} metrics per ticker"
    )
    assert result.metrics.shape[0] == 500
    assert len(result.metrics.columns) >= 45
    assert result.metrics["sma_200"].notna().all()
    assert finished - started < 60
