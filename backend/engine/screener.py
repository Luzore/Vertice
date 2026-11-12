"""Node 4: the screener (ROADMAP section 8).

Gives every ticker a buy setup label (or none) and a score from 0 to 100 whose
breakdown is kept, so the plan can show exactly where each point came from.

The roadmap states what earns the maximum of each component; partial credit is
defined here and is deliberately simple: each sub-rule is either a yes/no or a
straight line between "nothing" and "full".
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

import pandas as pd

from core.settings import ScreenerConfig
from engine.indicators import IndicatorResult

# Component -> points at the default volatility preference (they add up to 100).
BASE_WEIGHTS = {
    "tendencia": 25.0,
    "momentum": 20.0,
    "volumen": 20.0,
    "volatilidad": 15.0,
    "catalizador": 10.0,
    "calidad_setup": 10.0,
}
DEFAULT_VOLATILITY_PREFERENCE = 0.7
MAX_LESSON_ADJUSTMENT = 15.0

PENALTY_EXTENDED = -15.0
PENALTY_NEGATIVE_NEWS = -10.0
PENALTY_LOW_QUALITY = -5.0
PENALTY_TOO_VIOLENT = -5.0

SETUPS = ("CATALIZADOR", "RUPTURA", "PULLBACK", "REBOTE")


def _value(metrics: Mapping[str, float], name: str) -> float:
    value = metrics.get(name, float("nan"))
    return float("nan") if value is None else float(value)


def _is(value: float) -> bool:
    return not math.isnan(value)


def _ramp(value: float, zero_at: float, full_at: float) -> float:
    """0 at ``zero_at``, 1 at ``full_at``, a straight line in between; 0 for NaN."""
    if math.isnan(value) or full_at == zero_at:
        return 0.0
    return min(1.0, max(0.0, (value - zero_at) / (full_at - zero_at)))


def in_uptrend(metrics: Mapping[str, float]) -> bool:
    """Price above a 50-day average that is above the 200-day one, or rising."""
    close, sma50, sma200 = (_value(metrics, n) for n in ("close", "sma_50", "sma_200"))
    if not (_is(close) and _is(sma50)) or close <= sma50:
        return False
    return (_is(sma200) and sma50 > sma200) or _value(metrics, "sma50_slope_20d") > 0


def detect_setup(metrics: Mapping[str, float], regime: str) -> tuple[str | None, list[str]]:
    """The buy setup of a ticker and the facts behind it, or (None, [])."""
    close = _value(metrics, "close")
    atr = _value(metrics, "atr_14")
    rvol = _value(metrics, "rvol_20d")
    gap = _value(metrics, "gap_pct")
    rsi2 = _value(metrics, "rsi_2")
    uptrend = in_uptrend(metrics)

    # CATALIZADOR: gap above 3% on good news or earnings, heavy volume, and it held.
    good_news = (
        _value(metrics, "eps_surprise_pct") > 0
        or _value(metrics, "news_sentiment_72h") > 0
        or _value(metrics, "days_since_earnings") <= 1
    )
    vwap = _value(metrics, "price_vs_vwap_pct")
    held = (
        vwap > 0
        if _is(vwap)
        else _value(metrics, "close_position_in_range") >= 0.5 and _value(metrics, "ret_1d") >= gap
    )
    if gap > 3 and rvol > 2 and good_news and held:
        return "CATALIZADOR", [
            f"abrió con gap de +{gap:.1f}% y {rvol:.1f} veces su volumen normal",
            "sostuvo el avance durante la sesión",
        ]

    # RUPTURA: closes at a 20-day high with volume, in an uptrend.
    if uptrend and rvol >= 1.5 and close > _value(metrics, "prior_close_max_20d"):
        notes = [f"cierra en máximo de 20 días con {rvol:.1f} veces su volumen normal"]
        if _value(metrics, "squeeze_recent") == 1.0:
            notes.append("viene de una compresión de volatilidad")
        return "RUPTURA", notes

    # PULLBACK: uptrend pulling back to its averages on low volume, or very oversold.
    if uptrend and _is(atr) and atr > 0:
        near_average = min(
            abs(close - _value(metrics, "sma_20")), abs(close - _value(metrics, "ema_21"))
        )
        retreated = near_average <= 0.75 * atr and _value(metrics, "ret_5d") <= 0
        quiet = _value(metrics, "pullback_volume_ratio") < 1.0
        if (retreated or rsi2 < 10) and quiet:
            reason = (
                "que retrocede a su promedio de 20 días"
                if retreated
                else f"con sobreventa de corto plazo (RSI de 2 días en {rsi2:.0f})"
            )
            return "PULLBACK", [
                f"tendencia alcista {reason}",
                "el retroceso viene con poco volumen",
            ]

    # REBOTE: extreme oversold in a stock still above its 200-day average.
    if (
        regime in ("ALCISTA", "NEUTRAL")
        and rsi2 < 5
        and _value(metrics, "down_days_streak") >= 3
        and close > _value(metrics, "sma_200")
    ):
        streak = int(_value(metrics, "down_days_streak"))
        return "REBOTE", [
            f"sobreventa extrema: {streak} días seguidos a la baja y RSI de 2 días en {rsi2:.0f}",
            "sigue arriba de su promedio de 200 días",
        ]
    return None, []


def component_weights(
    volatility_preference: float, adjustments: Mapping[str, float] | None = None
) -> dict[str, float]:
    """Points per component. The volatility preference scales the volatility weight
    (0 removes it, 1 raises it) and confirmed lessons may shift a component by up to
    15 points; the result is rescaled so the maximum stays 100."""
    weights = dict(BASE_WEIGHTS)
    weights["volatilidad"] *= volatility_preference / DEFAULT_VOLATILITY_PREFERENCE
    for name, delta in (adjustments or {}).items():
        if name in weights:
            bounded = max(-MAX_LESSON_ADJUSTMENT, min(MAX_LESSON_ADJUSTMENT, delta))
            weights[name] = max(0.0, weights[name] + bounded)
    total = sum(weights.values())
    return {name: 100 * weight / total for name, weight in weights.items()}


def _volatility_fit(atr_pct: float, sweet_spot: tuple[float, float], preference: float) -> float:
    """1 inside the sweet spot (tilted toward its upper end as the preference
    grows), falling to 0 at 1% on the calm side and at 12% on the violent side."""
    low, high = sweet_spot
    if math.isnan(atr_pct):
        return 0.0
    if atr_pct < low:
        return _ramp(atr_pct, 1.0, low) * (1 - max(0.0, 2 * preference - 1))
    if atr_pct > high:
        return 1 - _ramp(atr_pct, high, 12.0)
    tilt = max(0.0, 2 * preference - 1)
    return (1 - tilt) + tilt * (atr_pct - low) / (high - low)


@dataclass(frozen=True)
class Candidate:
    ticker: str
    setup: str | None
    score: float
    # Points per component, and negative points per penalty.
    breakdown: dict[str, float]
    # Facts behind the setup, in Spanish.
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def score(
    metrics: Mapping[str, float],
    setup: str | None,
    config: ScreenerConfig,
    *,
    nearest_support_atr: float = float("nan"),
    adjustments: Mapping[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """Score from 0 to 100 and its breakdown."""
    weights = component_weights(config.volatility_preference, adjustments)
    v = lambda name: _value(metrics, name)  # noqa: E731 - a local shorthand

    trend = (
        (v("close") > v("sma_20"))
        + (v("sma_20") > v("sma_50"))
        + (v("sma_50") > v("sma_200"))
        + (v("adx_14") > 20)
        + (v("adx_change_5d") > 0)
    ) / 5
    momentum = (
        0.30 * _ramp(v("rs_20d_vs_spy"), 0, 10)
        + 0.30 * _ramp(v("rs_60d_vs_spy"), 0, 15)
        + 0.20 * _ramp(v("rs_20d_vs_sector"), 0, 5)
        + 0.20 * (v("macd_hist") > 0)
    )
    volume = (
        0.50 * _ramp(v("rvol_20d"), 1.0, 1.5)
        + 0.25 * (v("obv_slope_20d") > 0)
        + 0.25 * _ramp(v("cmf_20"), 0, 0.10)
    )
    volatility = _volatility_fit(
        v("atr_pct"), config.atr_pct_sweet_spot, config.volatility_preference
    )
    catalyst = (
        0.40 * _ramp(v("eps_surprise_pct"), 0, 10)
        + 0.40 * _ramp(v("news_sentiment_72h"), 0, 0.5)
        + 0.20 * (v("news_count_anomaly") > 2 and v("news_sentiment_72h") > 0)
    )
    setup_quality = (
        0.40 * (setup is not None)
        + 0.40 * (0.8 <= nearest_support_atr <= 3.0)
        + 0.20 * (v("extension_atr") < 2)
    )
    breakdown = {
        "tendencia": weights["tendencia"] * trend,
        "momentum": weights["momentum"] * momentum,
        "volumen": weights["volumen"] * volume,
        "volatilidad": weights["volatilidad"] * volatility,
        "catalizador": weights["catalizador"] * catalyst,
        "calidad_setup": weights["calidad_setup"] * setup_quality,
    }
    if v("extension_atr") > 3:
        breakdown["penalizacion_extension"] = PENALTY_EXTENDED
    if v("news_sentiment_72h") < 0:
        breakdown["penalizacion_noticias"] = PENALTY_NEGATIVE_NEWS
    if v("data_quality") < 0.9:
        breakdown["penalizacion_calidad_datos"] = PENALTY_LOW_QUALITY
    if v("atr_pct") > 12:
        breakdown["penalizacion_volatilidad_extrema"] = PENALTY_TOO_VIOLENT
    breakdown = {name: round(points, 2) for name, points in breakdown.items()}
    return round(min(100.0, max(0.0, sum(breakdown.values()))), 2), breakdown


def screen(
    result: IndicatorResult,
    config: ScreenerConfig,
    *,
    adjustments: Mapping[str, float] | None = None,
) -> list[Candidate]:
    """Every ticker with its setup and score, best first."""
    candidates = []
    for ticker, row in result.metrics.iterrows():
        metrics = {name: float(value) for name, value in row.items() if pd.notna(value)}
        setup, notes = detect_setup(metrics, result.regime)
        levels = result.levels.get(ticker)
        atr = _value(metrics, "atr_14")
        support_atr = float("nan")
        if levels is not None and levels.supports and _is(atr) and atr > 0:
            support_atr = (metrics["close"] - levels.supports[0]) / atr
        points, breakdown = score(
            metrics, setup, config, nearest_support_atr=support_atr, adjustments=adjustments
        )
        candidates.append(Candidate(str(ticker), setup, points, breakdown, notes, metrics))
    return sorted(candidates, key=lambda c: (-c.score, c.ticker))


def top_candidates(candidates: list[Candidate], count: int) -> list[Candidate]:
    """The best ``count`` candidates that have a setup: the ones that go to the controls."""
    return [c for c in candidates if c.setup is not None][:count]
