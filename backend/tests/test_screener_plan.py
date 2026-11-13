"""Screener, aggressiveness profile, trade plan and the rule-based daily plan."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, time, timedelta

import pandas as pd
import pytest

from core import clock, settings
from engine import screener as scr
from engine.data_engine import MarketState, Quality
from engine.indicators import IndicatorResult, Levels
from engine.portfolio import Holding, Portfolio
from engine.recommender import build_plan, correlation_60d, render_plan
from engine.risk_profile import profile_for
from engine.schemas import EconEvent
from engine.screener import Candidate
from engine.trade_plan import build_trade_plan
from tests.conftest import SHIPPED_CONFIG

CONFIG = settings.load_config(SHIPPED_CONFIG)
DAY = date(2026, 11, 10)
AS_OF = clock.market_time(DAY, time(9, 0))
NAN = float("nan")

# A strong uptrend breaking out on volume: every score component at its maximum
# except the catalyst (no news) and part of volatility (middle of the sweet spot).
STRONG = {
    "close": 100.0, "sma_20": 97.0, "sma_50": 92.0, "sma_200": 80.0, "ema_21": 97.2,
    "sma50_slope_20d": 4.0, "adx_14": 28.0, "adx_change_5d": 2.0,
    "rs_20d_vs_spy": 12.0, "rs_60d_vs_spy": 18.0, "rs_20d_vs_sector": 6.0, "macd_hist": 0.5,
    "rvol_20d": 1.8, "obv_slope_20d": 0.3, "cmf_20": 0.15,
    "atr_14": 5.0, "atr_pct": 5.0, "extension_atr": 0.6, "data_quality": 1.0,
    "prior_close_max_20d": 99.0, "squeeze_recent": 0.0, "gap_pct": 0.5, "ret_1d": 1.0,
    "ret_5d": 2.0, "rsi_2": 70.0, "down_days_streak": 0.0, "pullback_volume_ratio": 1.2,
    "close_position_in_range": 0.8, "news_count_anomaly": 1.0, "dollar_volume_20d": 150e6,
}  # fmt: skip


def m(**changes) -> dict[str, float]:
    return {**STRONG, **changes}


# --- setups --------------------------------------------------------------------------------


def test_breakout():
    setup, notes = scr.detect_setup(m(), "ALCISTA")

    assert setup == "RUPTURA"
    assert notes == ["cierra en máximo de 20 días con 1.8 veces su volumen normal"]
    assert (
        "viene de una compresión de volatilidad"
        in scr.detect_setup(m(squeeze_recent=1.0), "ALCISTA")[1]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"rvol_20d": 1.49},  # not enough volume
        {"prior_close_max_20d": 100.5},  # not a 20-day closing high
        {"close": 91.0, "prior_close_max_20d": 90.0},  # below its 50-day average: no uptrend
        {"sma_200": 95.0, "sma50_slope_20d": -1.0},  # 50-day below 200-day and falling
    ],
)
def test_not_a_breakout(changes):
    assert scr.detect_setup(m(**changes), "ALCISTA")[0] != "RUPTURA"


def test_pullback_to_the_average_on_low_volume():
    pullback = m(
        close=97.5, rvol_20d=0.7, ret_5d=-2.0, pullback_volume_ratio=0.8, prior_close_max_20d=103.0
    )

    setup, notes = scr.detect_setup(pullback, "ALCISTA")

    assert setup == "PULLBACK"
    assert notes[0] == "tendencia alcista que retrocede a su promedio de 20 días"
    assert (
        scr.detect_setup({**pullback, "pullback_volume_ratio": 1.1}, "ALCISTA")[0] is None
    )  # heavy volume
    assert (
        scr.detect_setup({**pullback, "close": 104.0}, "ALCISTA")[0] is None
    )  # not near the average
    assert scr.detect_setup({**pullback, "ret_5d": 1.0}, "ALCISTA")[0] is None  # it did not retreat


def test_pullback_by_short_term_oversold():
    oversold = m(close=96.0, rsi_2=8.0, rvol_20d=0.8, pullback_volume_ratio=0.9, ret_5d=-6.0,
                 prior_close_max_20d=104.0, sma_20=101.0, ema_21=101.0)  # fmt: skip

    setup, notes = scr.detect_setup(oversold, "NEUTRAL")

    assert setup == "PULLBACK"
    assert "RSI de 2 días en 8" in notes[0]


def test_catalyst_gap_that_holds():
    gap = m(
        gap_pct=6.0, rvol_20d=3.2, ret_1d=8.0, close_position_in_range=0.9, days_since_earnings=0.0
    )

    setup, notes = scr.detect_setup(gap, "NEUTRAL")

    assert setup == "CATALIZADOR"
    assert notes[0] == "abrió con gap de +6.0% y 3.2 veces su volumen normal"
    assert (
        scr.detect_setup({**gap, "gap_pct": 3.0}, "NEUTRAL")[0] != "CATALIZADOR"
    )  # must exceed 3%
    assert (
        scr.detect_setup({**gap, "rvol_20d": 2.0}, "NEUTRAL")[0] != "CATALIZADOR"
    )  # must exceed 2x
    faded = {**gap, "ret_1d": 2.0, "close_position_in_range": 0.2}  # closed below its open
    assert scr.detect_setup(faded, "NEUTRAL")[0] != "CATALIZADOR"
    no_news = {**gap, "days_since_earnings": 9.0}
    assert scr.detect_setup(no_news, "NEUTRAL")[0] != "CATALIZADOR"
    assert scr.detect_setup({**no_news, "eps_surprise_pct": 4.0}, "NEUTRAL")[0] == "CATALIZADOR"
    assert scr.detect_setup({**no_news, "news_sentiment_72h": 0.4}, "NEUTRAL")[0] == "CATALIZADOR"


def test_catalyst_uses_the_vwap_when_there_are_intraday_bars():
    gap = m(
        gap_pct=6.0, rvol_20d=3.2, ret_1d=8.0, close_position_in_range=0.9, days_since_earnings=0.0
    )

    assert scr.detect_setup({**gap, "price_vs_vwap_pct": 0.8}, "NEUTRAL")[0] == "CATALIZADOR"
    assert scr.detect_setup({**gap, "price_vs_vwap_pct": -0.3}, "NEUTRAL")[0] != "CATALIZADOR"


BOUNCE = m(close=85.0, sma_20=95.0, ema_21=95.0, sma_50=96.0, rsi_2=3.0, down_days_streak=4.0,
           rvol_20d=1.1, prior_close_max_20d=99.0)  # fmt: skip


def test_oversold_bounce_only_outside_high_risk():
    setup, notes = scr.detect_setup(BOUNCE, "NEUTRAL")

    assert setup == "REBOTE"
    assert notes[0] == "sobreventa extrema: 4 días seguidos a la baja y RSI de 2 días en 3"
    assert scr.detect_setup(BOUNCE, "ALCISTA")[0] == "REBOTE"
    assert scr.detect_setup(BOUNCE, "RIESGO_ALTO")[0] is None


@pytest.mark.parametrize("changes", [{"rsi_2": 5.0}, {"down_days_streak": 2.0}, {"sma_200": 90.0}])
def test_not_a_bounce(changes):
    assert scr.detect_setup({**BOUNCE, **changes}, "NEUTRAL")[0] is None


def test_no_setup_without_data():
    assert scr.detect_setup({}, "ALCISTA") == (None, [])
    assert scr.detect_setup({"close": 10.0}, "ALCISTA") == (None, [])


# --- score ---------------------------------------------------------------------------------


def test_weights_add_up_to_100_and_match_the_roadmap_by_default():
    weights = scr.component_weights(0.7)

    assert weights == pytest.approx(scr.BASE_WEIGHTS)
    assert sum(weights.values()) == pytest.approx(100)
    assert weights == pytest.approx(
        {
            "tendencia": 25,
            "momentum": 20,
            "volumen": 20,
            "volatilidad": 15,
            "catalizador": 10,
            "calidad_setup": 10,
        }
    )


def test_volatility_preference_moves_the_weight_of_volatility():
    calm, default, wild = (scr.component_weights(p) for p in (0.0, 0.7, 1.0))

    assert calm["volatilidad"] == 0
    assert calm["volatilidad"] < default["volatilidad"] < wild["volatilidad"]
    assert wild["volatilidad"] == pytest.approx(100 * (15 / 0.7) / (85 + 15 / 0.7))
    for weights in (calm, default, wild):
        assert sum(weights.values()) == pytest.approx(100)


def test_lesson_adjustments_are_capped_at_15_points():
    adjusted = scr.component_weights(0.7, {"calidad_setup": -5, "volumen": 40, "desconocido": 9})

    # volume +15 at most (not +40), setup quality -5; then rescaled to 100.
    total = 25 + 20 + 35 + 15 + 10 + 5
    assert adjusted["volumen"] == pytest.approx(100 * 35 / total)
    assert adjusted["calidad_setup"] == pytest.approx(100 * 5 / total)
    assert sum(adjusted.values()) == pytest.approx(100)


def test_score_of_a_strong_breakout_by_hand():
    points, breakdown = scr.score(STRONG, "RUPTURA", CONFIG.screener, nearest_support_atr=1.5)

    # Volatility: ATR% 5.0 in the 2.5-8 band with preference 0.7 -> 0.6 + 0.4*(2.5/5.5).
    volatility = 15 * (0.6 + 0.4 * 2.5 / 5.5)
    assert breakdown == {
        "tendencia": 25.0,
        "momentum": 20.0,
        "volumen": 20.0,
        "volatilidad": round(volatility, 2),
        "catalizador": 0.0,
        "calidad_setup": 10.0,
    }
    assert points == pytest.approx(75 + round(volatility, 2))


@pytest.mark.parametrize(
    ("changes", "component", "expected"),
    [
        ({"adx_14": 18.0}, "tendencia", 20.0),  # 4 of 5 trend checks
        ({"close": 96.0, "adx_change_5d": -1.0}, "tendencia", 15.0),
        ({"rs_20d_vs_spy": 5.0}, "momentum", 17.0),  # half of the 30% for 20-day strength
        (
            {
                "rs_20d_vs_spy": -3.0,
                "rs_60d_vs_spy": -1.0,
                "rs_20d_vs_sector": -2.0,
                "macd_hist": -0.1,
            },
            "momentum",
            0.0,
        ),
        ({"rvol_20d": 1.25}, "volumen", 15.0),  # half of the 50% for relative volume
        ({"rvol_20d": 0.8, "obv_slope_20d": -0.1, "cmf_20": -0.05}, "volumen", 0.0),
        ({"atr_pct": 8.0}, "volatilidad", 15.0),  # top of the sweet spot
        ({"atr_pct": 2.5}, "volatilidad", 9.0),  # bottom of it: 60%
        ({"atr_pct": 10.0}, "volatilidad", 7.5),  # half way from 8% to 12%
        ({"atr_pct": 12.0}, "volatilidad", 0.0),
        ({"atr_pct": 1.0}, "volatilidad", 0.0),
        ({"eps_surprise_pct": 5.0}, "catalizador", 2.0),
        (
            {"eps_surprise_pct": 20.0, "news_sentiment_72h": 0.6, "news_count_anomaly": 3.0},
            "catalizador",
            10.0,
        ),
        ({"extension_atr": 2.5}, "calidad_setup", 8.0),
    ],
)
def test_score_components(changes, component, expected):
    _, breakdown = scr.score(m(**changes), "RUPTURA", CONFIG.screener, nearest_support_atr=1.5)

    assert breakdown[component] == pytest.approx(expected, abs=0.01)


def test_setup_quality_needs_a_setup_and_a_stop_level():
    _, with_all = scr.score(STRONG, "RUPTURA", CONFIG.screener, nearest_support_atr=1.5)
    _, no_setup = scr.score(STRONG, None, CONFIG.screener, nearest_support_atr=1.5)
    _, far_support = scr.score(STRONG, "RUPTURA", CONFIG.screener, nearest_support_atr=4.0)
    _, no_support = scr.score(STRONG, "RUPTURA", CONFIG.screener)

    assert (with_all["calidad_setup"], no_setup["calidad_setup"]) == (10.0, 6.0)
    assert far_support["calidad_setup"] == no_support["calidad_setup"] == 6.0


@pytest.mark.parametrize(
    ("changes", "penalty", "points"),
    [
        ({"extension_atr": 3.2}, "penalizacion_extension", -15.0),
        ({"news_sentiment_72h": -0.3}, "penalizacion_noticias", -10.0),
        ({"data_quality": 0.85}, "penalizacion_calidad_datos", -5.0),
        ({"atr_pct": 13.0}, "penalizacion_volatilidad_extrema", -5.0),
    ],
)
def test_penalties(changes, penalty, points):
    base_points, base = scr.score(STRONG, "RUPTURA", CONFIG.screener, nearest_support_atr=1.5)
    total, breakdown = scr.score(m(**changes), "RUPTURA", CONFIG.screener, nearest_support_atr=1.5)

    assert breakdown[penalty] == points
    assert penalty not in base
    assert total < base_points
    assert total == pytest.approx(sum(breakdown.values()), abs=0.01)


def test_score_stays_between_0_and_100():
    awful = {
        "close": 10.0,
        "extension_atr": 5.0,
        "news_sentiment_72h": -1.0,
        "data_quality": 0.5,
        "atr_pct": 20.0,
    }

    assert scr.score(awful, None, CONFIG.screener)[0] == 0.0
    assert scr.score({}, None, CONFIG.screener)[0] == 0.0
    perfect = m(atr_pct=8.0, eps_surprise_pct=20.0, news_sentiment_72h=0.8, news_count_anomaly=4.0)
    assert scr.score(perfect, "RUPTURA", CONFIG.screener, nearest_support_atr=1.5)[0] == 100.0


def test_screen_ranks_the_universe_and_keeps_the_breakdown():
    frame = pd.DataFrame.from_dict({"WEAK": {"close": 10.0, "sma_20": 11.0}, "TOP": STRONG, "MID": m(rvol_20d=1.0)},
                                   orient="index")  # fmt: skip
    result = IndicatorResult(AS_OF, frame, {}, "ALCISTA", [], {"TOP": Levels(supports=(93.0,))})

    ranked = scr.screen(result, CONFIG.screener)

    assert [c.ticker for c in ranked] == ["TOP", "MID", "WEAK"]
    assert (
        ranked[0].setup == "RUPTURA" and ranked[0].breakdown["calidad_setup"] == 10.0
    )  # support 1.4 ATR below
    assert ranked[1].setup is None
    assert [c.ticker for c in scr.top_candidates(ranked, 25)] == [
        "TOP"
    ]  # only the ones with a setup


# --- aggressiveness ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (1, (0.25, 3, 15, 80, 0.60, 1.5)),
        (3, (0.50, 4, 20, 75, 0.55, 1.75)),
        (5, (0.75, 5, 25, 70, 0.50, 2.0)),
        (7, (1.00, 6, 30, 65, 0.47, 2.0)),
        (10, (1.50, 8, 35, 55, 0.42, 2.5)),
        # In between: interpolated; the number of positions is rounded down.
        (2, (0.375, 3, 17.5, 77.5, 0.575, 1.625)),
        (4, (0.625, 4, 22.5, 72.5, 0.525, 1.875)),
        (6, (0.875, 5, 27.5, 67.5, 0.485, 2.0)),
        (8, (1.1667, 6, 31.6667, 61.6667, 0.4533, 2.1667)),
        (9, (1.3333, 7, 33.3333, 58.3333, 0.4367, 2.3333)),
    ],
)
def test_aggressiveness_table(level, expected):
    p = profile_for(level)

    assert (
        p.risk_pct,
        p.max_positions,
        p.max_position_pct,
        p.min_score,
        p.min_probability,
        p.stop_atr,
    ) == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize("level", [0, 11, -1])
def test_aggressiveness_out_of_range(level):
    with pytest.raises(ValueError, match="between 1 and 10"):
        profile_for(level)


# --- trade plan ----------------------------------------------------------------------------

PLAN_METRICS = {
    "close": 100.0,
    "atr_14": 2.0,
    "sma_20": 99.5,
    "sma_50": 93.0,
    "ema_21": 99.6,
    "donchian_low_20": 91.0,
}


def test_plan_puts_the_stop_under_the_level_closest_to_the_profile_distance():
    levels = Levels(supports=(98.9, 96.5, 90.0), resistances=(108.0, 115.0))

    plan = build_trade_plan(PLAN_METRICS, levels, profile_for(5))  # wants a stop about 2 ATR away

    # 96.5 - 0.1 ATR = 96.3, which is 1.85 ATR below: the closest to 2 ATR.
    assert (plan.stop, plan.stop_level, plan.stop_level_name) == (96.3, 96.5, "soporte")
    assert plan.stop_atr == pytest.approx(1.85)
    assert (plan.entry_min, plan.entry_max, plan.no_chase_above) == (99.5, 100.5, 101.5)
    assert plan.targets == (108.0, 115.0)
    assert plan.risk_per_share == pytest.approx(3.7)
    assert plan.reward_r == pytest.approx(8 / 3.7)
    assert plan.r_multiple(plan.stop) == pytest.approx(-1.0)


def test_plan_can_use_a_moving_average_as_the_level():
    plan = build_trade_plan({**PLAN_METRICS, "sma_50": 96.4}, Levels(), profile_for(5))

    assert (plan.stop_level, plan.stop_level_name) == (96.4, "promedio de 50 días")
    assert plan.stop == 96.2


def test_plan_without_a_level_in_range_falls_back_to_atr_and_says_so():
    metrics = {"close": 100.0, "atr_14": 2.0, "sma_20": 99.5, "sma_50": 80.0}  # too close / too far

    plan = build_trade_plan(metrics, Levels(supports=(99.0,)), profile_for(5))

    assert plan.stop == 96.0  # 2 ATR below
    assert plan.stop_level is None  # control C14 fails on this


def test_plan_targets_without_resistance_are_multiples_of_r():
    plan = build_trade_plan(PLAN_METRICS, Levels(supports=(96.5,)), profile_for(5))

    assert plan.targets == (107.4, 111.1)  # +2R and +3R with R = 3.7
    assert plan.reward_r == pytest.approx(2.0)


def test_plan_second_target_skips_a_resistance_too_close_to_the_first():
    plan = build_trade_plan(
        PLAN_METRICS, Levels(supports=(96.5,), resistances=(108.0, 108.9, 118.0)), profile_for(5)
    )

    assert plan.targets == (108.0, 118.0)


@pytest.mark.parametrize(
    "metrics",
    [{}, {"close": 100.0}, {"close": 100.0, "atr_14": NAN}, {"close": 100.0, "atr_14": 0.0}],
)
def test_no_plan_without_price_and_atr(metrics):
    assert build_trade_plan(metrics, Levels(), profile_for(5)) is None


def test_more_aggressive_profiles_accept_wider_stops():
    levels = Levels(supports=(98.0, 95.4))

    prudent = build_trade_plan(PLAN_METRICS, levels, profile_for(1))  # 1.5 ATR
    aggressive = build_trade_plan(PLAN_METRICS, levels, profile_for(10))  # 2.5 ATR

    assert prudent.stop_level == 98.0 and aggressive.stop_level == 95.4


# --- the daily plan --------------------------------------------------------------------------


def candidate(
    ticker: str, score: float, setup: str | None = "RUPTURA", **metric_changes
) -> Candidate:
    metrics = m(close=50.0, atr_14=1.0, sma_20=49.6, sma_50=47.0, sma_200=40.0, ema_21=49.6,
                extension_atr=0.4, **metric_changes)  # fmt: skip
    return Candidate(
        ticker, setup, score, {"tendencia": 25.0}, ["rompe máximo de 20 días"], metrics
    )


def state_and_indicators(candidates: list[Candidate], *, regime: str = "ALCISTA", caps: dict[str, float] | None = None,
                         events: list[EconEvent] | None = None, sectors: dict[str, str] | None = None):  # fmt: skip
    tickers = [c.ticker for c in candidates]
    universe = pd.DataFrame(
        {
            "name": [f"{t} Corp." for t in tickers],
            "exchange": "NYSE",
            "sector": "Tecnología",
            "sector_etf": [(sectors or {}).get(t, f"S{i}") for i, t in enumerate(tickers)],
            "market_cap_usd": [(caps or {}).get(t, 5e9) for t in tickers],
            "sic_listed": False,
        },
        index=pd.Index(tickers, name="ticker"),
    )
    state = MarketState(as_of=AS_OF, session_date=date(2026, 11, 9), universe=universe, daily={},
                        econ_events=events or [], quality={t: Quality(1.0) for t in tickers})  # fmt: skip
    # A support 1.6 below gives a stop of 48.3 (1.7 ATR) and targets at +2R and +3R.
    levels = {t: Levels(supports=(48.4,)) for t in tickers}
    indicators = IndicatorResult(AS_OF, pd.DataFrame(), {"vix_level": 15.2, "breadth_pct_above_sma50": 62.0},
                                 regime, ["SPY arriba de sus promedios"], levels)  # fmt: skip
    return state, indicators


def plan_for(candidates, portfolio=None, config=CONFIG, **kwargs):
    opening_prices = kwargs.pop("opening_prices", None)
    state, indicators = state_and_indicators(candidates, **kwargs)
    return build_plan(state, indicators, portfolio or Portfolio(cash=5000.0), config,
                      candidates=candidates, opening_prices=opening_prices)  # fmt: skip


EIGHT = [candidate(f"T{i}", 90.0 - i) for i in range(8)]


def test_plan_lists_buys_first_then_watch_then_rejected():
    mixed = [candidate("BUY", 85.0), candidate("NEAR", 65.0), candidate("LOW", 40.0),
             candidate("CAP", 88.0), candidate("EARN", 80.0, days_to_earnings=1.0)]  # fmt: skip

    plan = plan_for(mixed, caps={"CAP": 4e8})

    assert [(i.ticker, i.verdict) for i in plan.ideas] == [
        ("BUY", "COMPRAR"), ("NEAR", "VIGILAR"), ("CAP", "NO_HOY"), ("EARN", "NO_HOY"), ("LOW", "NO_HOY"),
    ]  # fmt: skip
    by_ticker = {i.ticker: i for i in plan.ideas}
    assert by_ticker["NEAR"].reason == "puntaje 65, cerca del mínimo de 70"
    assert by_ticker["NEAR"].watch_trigger.startswith("cierre de vela de 5 min arriba de $50.25")
    assert by_ticker["LOW"].reason == "puntaje 40; el mínimo es 70"
    assert (
        by_ticker["CAP"].reason
        == "C05 Tamaño de empresa: capitalización $400 millones; el mínimo es $1.0 mil millones"
    )
    assert "C08 Resultados" in by_ticker["EARN"].reason
    assert all(len(i.controls) == 22 for i in plan.ideas)
    assert all(i.no_ai for i in plan.ideas)


def test_plan_always_lists_at_least_five_ideas_even_with_few_setups():
    few = [candidate("A", 85.0), candidate("B", 60.0, setup=None), candidate("C", 55.0, setup=None),
           candidate("D", 50.0, setup=None), candidate("E", 45.0, setup=None), candidate("F", 44.0, setup=None)]  # fmt: skip

    plan = plan_for(few)

    assert len(plan.ideas) == 5
    assert [i.verdict for i in plan.ideas] == ["COMPRAR", "NO_HOY", "NO_HOY", "NO_HOY", "NO_HOY"]
    assert {i.reason for i in plan.ideas[1:]} == {"sin setup de compra"}
    assert (plan.screened, plan.with_setup) == (6, 1)


def test_plan_never_forces_a_purchase():
    """High-risk regime and mediocre scores: five ideas listed, none to buy."""
    weak = [candidate(f"W{i}", 75.0 - i) for i in range(6)]

    plan = plan_for(weak, regime="RIESGO_ALTO")

    assert len(plan.ideas) >= 5
    assert plan.by_verdict("COMPRAR") == []
    assert all("C10 Régimen: RIESGO ALTO" in i.reason for i in plan.ideas)
    text = render_plan(plan, CONFIG, capital=5000.0)
    assert "Hoy no hay ninguna idea en COMPRAR: ninguna supera todos los controles." in text


def test_sizing_respects_cash_risk_and_position_limits():
    plan = plan_for(EIGHT)
    buys = plan.by_verdict("COMPRAR")
    profile = profile_for(5)

    assert len(buys) == profile.max_positions == 5
    assert sum(i.amount for i in buys) <= 5000.0
    for idea in buys:
        assert idea.shares == int(idea.shares) and idea.shares >= 1
        assert idea.amount <= profile.max_position_pct / 100 * 5000 + 1e-9  # 25% = $1,250
        assert idea.risk_usd <= profile.risk_pct / 100 * 5000 + 1e-9  # 0.75% = $37.50
        assert idea.amount == pytest.approx(idea.shares * idea.plan.entry_max)
        # Without the AI there is no probability: the conviction factor is the lowest.
        assert idea.multipliers == {"conviccion": 0.5, "controles": 1.0, "racha": 1.0}
    # Risk 37.50 x 0.5 = 18.75 over the worst-case loss per share (50.25 - 48.30 = 1.95)
    # -> 9 shares.
    assert (buys[0].shares, buys[0].amount) == (9, pytest.approx(9 * 50.25))
    assert buys[0].risk_usd == pytest.approx(9 * 1.95)
    waiting = [i for i in plan.ideas if i.verdict == "VIGILAR"]
    assert [i.reason for i in waiting] == ["sin espacio: el límite es de 5 posiciones"] * 3


def test_sizing_never_spends_more_than_the_cash():
    plan = plan_for(EIGHT, portfolio=Portfolio(cash=2600.0))
    buys = plan.by_verdict("COMPRAR")

    assert sum(i.amount for i in buys) <= 2600.0
    # Capital 2,600: 0.75% x 0.5 is 9.75 of risk over 1.95 per share -> 5 shares each.
    assert [i.shares for i in buys] == [5, 5, 5, 5, 5]
    assert plan.by_verdict("VIGILAR")[0].reason == "sin espacio: el límite es de 5 posiciones"


def test_the_last_idea_gets_only_the_cash_that_is_left():
    held = tuple(
        Holding(f"H{i}", f"X{i}", 10, 110.0, 110.0, 100.0, date(2026, 11, 5)) for i in range(2)
    )

    plan = plan_for(EIGHT[:3], portfolio=Portfolio(cash=700.0, holdings=held))  # capital 2,900

    # 0.75% x 0.5 of 2,900 is 10.88 -> 5 shares (251.25 each). The third finds 197.50 left:
    # 3 shares.
    assert [(i.verdict, i.shares) for i in plan.ideas] == [
        ("COMPRAR", 5),
        ("COMPRAR", 5),
        ("COMPRAR", 3),
    ]
    assert [i.limited_by for i in plan.ideas] == [None, None, "efectivo disponible"]


def test_no_cash_means_buy_if_you_free_cash():
    # Capital 5,000 but only 60 in cash: one share of the best idea, nothing for the rest.
    held = tuple(
        Holding(f"H{i}", f"X{i}", 10, 247.0, 247.0, 240.0, date(2026, 11, 5)) for i in range(2)
    )

    plan = plan_for(EIGHT[:3], portfolio=Portfolio(cash=60.0, holdings=held))

    assert [(i.ticker, i.verdict, i.shares) for i in plan.ideas] == [
        ("T0", "COMPRAR", 1), ("T1", "VIGILAR", 0), ("T2", "VIGILAR", 0),
    ]  # fmt: skip
    assert {i.reason for i in plan.ideas[1:]} == {"COMPRAR si liberas efectivo"}


def test_account_too_small_for_one_share_at_the_allowed_risk():
    plan = plan_for(EIGHT[:5], portfolio=Portfolio(cash=120.0))

    assert plan.by_verdict("COMPRAR") == []
    assert plan.ideas[0].reason == (
        "el riesgo permitido para esta idea ($0.45) no alcanza para una acción "
        "(riesgo por acción $1.95)"
    )


def test_open_positions_count_against_the_limits():
    held = tuple(
        Holding(f"H{i}", f"X{i}", 10, 40.0, 40.0, 38.0, date(2026, 11, 5)) for i in range(3)
    )

    plan = plan_for(EIGHT, portfolio=Portfolio(cash=3800.0, holdings=held))

    assert len(plan.by_verdict("COMPRAR")) == 2  # 5 allowed, 3 already open


def test_a_ticker_already_held_is_not_bought_again():
    held = (Holding("T0", "S0", 10, 48.0, 50.0, 46.0, date(2026, 11, 5)),)

    plan = plan_for(EIGHT[:5], portfolio=Portfolio(cash=4500.0, holdings=held))

    t0 = next(i for i in plan.ideas if i.ticker == "T0")
    assert (t0.verdict, t0.reason) == ("VIGILAR", "ya tienes una posición abierta en este ticker")


def test_sector_limit_caps_the_position():
    same_sector = {f"T{i}": "XLK" for i in range(8)}
    held = (Holding("H", "XLK", 30, 50.0, 50.0, 47.0, date(2026, 11, 5)),)  # 1,500 = 30% of 5,000

    plan = plan_for(EIGHT, portfolio=Portfolio(cash=3500.0, holdings=held), sectors=same_sector)
    buys = plan.by_verdict("COMPRAR")

    # 40% of 5,000 is 2,000; 1,500 is already there, so only 500 more fits: 9 shares.
    assert [(i.ticker, i.shares) for i in buys] == [("T0", 9)]


def test_reducing_controls_halve_the_size():
    fomc = EconEvent(
        name="FOMC",
        country="US",
        impact="high",
        source="config",
        scheduled_at=AS_OF + timedelta(hours=5),
    )

    normal = plan_for(EIGHT[:1]).ideas[0]
    reduced = plan_for(EIGHT[:1], events=[fomc]).ideas[0]

    assert (normal.size_multiplier, reduced.size_multiplier) == (1.0, 0.5)
    assert (normal.shares, reduced.shares) == (9, 4)
    assert reduced.multipliers == {"conviccion": 0.5, "controles": 0.5, "racha": 1.0}
    assert reduced.verdict == "COMPRAR"


def test_watch_only_mode_proposes_nothing_to_buy():
    watch_only = CONFIG.model_copy(
        update={"system": CONFIG.system.model_copy(update={"mode": "watch_only"})}
    )

    plan = plan_for(EIGHT[:5], config=watch_only)

    assert plan.by_verdict("COMPRAR") == []
    assert plan.ideas[0].reason == "el sistema está en modo solo vigilar: no propone compras"


def test_recheck_after_the_open_moves_chased_ideas_to_watch_and_blocks_big_gaps():
    plan = plan_for(EIGHT[:3], opening_prices={"T0": 50.2, "T1": 51.0, "T2": 52.3})
    by_ticker = {i.ticker: i for i in plan.ideas}

    assert by_ticker["T0"].verdict == "COMPRAR"
    assert by_ticker["T1"].verdict == "VIGILAR"  # 51.00 is above the no-chase price of 50.75
    assert by_ticker["T1"].reason == "abrió en $51.00, arriba del precio de no perseguir ($50.75)"
    assert by_ticker["T2"].verdict == "NO_HOY"  # a gap of 2.3 ATR: control C11
    assert "C11 Gap: abrió 2.3 ATR arriba" in by_ticker["T2"].reason


def test_plan_text():
    fomc = EconEvent(
        name="FOMC",
        country="US",
        impact="high",
        source="config",
        scheduled_at=clock.market_time(DAY, time(14, 0)),
    )
    plan = plan_for(
        [candidate("XYZ", 82.0), candidate("NEAR", 65.0), candidate("LOW", 30.0)], events=[fomc]
    )

    text = render_plan(plan, CONFIG, capital=5000.0)

    assert text.splitlines()[0] == "VERTICE · Plan del día · mar 10 nov 2026 · Régimen ALCISTA"
    assert (
        "VIX en 15.2." in text
        and "62% de las acciones cotiza sobre su promedio de 50 días." in text
    )
    assert "Eventos de hoy: FOMC 14:00 NY." in text
    assert "COMPRAR  XYZ  (RUPTURA · puntaje 82 · sin IA)" in text
    assert "Entrada: límite 49.75–50.25 (no perseguir sobre 50.75)" in text
    assert "Stop: 48.30 (−1R)   Objetivos: 53.40 (+2.0R) / 55.10 (+3.0R)" in text
    assert "Tamaño: 4 acciones ≈ $201 (4% del capital) · riesgo $8" in text  # halved by the FOMC
    # Probability 0.50 assumed, 2.0R to the target, costs of 0.20R: 1.0 - 0.5 - 0.2.
    assert "Expectativa: +0.30R después de costos" in text
    assert "Cómo entrar: orden límite dentro de la zona, nunca a mercado" in text
    assert "primeros 15 minutos" in text
    assert "Por qué: rompe máximo de 20 días" in text
    assert "Avisos: C09 FOMC a las 14:00 NY · 13:00 Monterrey: tamaño a la mitad" in text
    assert "VIGILAR  NEAR" in text and "Se activa si: cierre de vela de 5 min" in text
    assert "NO HOY  LOW" in text and "Motivo: puntaje 30; el mínimo es 70" in text


def test_correlation_of_two_tickers():
    index = pd.bdate_range("2026-01-02", periods=80)
    base = pd.Series(range(80), index=index, dtype=float).apply(
        lambda i: 100 + math.sin(i / 3) * 5 + i * 0.1
    )
    same = pd.DataFrame({"close": base * 2})
    opposite = pd.DataFrame({"close": 300 - base})

    assert correlation_60d(pd.DataFrame({"close": base}), same) == pytest.approx(1.0)
    assert correlation_60d(pd.DataFrame({"close": base}), opposite) < -0.9
    assert math.isnan(correlation_60d(pd.DataFrame({"close": base}), None))
    assert math.isnan(correlation_60d(pd.DataFrame({"close": base.iloc[:10]}), same))


def test_idea_keeps_the_plan_levels_unchanged():
    idea = plan_for(EIGHT[:1]).ideas[0]

    assert idea.plan == replace(idea.plan)  # frozen: the levels cannot be edited after the fact
    assert (idea.plan.stop, idea.plan.stop_level_name) == (48.3, "soporte")
