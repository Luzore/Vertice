"""Exit rules on price bars, and the replay that uses them."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from core import clock, market_calendar, settings
from db import models
from db.session import session_scope
from engine import data_engine as de
from engine import exits
from engine import indicators as ind
from engine.ai_analyst import simulated_analyst
from engine.exits import Bar, ShadowPosition
from engine.indicators import Levels
from engine.preview import main as preview_main
from engine.screener import Candidate
from replay import report
from replay.__main__ import main as replay_main
from replay.engine import DaySignals, run_replay, simulate
from tests.conftest import SHIPPED_CONFIG
from tests.test_market_state import _sessions, seed

CONFIG = settings.load_config(SHIPPED_CONFIG)
EXITS, COSTS = CONFIG.exits, CONFIG.costs
D0 = date(2026, 11, 9)


def position(**changes) -> ShadowPosition:
    """10 shares bought at 100 with the stop at 96 (R = 4) and targets at +2R and +3R."""
    fields = {"ticker": "XYZ", "setup": "RUPTURA", "sector_etf": "XLK", "entry_day": D0, "entry_price": 100.0,
              "shares": 10.0, "stop": 96.0, "targets": (108.0, 112.0), "atr": 2.0, "horizon_days": 10}  # fmt: skip
    pos = ShadowPosition(**{**fields, **changes})
    exits.open_position(pos, COSTS)
    return pos


def bar(day_offset: int, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(D0 + timedelta(days=day_offset), o, h, low, c)


def apply(pos: ShadowPosition, *bars: Bar, **kwargs) -> list[exits.Fill]:
    fills = []
    for one in bars:
        fills += exits.apply_bar(pos, one, EXITS, COSTS, **kwargs)
    return fills


# --- costs ---------------------------------------------------------------------------------


def test_trading_cost_by_hand():
    # 1,000 traded: commission 0.25% = 2.50, VAT 16% on it = 0.40, slippage 0.05% = 0.50.
    assert exits.trading_cost(100.0, 10, COSTS) == pytest.approx(3.40)


def test_net_result_includes_every_cost():
    pos = position()

    apply(pos, bar(1, 101, 113, 100.5, 112.5))  # both targets on the same day

    # Bought 1,000 (cost 3.40); sold 5 at 108 (cost 1.836) and 5 at 112 (cost 1.904).
    assert pos.net_pnl == pytest.approx(540 + 560 - 1000 - (3.40 + 1.836 + 1.904))
    assert pos.net_r == pytest.approx(92.86 / 40)
    assert (pos.exit_reason, pos.is_open) == (exits.TARGET_2, False)


# --- rule 1: the stop -----------------------------------------------------------------------


def test_stop_is_sold_at_the_stop_price():
    pos = position()

    (fill,) = apply(pos, bar(1, 99, 100, 95.5, 97))

    assert (fill.price, fill.shares, fill.reason) == (96.0, 10, exits.STOP)
    assert pos.exit_reason == exits.STOP and not pos.is_open
    assert pos.net_r == pytest.approx((-40 - 3.40 - 960 * 0.0034) / 40)  # a bit worse than -1R


def test_gap_below_the_stop_is_sold_at_the_open_not_at_the_stop():
    pos = position()

    (fill,) = apply(pos, bar(1, 94, 95, 93, 94.5))

    assert (fill.price, fill.reason) == (94.0, exits.STOP_GAP)
    assert pos.net_r < -1.5  # the stop does not guarantee the price


def test_stop_and_target_on_the_same_day_counts_as_the_stop():
    pos = position()

    fills = apply(pos, bar(1, 100, 109, 95, 104))

    assert [f.reason for f in fills] == [exits.STOP]


# --- rules 3 and 5: targets -----------------------------------------------------------------


def test_first_target_sells_half_and_moves_the_stop_to_the_entry():
    pos = position(atr=4.0)  # a wide ATR keeps the trailing stop (108.5 - 10) below the entry

    (fill,) = apply(pos, bar(1, 101, 108.5, 100.5, 107))

    assert (fill.price, fill.shares, fill.reason) == (108.0, 5, exits.TARGET_1)
    assert (pos.shares_open, pos.stop, pos.target_1_done) == (5, 100.0, True)

    (out,) = apply(pos, bar(2, 105, 106, 99.5, 100.5))  # comes back to the entry price
    assert (out.price, out.shares, out.reason) == (100.0, 5, exits.STOP)
    assert pos.net_pnl > 0  # half was sold at +2R, the rest at break-even


def test_after_the_first_target_the_higher_of_break_even_and_trailing_applies():
    pos = position()  # ATR 2: the trailing stop is 108.5 - 5 = 103.5, above the entry

    apply(pos, bar(1, 101, 108.5, 100.5, 107))

    assert pos.stop == 103.5


def test_second_target_sells_the_rest():
    pos = position()

    fills = apply(pos, bar(1, 101, 108.5, 100.5, 108), bar(2, 108, 112.4, 107, 112))

    assert [(f.price, f.shares, f.reason) for f in fills] == [
        (108.0, 5, exits.TARGET_1),
        (112.0, 5, exits.TARGET_2),
    ]
    assert pos.exit_day == D0 + timedelta(days=2)


def test_first_target_is_sold_only_once():
    pos = position()

    fills = apply(pos, bar(1, 101, 108.5, 100.5, 108), bar(2, 108, 109, 107, 108.5))

    assert [f.reason for f in fills] == [exits.TARGET_1]


@pytest.mark.parametrize(
    ("shares", "fractional", "sold"),
    [(11, False, 5), (1, False, 1), (3, False, 1), (11, True, 5.5)],
)
def test_first_target_with_whole_and_fractional_shares(shares, fractional, sold):
    pos = position(shares=float(shares))

    (fill,) = apply(pos, bar(1, 101, 108.5, 100.5, 107), fractional_shares=fractional)

    assert fill.shares == sold
    assert pos.shares_open == shares - sold


def test_without_moving_to_break_even_the_stop_stays():
    pos = position()
    keep = EXITS.model_copy(update={"move_stop_to_breakeven_after_t1": False})

    exits.apply_bar(pos, bar(1, 101, 108.5, 100.5, 107), keep, COSTS)

    assert pos.stop == 103.5  # only the trailing stop moved it: 108.5 - 2.5 ATR


# --- rule 4: trailing stop, and the golden rule ------------------------------------------------


def test_trailing_stop_starts_after_one_and_a_half_r():
    pos = position()

    apply(pos, bar(1, 101, 105.9, 100.5, 105))  # +1.475R at best: not yet
    assert pos.stop == 96.0

    apply(pos, bar(2, 105, 106.5, 104, 106))  # +1.625R: stop = 106.5 - 2.5 * 2
    assert pos.stop == 101.5

    (fill,) = apply(pos, bar(3, 105, 105.5, 101, 102))
    assert (fill.price, fill.reason) == (101.5, exits.STOP)
    assert pos.net_pnl > 0


def test_the_stop_never_moves_down():
    pos = position()
    apply(pos, bar(1, 101, 107, 100.5, 106.5))  # trailing stop at 107 - 5 = 102
    assert pos.stop == 102.0

    assert pos.raise_stop(99.0) is False
    assert pos.stop == 102.0
    # A lower high does not pull the trailing stop back down.
    apply(pos, bar(2, 106, 106.5, 104, 105))
    assert pos.stop == 102.0
    # High risk asks for one ATR under the close (103.8 - 2 = 101.8): lower, so ignored.
    apply(pos, bar(3, 105, 105.5, 103, 103.8), regime="RIESGO_ALTO")
    assert pos.stop == 102.0
    # A new high does move it up.
    apply(pos, bar(4, 104, 107.6, 103.5, 107))
    assert pos.stop == pytest.approx(102.6)


# --- rules 6, 8, 9 and 10 ---------------------------------------------------------------------


def test_position_is_sold_before_an_earnings_report():
    pos = position()

    (fill,) = apply(pos, bar(1, 101, 103, 100, 102.5), earnings_before_next_open=True)

    assert (fill.price, fill.shares, fill.reason) == (102.5, 10, exits.EARNINGS)


def test_holding_through_earnings_when_configured():
    pos = position()
    hold = EXITS.model_copy(update={"hold_through_earnings": True})

    fills = exits.apply_bar(
        pos, bar(1, 101, 103, 100, 102.5), hold, COSTS, earnings_before_next_open=True
    )

    assert fills == [] and pos.is_open


def test_high_risk_regime_tightens_the_stop_of_a_trade_below_one_r():
    pos = position()

    apply(pos, bar(1, 101, 102, 100, 101.5), regime="RIESGO_ALTO")  # +0.375R

    assert pos.stop == 99.5  # one ATR under the close

    calm = position()
    apply(calm, bar(1, 101, 102, 100, 101.5), regime="NEUTRAL")
    assert calm.stop == 96.0


def test_time_stop_after_five_days_without_half_an_r():
    pos = position()
    quiet = [bar(i, 100, 101.9, 99, 100.5) for i in range(1, 6)]  # never reaches 102 (+0.5R)

    fills = apply(pos, *quiet)

    assert [(f.reason, f.price, f.day) for f in fills] == [
        (exits.TIME_STOP, 100.5, D0 + timedelta(days=5))
    ]


def test_no_time_stop_once_it_reached_half_an_r():
    pos = position()
    days = [bar(1, 100, 102.1, 99, 101)] + [bar(i, 100, 101, 99, 100.5) for i in range(2, 10)]

    assert apply(pos, *days) == []
    (fill,) = apply(pos, bar(10, 100, 101, 99, 100.2))
    assert (fill.reason, fill.price) == (exits.HORIZON, 100.2)  # the 10-day horizon


def test_on_the_entry_day_only_the_stop_is_checked():
    pos = position()

    assert apply(pos, bar(0, 100, 109, 99, 108), entry_day=True) == []  # the high is not credited
    assert (pos.days_held, pos.highest, pos.target_1_done) == (0, 100.0, False)

    stopped = position()
    (fill,) = apply(stopped, bar(0, 100, 101, 95, 96.5), entry_day=True)
    assert fill.reason == exits.STOP


def test_closing_at_the_end_of_a_test_period():
    pos = position()
    apply(pos, bar(1, 101, 103, 100, 102))

    fill = exits.close_at(pos, bar(2, 102, 104, 101, 103.5), exits.END_OF_TEST, COSTS)

    assert (fill.price, fill.shares, pos.exit_reason) == (103.5, 10, exits.END_OF_TEST)
    assert exits.close_at(pos, bar(3, 1, 1, 1, 1), exits.END_OF_TEST, COSTS) is None


# --- replay: a scripted market with known outcomes ----------------------------------------------

REPLAY_DAYS = [date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)]
SCRIPT = {
    # ticker: (flat price before, bars of the four replay days as (open, high, low, close))
    "WIN": (
        100.0,
        [
            (100.2, 101, 99.8, 100.8),
            (101, 107, 100.5, 106.5),
            (107, 110, 106, 109.8),
            (110, 111, 109, 110),
        ],
    ),
    "LOSE": (
        50.0,
        [
            (50.1, 50.4, 49.9, 50.0),
            (49.8, 50.0, 48.0, 48.2),
            (48, 48.5, 47.5, 48),
            (48, 48.5, 47.5, 48),
        ],
    ),
    "GAP": (50.0, [(52.0, 53, 51.5, 52.5), (52, 53, 51, 52), (52, 53, 51, 52), (52, 53, 51, 52)]),
}


@pytest.fixture
def scripted(engine):
    """A database where three tickers do exactly what the test needs."""
    history = market_calendar.trading_days_between(market_calendar.add_trading_days(D0, -259), D0)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    rows = []

    def add(ticker: str, day: date, o: float, h: float, low: float, c: float) -> None:
        rows.append({"ticker": ticker, "session_date": day, "open": o, "high": h, "low": low, "close": c,
                     "volume": 4e6, "vwap": None, "source": "script", "updated_at": now})  # fmt: skip

    flat = {ticker: price for ticker, (price, _) in SCRIPT.items()}
    flat.update({ticker: 500.0 for ticker in ("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP",
                                              "XLI", "XLB", "XLU", "XLRE", "XLC")})  # fmt: skip
    for ticker, price in flat.items():
        for day in history:
            add(ticker, day, price, price * 1.01, price * 0.99, price)
        for index, day in enumerate(REPLAY_DAYS):
            if ticker in SCRIPT:
                add(ticker, day, *SCRIPT[ticker][1][index])
            else:
                add(ticker, day, price, price * 1.01, price * 0.99, price)
    with session_scope(engine) as session:
        de._upsert(session, models.BarDaily, rows, ("ticker", "session_date"))
        for index, ticker in enumerate(SCRIPT):
            session.add(models.Universe(ticker=ticker, name=f"{ticker} Corp.", exchange="NYSE", active=True,
                                        updated_at=now, sector_etf=("XLK", "XLF", "XLE")[index],
                                        market_cap_usd=5e9))  # fmt: skip
    with session_scope(engine) as session:
        return de.MarketDataCache(session, CONFIG, start=REPLAY_DAYS[0], end=REPLAY_DAYS[-1])


def _candidate(ticker: str, score: float, close: float, atr: float) -> Candidate:
    metrics = {
        "close": close,
        "atr_14": atr,
        "dollar_volume_20d": close * 4e6,
        "extension_atr": 0.5,
    }
    return Candidate(
        ticker, "RUPTURA", score, {"tendencia": 25.0}, ["rompe máximo de 20 días"], metrics
    )


def _signals(day: date, candidates: list[Candidate]) -> DaySignals:
    levels = {
        "WIN": Levels(supports=(97.0,)),
        "LOSE": Levels(supports=(48.5,)),
        "GAP": Levels(supports=(48.5,)),
    }
    return DaySignals(day=day, as_of=clock.market_time(day, time(9, 0)), regime="ALCISTA",
                      regime_reasons=("SPY arriba de sus promedios",), context={}, candidates=tuple(candidates),
                      levels={c.ticker: levels[c.ticker] for c in candidates}, quality={}, screened=3,
                      with_setup=len(candidates), seconds=0.0)  # fmt: skip


WIN, LOSE, GAP = (
    _candidate("WIN", 90, 100.0, 2.0),
    _candidate("LOSE", 85, 50.0, 1.0),
    _candidate("GAP", 80, 50.0, 1.0),
)


@pytest.fixture
def played(scripted):
    signals = [_signals(REPLAY_DAYS[0], [WIN, LOSE, GAP]), _signals(REPLAY_DAYS[1], [LOSE]),
               _signals(REPLAY_DAYS[2], [LOSE]), _signals(REPLAY_DAYS[3], [])]  # fmt: skip
    return simulate(scripted, CONFIG, signals, capital=5000.0)


def test_replay_follows_each_buy_idea_as_a_shadow_position(played):
    trades = {t.ticker: t for t in played.trades}

    assert set(trades) == {"WIN", "LOSE"}
    win = trades["WIN"]
    # 0.75% of 5,000 is 37.50; without the AI the conviction factor is 0.5 -> 18.75 of risk.
    # Worst-case loss per share 100.50 - 96.80 = 3.70 -> 5 shares, bought at the open of 100.20.
    assert (win.shares, win.entry_price, win.entry_day) == (5, 100.2, REPLAY_DAYS[0])
    assert (win.initial_stop, win.targets) == (96.8, (106.4, 109.6))
    assert [(f.day, f.side, f.price, f.shares) for f in win.fills] == [
        (REPLAY_DAYS[0], "buy", 100.2, 5),
        (REPLAY_DAYS[1], "sell", 106.4, 2),  # first target: half, rounded down
        (REPLAY_DAYS[2], "sell", 109.6, 3),  # second target: the rest
    ]
    assert win.exit_reason == exits.TARGET_2
    assert win.net_pnl == pytest.approx(40.6 - (501 + 212.8 + 328.8) * 0.0034)
    assert win.net_r == pytest.approx(win.net_pnl / (5 * 3.4))
    # Bought inside the zone, so the real risk stays within the 18.75 budget.
    assert win.shares * win.risk_per_share <= 18.75

    lose = trades["LOSE"]
    assert (lose.shares, lose.entry_price, lose.exit_reason, lose.exit_day) == (
        10,
        50.1,
        exits.STOP,
        REPLAY_DAYS[1],
    )
    assert lose.fills[-1].price == 48.4  # sold at the stop, under the support of 48.50
    assert lose.net_r == pytest.approx((-17.0 - (501 + 484) * 0.0034) / (10 * 1.7))
    assert lose.shares * lose.risk_per_share <= 18.75


def test_replay_records_what_happened_to_every_idea(played):
    by_day = {(i.day, i.ticker): i for i in played.ideas}

    assert by_day[(REPLAY_DAYS[0], "WIN")].outcome == "entró"
    assert by_day[(REPLAY_DAYS[0], "LOSE")].outcome == "entró"
    gap = by_day[(REPLAY_DAYS[0], "GAP")]
    assert gap.verdict == "COMPRAR"  # it was a buy at 09:00...
    assert gap.outcome == "no entró: abrió en $52.00, arriba del precio de no perseguir ($50.75)"
    # Second day: LOSE is still held when the plan is published, so it is not bought again.
    assert (
        by_day[(REPLAY_DAYS[1], "LOSE")].reason == "ya tienes una posición abierta en este ticker"
    )
    # Third day: it was stopped out yesterday, so the cooldown blocks it.
    third = by_day[(REPLAY_DAYS[2], "LOSE")]
    assert (
        third.verdict == "NO_HOY"
        and "C19 Enfriamiento: salió por stop el 2026-11-11" in third.reason
    )
    assert played.blockers == {"C19 Enfriamiento": 1}


def test_replay_account_and_benchmark(played):
    first, last = played.days[0], played.days[-1]

    # End of day 1: cash after both purchases and their costs, positions at the close.
    cash = 5000 - 501 * 1.0034 - 501 * 1.0034  # 5 at 100.20 and 10 at 50.10
    assert first.cash == pytest.approx(cash)
    assert first.equity == pytest.approx(cash + 5 * 100.8 + 10 * 50.0)
    assert (first.buy, first.entered, first.open_positions) == (3, 2, 2)
    total = sum(t.net_pnl for t in played.trades)
    assert last.equity == pytest.approx(5000 + total)
    assert last.cash == pytest.approx(last.equity) and last.open_positions == 0
    assert [d.spy_equity for d in played.days] == pytest.approx([5000.0] * 4)  # SPY did not move


def test_replay_report_has_ideas_shadows_and_the_result_against_spy(played, tmp_path):
    text = report.render(played)

    for section in ("## Resultado contra SPY", "## Ideas", "## Sombras (operaciones virtuales)",
                    "## Controles que más bloquearon", "## Tiempos", "## Cómo leer este reporte"):  # fmt: skip
        assert section in text
    assert "| Capital final | $5,017 | $5,000 |" in text
    assert "| Rendimiento | +0.33% | +0.00% |" in text
    assert "Diferencia contra SPY: +0.33 puntos porcentuales." in text
    assert "| Todas | 2 | 50% |" in text
    assert "| alcanzó el objetivo 2 | 1 |" in text and "| saltó el stop | 1 |" in text
    assert "| C19 Enfriamiento | 1 |" in text
    assert "No demuestra una ventaja" in text and "El universo es el de hoy" in text

    path = report.write(played, tmp_path / "out")
    assert path.read_text(encoding="utf-8") == text
    trades = pd.read_csv(tmp_path / "out" / "trades.csv")
    assert (
        set(trades["ticker"]) == {"WIN", "LOSE"}
        and len(pd.read_csv(tmp_path / "out" / "equity.csv")) == 4
    )
    assert len(pd.read_csv(tmp_path / "out" / "ideas.csv")) == len(played.ideas)


def test_report_statistics():
    stats = report.trade_stats([])
    assert stats.count == 0 and stats.net_pnl == 0.0

    assert report.max_drawdown_pct([100, 110, 99, 105, 120, 108]) == pytest.approx(10.0)
    assert report.max_drawdown_pct([100, 101, 102]) == 0.0


# --- replay on a whole (synthetic) market --------------------------------------------------------


@pytest.fixture
def market(engine):
    tickers = [f"T{i:02d}" for i in range(24)]
    seed(engine, tickers, _sessions(300))
    with session_scope(engine) as session:
        for row in session.query(models.Universe).all():
            row.market_cap_usd = 5e9
    return engine


def test_cache_gives_the_same_state_as_the_database_readers(market):
    """Replay reads from memory; it must see exactly what the database readers see."""
    moment = clock.market_time(date(2026, 11, 10), time(9, 0))
    with session_scope(market) as session:
        cache = de.MarketDataCache(session, CONFIG, start=date(2026, 10, 1), end=date(2026, 11, 13))
        direct = ind.compute_indicators(
            de.build_market_state(session, CONFIG, as_of=moment), CONFIG.regime
        )
    cached = ind.compute_indicators(cache.state_at(moment), CONFIG.regime)

    pd.testing.assert_frame_equal(cached.metrics, direct.metrics)
    assert cached.context == pytest.approx(direct.context, nan_ok=True)
    assert (cached.regime, cached.levels) == (direct.regime, direct.levels)


def test_cache_never_serves_the_future(market):
    moment = clock.market_time(date(2026, 11, 10), time(9, 0))
    with session_scope(market) as session:
        cache = de.MarketDataCache(session, CONFIG, start=date(2026, 10, 1), end=date(2026, 11, 13))

    state = cache.state_at(moment)

    assert all(bars.index[-1].date() <= date(2026, 11, 9) for bars in state.daily.values())
    assert all(series.index[-1].date() < date(2026, 11, 10) for series in state.macro.values())
    assert all(item.published_at <= moment for item in state.news)
    assert cache.bar_on("T00", date(2026, 11, 13)) is not None  # the simulator can look ahead


def test_replay_of_a_market_lists_five_ideas_a_day_in_seconds(market):
    result = run_replay(market, CONFIG, date(2026, 11, 2), date(2026, 11, 13), capital=5000.0)

    assert [d.day for d in result.days] == market_calendar.trading_days_between(
        date(2026, 11, 2), date(2026, 11, 13)
    )
    assert all(d.listed >= 5 for d in result.days)
    assert result.slowest_day_seconds < 30
    assert all(d.equity > 0 and d.spy_equity > 0 for d in result.days)
    assert sum(t.net_pnl for t in result.trades) == pytest.approx(result.days[-1].equity - 5000.0)
    assert "## Resultado contra SPY" in report.render(result)


def test_replay_is_the_same_with_one_or_several_processes(market):
    single = run_replay(market, CONFIG, date(2026, 11, 9), date(2026, 11, 13), workers=1)
    parallel = run_replay(market, CONFIG, date(2026, 11, 9), date(2026, 11, 13), workers=2)

    assert [(i.day, i.ticker, i.verdict, i.score) for i in single.ideas] == [
        (i.day, i.ticker, i.verdict, i.score) for i in parallel.ideas
    ]
    assert [d.equity for d in single.days] == pytest.approx([d.equity for d in parallel.days])


def test_replay_needs_trading_days(market):
    with pytest.raises(ValueError, match="no hay días hábiles"):
        run_replay(market, CONFIG, date(2026, 11, 14), date(2026, 11, 15))


def test_replay_command_writes_the_report(market, db_url, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)
    out = tmp_path / "replay-out"

    code = replay_main(
        ["--from", "2026-11-11", "--to", "2026-11-13", "--workers", "1", "--out", str(out)]
    )

    printed = capsys.readouterr().out
    assert code == 0
    assert "# Replay 2026-11-11 a 2026-11-13" in printed and "Reporte y CSV en:" in printed
    assert {p.name for p in out.iterdir()} == {"report.md", "trades.csv", "ideas.csv", "equity.csv"}


# --- replay with the simulated AI ----------------------------------------------------------


def test_replay_with_the_simulated_ai_runs_the_whole_ai_path_without_touching_the_database(
    scripted, played, engine
):
    signals = [_signals(REPLAY_DAYS[0], [WIN, LOSE, GAP]), _signals(REPLAY_DAYS[1], [LOSE]),
               _signals(REPLAY_DAYS[2], [LOSE]), _signals(REPLAY_DAYS[3], [])]  # fmt: skip
    analyst = simulated_analyst(CONFIG)

    result = simulate(scripted, CONFIG, signals, capital=5000.0, analyst=analyst)

    # The simulated AI mirrors the verdicts of the rules, so the same trades happen...
    def summary(trades):
        return [(t.ticker, t.entry_day, t.exit_day, t.exit_reason) for t in trades]

    assert summary(result.trades) == summary(played.trades)
    # ...but its probabilities raise the conviction factor, so the positions are larger:
    # WIN (score 90) gets 0.60 -> factor 1.0; LOSE (score 85) gets 0.575 -> factor 0.875.
    assert {t.ticker: t.shares for t in result.trades} == {"WIN": 10, "LOSE": 17}
    assert {t.ticker: t.shares for t in played.trades} == {"WIN": 5, "LOSE": 10}
    assert (result.ai, played.ai) == ("simulated", "none")
    # One call per day with finalists; the 09:45 recheck reuses the morning's review.
    first_day = result.plans[REPLAY_DAYS[0]]
    assert first_day.used_ai and all(not i.no_ai for i in first_day.by_verdict("COMPRAR"))
    assert result.ai_calls == len(analyst.gateway._ledger.records)  # noqa: SLF001
    assert 1 <= result.ai_calls <= 3
    assert result.ai_days == result.ai_calls
    with session_scope(engine) as session:
        assert session.scalars(select(models.AICall)).all() == []
    text = report.render(result)
    assert f"Veredictos con la IA simulada ({result.ai_calls} consultas" in text
    assert "**IA simulada**: responde con reglas fijas" in text
    assert "Veredictos por reglas, sin IA." in report.render(played)


def test_replay_of_a_market_with_the_simulated_ai(market):
    rules = run_replay(market, CONFIG, date(2026, 11, 9), date(2026, 11, 13))
    simulated = run_replay(market, CONFIG, date(2026, 11, 9), date(2026, 11, 13), ai="simulated")

    assert [(i.day, i.ticker, i.verdict) for i in simulated.ideas] == [
        (i.day, i.ticker, i.verdict) for i in rules.ideas
    ]
    assert simulated.ai_calls <= len(simulated.days)
    with pytest.raises(ValueError, match="solo acepta"):
        run_replay(market, CONFIG, date(2026, 11, 9), date(2026, 11, 13), ai="real")


def test_replay_command_accepts_the_simulated_ai(market, db_url, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)

    code = replay_main(["--from", "2026-11-12", "--to", "2026-11-13", "--workers", "1",
                        "--out", str(tmp_path / "out"), "--ai", "simulated"])  # fmt: skip

    assert code == 0
    assert "Veredictos con la IA simulada" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        replay_main(["--from", "2026-11-12", "--to", "2026-11-13", "--ai", "real"])


def test_preview_shows_the_plan_of_a_past_day_and_never_uses_the_real_ai_on_it(
    market, db_url, monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", db_url)

    code = preview_main(["--date", "2026-11-12", "--ai", "simulated"])

    printed = capsys.readouterr().out
    assert code == 0
    assert "Plan del día · jue 12 nov 2026" in printed
    assert "Consultas IA usadas hoy:" in printed
    with pytest.raises(SystemExit):
        preview_main(["--date", "2026-11-12", "--ai", "real"])
    assert "la IA real no se usa sobre fechas pasadas" in capsys.readouterr().err
