"""Node 3: indicators (ROADMAP section 7).

Every formula is written here, with a unit test against known values, so each
number in the plan can be traced and verified (principle 3: the code computes the
numbers, not the AI).

Conventions, the same as the classic definitions:

* moving averages and smoothed series are NaN until there is enough history;
* EMA is seeded with the simple average of its first ``n`` values;
* RSI, ATR and ADX use Wilder's smoothing (alpha = 1/n), seeded the same way;
* Bollinger bands use the population standard deviation;
* returns and percentages are in percent (5.0 means 5%).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from core import clock, market_calendar
from core.settings import RegimeConfig
from db import models
from engine.data_engine import MarketState, _upsert
from engine.schemas import CorporateEvent, NewsItem
from engine.universe import SECTOR_ETFS

NAN = float("nan")
TRADING_DAYS_PER_YEAR = 252
MARKET_ROW = "_MARKET"


# --- primitives: arrays in, arrays out ---------------------------------------------


def _array(values: Iterable[float]) -> np.ndarray:
    return np.asarray(values, dtype=float)


def sma(values: Iterable[float], n: int) -> np.ndarray:
    """Simple moving average of the last ``n`` values."""
    return pd.Series(_array(values)).rolling(n).mean().to_numpy()


def _smoothed(values: Iterable[float], n: int, alpha: float) -> np.ndarray:
    """Exponential smoothing seeded with the mean of the first ``n`` valid values."""
    data = _array(values)
    out = np.full(len(data), NAN)
    valid = np.flatnonzero(~np.isnan(data))
    if len(valid) < n:
        return out
    start = valid[0]
    seed = data[start : start + n].mean()
    tail = np.concatenate(([seed], data[start + n :]))
    out[start + n - 1 :] = pd.Series(tail).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return out


def ema(values: Iterable[float], n: int) -> np.ndarray:
    """Exponential moving average with alpha = 2 / (n + 1)."""
    return _smoothed(values, n, 2 / (n + 1))


def wilder(values: Iterable[float], n: int) -> np.ndarray:
    """Wilder's smoothing (alpha = 1 / n), used by RSI, ATR and ADX."""
    return _smoothed(values, n, 1 / n)


def rsi(close: Iterable[float], n: int = 14) -> np.ndarray:
    """Relative Strength Index: 100 when every change is a gain, 0 when every one is a loss."""
    prices = _array(close)
    out = np.full(len(prices), NAN)
    if len(prices) <= n:
        return out
    change = np.diff(prices)
    gain = wilder(np.clip(change, 0, None), n)
    loss = wilder(np.clip(-change, 0, None), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100 - 100 / (1 + gain / loss)
    value = np.where(loss == 0, np.where(gain == 0, 50.0, 100.0), value)
    out[1:] = np.where(np.isnan(gain), NAN, value)
    return out


def macd(
    close: Iterable[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD line (fast EMA minus slow EMA), its signal line and the histogram."""
    prices = _array(close)
    line = ema(prices, fast) - ema(prices, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def true_range(high: Iterable[float], low: Iterable[float], close: Iterable[float]) -> np.ndarray:
    """Largest of: high-low, |high-previous close|, |low-previous close|."""
    h, low_, c = _array(high), _array(low), _array(close)
    if len(h) == 0:
        return h
    previous = np.concatenate(([c[0]], c[:-1]))
    ranges = np.maximum(h - low_, np.maximum(np.abs(h - previous), np.abs(low_ - previous)))
    ranges[0] = h[0] - low_[0]
    return ranges


def atr(
    high: Iterable[float], low: Iterable[float], close: Iterable[float], n: int = 14
) -> np.ndarray:
    """Average True Range (Wilder)."""
    return wilder(true_range(high, low, close), n)


def adx(
    high: Iterable[float], low: Iterable[float], close: Iterable[float], n: int = 14
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Average Directional Index with +DI and -DI (Wilder)."""
    h, low_, c = _array(high), _array(low), _array(close)
    size = len(h)
    empty = np.full(size, NAN)
    if size < 2:
        return empty, empty.copy(), empty.copy()
    up = h[1:] - h[:-1]
    down = low_[:-1] - low_[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    ranges = wilder(true_range(h, low_, c)[1:], n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * wilder(plus_dm, n) / ranges
        minus_di = 100 * wilder(minus_dm, n) / ranges
        total = plus_di + minus_di
        dx = np.where(total == 0, 0.0, 100 * np.abs(plus_di - minus_di) / total)
    dx = np.where(np.isnan(plus_di), NAN, dx)
    pad = np.array([NAN])
    return (
        np.concatenate((pad, wilder(dx, n))),
        np.concatenate((pad, plus_di)),
        np.concatenate((pad, minus_di)),
    )


def obv(close: Iterable[float], volume: Iterable[float]) -> np.ndarray:
    """On-Balance Volume: adds the day's volume on an up close, subtracts it on a down close."""
    prices, vol = _array(close), _array(volume)
    if len(prices) == 0:
        return prices
    direction = np.sign(np.diff(prices))
    return np.concatenate(([0.0], np.cumsum(direction * vol[1:])))


def mfi(
    high: Iterable[float],
    low: Iterable[float],
    close: Iterable[float],
    volume: Iterable[float],
    n: int = 14,
) -> np.ndarray:
    """Money Flow Index: an RSI weighted by volume."""
    typical = (_array(high) + _array(low) + _array(close)) / 3
    out = np.full(len(typical), NAN)
    if len(typical) <= n:
        return out
    flow = typical * _array(volume)
    change = np.diff(typical)
    positive = pd.Series(np.where(change > 0, flow[1:], 0.0)).rolling(n).sum().to_numpy()
    negative = pd.Series(np.where(change < 0, flow[1:], 0.0)).rolling(n).sum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100 - 100 / (1 + positive / negative)
    value = np.where(negative == 0, np.where(positive == 0, 50.0, 100.0), value)
    out[1:] = np.where(np.isnan(positive), NAN, value)
    return out


def cmf(
    high: Iterable[float],
    low: Iterable[float],
    close: Iterable[float],
    volume: Iterable[float],
    n: int = 20,
) -> np.ndarray:
    """Chaikin Money Flow: +1 when every close is at the high, -1 at the low."""
    h, low_, c, vol = _array(high), _array(low), _array(close), _array(volume)
    spread = h - low_
    with np.errstate(divide="ignore", invalid="ignore"):
        multiplier = np.where(spread == 0, 0.0, ((c - low_) - (h - c)) / spread)
    money = pd.Series(multiplier * vol).rolling(n).sum().to_numpy()
    total = pd.Series(vol).rolling(n).sum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total == 0, 0.0, money / total)


def bollinger(
    close: Iterable[float], n: int = 20, k: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Middle, upper and lower Bollinger bands."""
    series = pd.Series(_array(close))
    middle = series.rolling(n).mean().to_numpy()
    deviation = series.rolling(n).std(ddof=0).to_numpy()
    return middle, middle + k * deviation, middle - k * deviation


def keltner(
    high: Iterable[float],
    low: Iterable[float],
    close: Iterable[float],
    n: int = 20,
    multiplier: float = 1.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Keltner channel: EMA plus and minus a multiple of the ATR."""
    middle = ema(close, n)
    width = multiplier * atr(high, low, close, n)
    return middle, middle + width, middle - width


def realized_volatility(close: Iterable[float], n: int) -> np.ndarray:
    """Annualized standard deviation of daily log returns, in percent."""
    prices = _array(close)
    out = np.full(len(prices), NAN)
    if len(prices) <= n:
        return out
    returns = pd.Series(np.diff(np.log(prices)))
    out[1:] = returns.rolling(n).std(ddof=1).to_numpy() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100
    return out


def pct_return(close: Iterable[float], n: int) -> float:
    """Percent change over the last ``n`` bars; NaN without enough history."""
    prices = _array(close)
    if len(prices) <= n or prices[-1 - n] == 0:
        return NAN
    return (prices[-1] / prices[-1 - n] - 1) * 100


def linear_slope(values: Iterable[float]) -> float:
    """Least-squares slope per step of a series."""
    data = _array(values)
    if len(data) < 2 or np.isnan(data).any():
        return NAN
    x = np.arange(len(data), dtype=float)
    x -= x.mean()
    return float((x * (data - data.mean())).sum() / (x * x).sum())


def beta(returns: Iterable[float], benchmark_returns: Iterable[float]) -> float:
    """Sensitivity to the benchmark: covariance over the benchmark's variance."""
    own, bench = _array(returns), _array(benchmark_returns)
    if len(own) != len(bench) or len(own) < 3:
        return NAN
    variance = bench.var(ddof=1)
    if variance == 0:
        return NAN
    return float(np.cov(own, bench, ddof=1)[0, 1] / variance)


def pivot_points(
    high: Iterable[float], low: Iterable[float], span: int = 3
) -> tuple[list[int], list[int]]:
    """Indexes of swing highs and swing lows.

    A swing high is a bar whose high exceeds the ``span`` bars before it and is not
    exceeded by the ``span`` bars after it. The last ``span`` bars cannot be pivots
    yet: confirming one needs bars that have not happened.
    """
    h, low_ = _array(high), _array(low)
    highs, lows = [], []
    for index in range(span, len(h) - span):
        before_h, after_h = h[index - span : index], h[index + 1 : index + span + 1]
        if h[index] > before_h.max() and h[index] >= after_h.max():
            highs.append(index)
        before_l, after_l = low_[index - span : index], low_[index + 1 : index + span + 1]
        if low_[index] < before_l.min() and low_[index] <= after_l.min():
            lows.append(index)
    return highs, lows


# --- levels ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Levels:
    """Valid price levels for entries, stops and targets (principle 3)."""

    supports: tuple[float, ...] = ()
    resistances: tuple[float, ...] = ()

    @property
    def nearest_support(self) -> float:
        return self.supports[0] if self.supports else NAN

    @property
    def nearest_resistance(self) -> float:
        return self.resistances[0] if self.resistances else NAN


def _merge_close_levels(levels: list[float], tolerance: float) -> list[float]:
    merged: list[float] = []
    for level in levels:
        if not merged or abs(level - merged[-1]) > tolerance:
            merged.append(level)
    return merged


def support_resistance(
    bars: pd.DataFrame, atr_value: float, *, lookback: int = 60, span: int = 3
) -> Levels:
    """Supports below and resistances above the last close, nearest first, from the
    swing points of the last ``lookback`` sessions. Levels closer than half an ATR
    to each other count as one."""
    window = bars.iloc[-lookback:]
    highs, lows = pivot_points(window["high"], window["low"], span)
    close = float(window["close"].iloc[-1])
    tolerance = 0.5 * atr_value if atr_value and not math.isnan(atr_value) else 0.0
    high_values = window["high"].to_numpy()
    low_values = window["low"].to_numpy()
    below = sorted(
        {round(float(low_values[i]), 4) for i in lows if low_values[i] < close}, reverse=True
    )
    above = sorted({round(float(high_values[i]), 4) for i in highs if high_values[i] > close})
    return Levels(
        supports=tuple(_merge_close_levels(below, tolerance)),
        resistances=tuple(_merge_close_levels(above, tolerance)),
    )


# --- metrics per ticker ------------------------------------------------------------------


def _last(values: np.ndarray) -> float:
    return float(values[-1]) if len(values) else NAN


def _ratio(numerator: float, denominator: float) -> float:
    if denominator == 0 or math.isnan(denominator) or math.isnan(numerator):
        return NAN
    return numerator / denominator


def daily_metrics(
    bars: pd.DataFrame,
    *,
    benchmark: pd.DataFrame | None = None,
    sector: pd.DataFrame | None = None,
) -> tuple[dict[str, float], Levels]:
    """Trend, momentum, relative strength, volume, volatility and level metrics
    from one ticker's daily bars (oldest first)."""
    o = bars["open"].to_numpy(dtype=float)
    h = bars["high"].to_numpy(dtype=float)
    low_ = bars["low"].to_numpy(dtype=float)
    c = bars["close"].to_numpy(dtype=float)
    v = bars["volume"].to_numpy(dtype=float)
    size = len(c)
    close = c[-1]
    m: dict[str, float] = {"close": close}

    # Trend
    sma20, sma50, sma200 = sma(c, 20), sma(c, 50), sma(c, 200)
    m["sma_20"], m["sma_50"], m["sma_200"] = _last(sma20), _last(sma50), _last(sma200)
    m["ema_9"], m["ema_21"] = _last(ema(c, 9)), _last(ema(c, 21))
    m["sma50_slope_20d"] = (_ratio(sma50[-1], sma50[-21]) - 1) * 100 if size >= 71 else NAN
    adx_line, plus_di, minus_di = adx(h, low_, c, 14)
    m["adx_14"], m["plus_di_14"], m["minus_di_14"] = (
        _last(adx_line),
        _last(plus_di),
        _last(minus_di),
    )
    m["adx_change_5d"] = adx_line[-1] - adx_line[-6] if size >= 6 else NAN

    # Momentum
    m["rsi_14"], m["rsi_2"] = _last(rsi(c, 14)), _last(rsi(c, 2))
    macd_line, macd_signal, macd_hist = macd(c)
    m["macd_line"], m["macd_signal"], m["macd_hist"] = (
        _last(macd_line),
        _last(macd_signal),
        _last(macd_hist),
    )
    for days in (1, 5, 20, 60):
        m[f"ret_{days}d"] = pct_return(c, days)
    down_streak = 0
    for index in range(size - 1, 0, -1):
        if c[index] < c[index - 1]:
            down_streak += 1
        else:
            break
    m["down_days_streak"] = float(down_streak)

    # Relative strength
    for days in (20, 60):
        other = pct_return(benchmark["close"], days) if benchmark is not None else NAN
        m[f"rs_{days}d_vs_spy"] = m[f"ret_{days}d"] - other
    sector_return = pct_return(sector["close"], 20) if sector is not None else NAN
    m["rs_20d_vs_sector"] = m["ret_20d"] - sector_return

    # Volume
    m["rvol_20d"] = _ratio(v[-1], v[-21:-1].mean()) if size >= 21 else NAN
    m["dollar_volume_20d"] = float((c[-20:] * v[-20:]).mean()) if size >= 20 else NAN
    if size >= 20:
        mean_volume = v[-20:].mean()
        m["obv_slope_20d"] = _ratio(linear_slope(obv(c, v)[-20:]), mean_volume)
    else:
        m["obv_slope_20d"] = NAN
    # Volume of the last 3 sessions against the 20-day average: below 1 is a quiet pullback.
    m["pullback_volume_ratio"] = _ratio(v[-3:].mean(), v[-20:].mean()) if size >= 20 else NAN
    m["mfi_14"], m["cmf_20"] = _last(mfi(h, low_, c, v, 14)), _last(cmf(h, low_, c, v, 20))
    if size >= 21:
        change = np.diff(c[-21:])
        up_volume = v[-20:][change > 0].sum()
        down_volume = v[-20:][change < 0].sum()
        m["up_down_volume_ratio_20d"] = _ratio(up_volume, down_volume)
    else:
        m["up_down_volume_ratio_20d"] = NAN

    # Volatility
    atr_line = atr(h, low_, c, 14)
    atr_value = _last(atr_line)
    m["atr_14"] = atr_value
    m["atr_pct"] = _ratio(atr_value, close) * 100
    m["realized_vol_10d"] = _last(realized_volatility(c, 10))
    m["realized_vol_20d"] = _last(realized_volatility(c, 20))
    middle, upper, lower = bollinger(c, 20, 2.0)
    m["bb_percent_b"] = _ratio(close - lower[-1], upper[-1] - lower[-1])
    m["bb_width"] = _ratio(upper[-1] - lower[-1], middle[-1])
    _, keltner_upper, keltner_lower = keltner(h, low_, c, 20, 1.5)
    with np.errstate(invalid="ignore"):
        squeezed = (upper < keltner_upper) & (lower > keltner_lower)
    m["squeeze"] = float(squeezed[-1]) if not math.isnan(keltner_upper[-1]) else NAN
    m["squeeze_recent"] = (
        float(squeezed[-10:].any()) if size >= 10 and not math.isnan(keltner_upper[-1]) else NAN
    )
    m["range_vs_atr"] = _ratio(h[-1] - low_[-1], atr_value)
    # Where the close sits in the day's range: 1 at the high, 0 at the low.
    m["close_position_in_range"] = _ratio(close - low_[-1], h[-1] - low_[-1])
    m["gap_pct"] = (_ratio(o[-1], c[-2]) - 1) * 100 if size >= 2 else NAN
    m["gap_atr"] = _ratio(o[-1] - c[-2], atr_line[-2]) if size >= 2 else NAN
    m["beta_60d"] = NAN
    if benchmark is not None and size >= 61:
        joined = pd.concat([bars["close"], benchmark["close"]], axis=1, join="inner").tail(61)
        if len(joined) == 61:
            returns = joined.pct_change().dropna().to_numpy()
            m["beta_60d"] = beta(returns[:, 0], returns[:, 1])

    # Levels
    m["donchian_high_20"] = float(h[-20:].max()) if size >= 20 else NAN
    m["donchian_low_20"] = float(low_[-20:].min()) if size >= 20 else NAN
    m["prior_high_20d"] = float(h[-21:-1].max()) if size >= 21 else NAN
    m["prior_close_max_20d"] = float(c[-21:-1].max()) if size >= 21 else NAN
    year = min(size, TRADING_DAYS_PER_YEAR)
    m["dist_52w_high_pct"] = (_ratio(close, h[-year:].max()) - 1) * 100 if size >= 60 else NAN
    m["high_52w"] = float(h[-year:].max())
    m["low_52w"] = float(low_[-year:].min())
    m["extension_atr"] = _ratio(close - sma20[-1], atr_value)
    levels = support_resistance(bars, atr_value) if size >= 10 else Levels()
    m["nearest_support"], m["nearest_resistance"] = (
        levels.nearest_support,
        levels.nearest_resistance,
    )
    return m, levels


def intraday_metrics(bars: pd.DataFrame, as_of: datetime) -> dict[str, float]:
    """Price against today's VWAP, and volume so far against the same time on prior days."""
    result = {"price_vs_vwap_pct": NAN, "rvol_time_of_day": NAN}
    if bars is None or bars.empty:
        return result
    local = bars.tz_convert(clock.MARKET_TZ)
    today = clock.market_date(as_of)
    days = local.index.date
    session = local[days == today]
    if session.empty:
        return result
    typical = (session["high"] + session["low"] + session["close"]) / 3
    volume_today = float(session["volume"].sum())
    if volume_today > 0:
        vwap = float((typical * session["volume"]).sum() / volume_today)
        result["price_vs_vwap_pct"] = (float(session["close"].iloc[-1]) / vwap - 1) * 100
    cutoff = session.index[-1].time()
    earlier = local[days < today]
    if not earlier.empty:
        same_window = earlier[earlier.index.time <= cutoff]
        per_day = same_window.groupby(same_window.index.date)["volume"].sum()
        if len(per_day) >= 5:
            result["rvol_time_of_day"] = _ratio(volume_today, float(per_day.tail(20).mean()))
    return result


def event_metrics(
    ticker: str,
    as_of: datetime,
    events: list[CorporateEvent],
    news: list[NewsItem],
) -> dict[str, float]:
    """Catalysts and traps: earnings, news tone and flow, insiders, dilution."""
    today = clock.market_date(as_of)
    m = {
        "days_to_earnings": NAN,
        "days_since_earnings": NAN,
        "eps_surprise_pct": NAN,
        "news_sentiment_24h": NAN,
        "news_sentiment_72h": NAN,
        "news_count_anomaly": NAN,
        "insider_buy_value_90d": 0.0,
        "recent_offering": 0.0,
    }
    scheduled = sorted(
        e.event_date
        for e in events
        if e.event_type == "earnings"
        and e.payload.get("kind") == "scheduled"
        and e.event_date >= today
    )
    if scheduled:
        # Sessions after today up to the report day; 0 when it reports today.
        ahead = market_calendar.count_trading_days(today + timedelta(days=1), scheduled[0])
        m["days_to_earnings"] = float(ahead)
    reported = sorted(
        e.event_date
        for e in events
        if e.event_type == "earnings"
        and e.payload.get("kind") == "scheduled"
        and e.event_date < today
    )
    if reported:
        # Sessions after the report day up to today.
        m["days_since_earnings"] = float(
            market_calendar.count_trading_days(reported[-1] + timedelta(days=1), today)
        )
    surprises = [
        e for e in events if e.event_type == "earnings" and e.payload.get("kind") == "surprise"
    ]
    if surprises:
        latest = max(surprises, key=lambda e: e.event_date).payload.get("surprise_pct")
        m["eps_surprise_pct"] = float(latest) if latest is not None else NAN
    for event in events:
        age = (today - event.event_date).days
        if event.event_type == "form_4" and event.payload.get("code") == "P" and 0 <= age <= 90:
            shares, price = event.payload.get("shares") or 0, event.payload.get("price") or 0
            m["insider_buy_value_90d"] += abs(float(shares)) * float(price)
        if event.event_type == "offering" and 0 <= age <= 30:
            m["recent_offering"] = 1.0

    own = [item for item in news if item.published_at <= as_of and ticker in item.tickers]
    for hours in (24, 72):
        scores = [
            item.sentiment
            for item in own
            if item.sentiment is not None and item.published_at > as_of - timedelta(hours=hours)
        ]
        m[f"news_sentiment_{hours}h"] = float(np.mean(scores)) if scores else NAN
    last_day = sum(1 for item in own if item.published_at > as_of - timedelta(hours=24))
    baseline_start, baseline_end = as_of - timedelta(days=15), as_of - timedelta(hours=24)
    baseline = sum(1 for item in own if baseline_start < item.published_at <= baseline_end) / 14
    # +1 on both sides so a quiet ticker with one headline is not an "anomaly".
    m["news_count_anomaly"] = (last_day + 1) / (baseline + 1)
    if any(
        item.category == "offering" and item.published_at > as_of - timedelta(hours=72)
        for item in own
    ):
        m["recent_offering"] = 1.0
    return m


# --- market context and regime -----------------------------------------------------------


def _latest(series: pd.Series | None, back: int = 0) -> float:
    if series is None or len(series) <= back:
        return NAN
    return float(series.iloc[-1 - back])


def market_context(state: MarketState, closes_above_sma50: Iterable[bool] = ()) -> dict[str, float]:
    """Metrics common to every ticker: benchmark trend, volatility, breadth, rates."""
    context: dict[str, float] = {}
    spy = state.daily.get("SPY")
    if spy is not None and len(spy) >= 200:
        close = spy["close"].to_numpy(dtype=float)
        context["spy_close"] = close[-1]
        context["spy_sma_50"] = _last(sma(close, 50))
        context["spy_sma_200"] = _last(sma(close, 200))
        context["spy_above_sma50"] = float(close[-1] > context["spy_sma_50"])
        context["spy_above_sma200"] = float(close[-1] > context["spy_sma_200"])
        context["spy_ret_20d"] = pct_return(close, 20)
    else:
        for name in (
            "spy_close",
            "spy_sma_50",
            "spy_sma_200",
            "spy_above_sma50",
            "spy_above_sma200",
        ):
            context[name] = NAN
        context["spy_ret_20d"] = NAN

    vix = state.macro.get("VIXCLS")
    context["vix_level"] = _latest(vix)
    context["vix_change_5d_pct"] = (_ratio(_latest(vix), _latest(vix, 5)) - 1) * 100

    flags = list(closes_above_sma50)
    context["breadth_pct_above_sma50"] = 100 * sum(flags) / len(flags) if flags else NAN

    curve = state.macro.get("T10Y2Y")
    ten, two = state.macro.get("DGS10"), state.macro.get("DGS2")
    context["curve_10y_2y"] = _latest(curve) if curve is not None else _latest(ten) - _latest(two)
    context["ust10y_change_5d"] = _latest(ten) - _latest(ten, 5)
    context["hy_spread"] = _latest(state.macro.get("BAMLH0A0HYM2"))
    context["usd_mxn"] = _latest(state.macro.get("DEXMXUS"))
    return context


def classify_regime(context: dict[str, float], config: RegimeConfig) -> tuple[str, list[str]]:
    """The day's regime and the reasons, in Spanish.

    RIESGO_ALTO: SPY below its 200-day average, or VIX above the high-risk level,
    or VIX up more than the spike threshold in 5 days.
    ALCISTA: SPY above its 50 and 200-day averages, VIX below the bullish level and
    breadth above the bullish minimum. Anything else is NEUTRAL.
    """
    vix = context.get("vix_level", NAN)
    vix_change = context.get("vix_change_5d_pct", NAN)
    above_200 = context.get("spy_above_sma200", NAN)
    above_50 = context.get("spy_above_sma50", NAN)
    breadth = context.get("breadth_pct_above_sma50", NAN)

    risk = []
    if above_200 == 0.0:
        risk.append("SPY está debajo de su promedio de 200 días")
    if vix > config.vix_high_risk_min:
        risk.append(f"VIX en {vix:.1f}, arriba de {config.vix_high_risk_min:g}")
    if vix_change > config.vix_spike_5d_pct:
        risk.append(f"VIX subió {vix_change:.0f}% en 5 días")
    if risk:
        return "RIESGO_ALTO", risk

    missing = [
        label
        for label, value in (("SPY", above_200), ("VIX", vix), ("amplitud", breadth))
        if math.isnan(value)
    ]
    if missing:
        return "NEUTRAL", [f"datos incompletos para clasificar: falta {', '.join(missing)}"]
    if (
        above_50 == 1.0
        and above_200 == 1.0
        and vix < config.vix_bullish_max
        and breadth > config.breadth_bullish_min
    ):
        return "ALCISTA", [
            "SPY arriba de sus promedios de 50 y 200 días",
            f"VIX en {vix:.1f}, debajo de {config.vix_bullish_max:g}",
            f"{breadth:.0f}% de las acciones sobre su promedio de 50 días",
        ]
    reasons = []
    if above_50 != 1.0:
        reasons.append("SPY está debajo de su promedio de 50 días")
    if vix >= config.vix_bullish_max:
        reasons.append(f"VIX en {vix:.1f}, no menor a {config.vix_bullish_max:g}")
    if breadth <= config.breadth_bullish_min:
        reasons.append(
            f"solo {breadth:.0f}% de las acciones sobre su promedio de 50 días "
            f"(mínimo {config.breadth_bullish_min:g}%)"
        )
    return "NEUTRAL", reasons


# --- the whole universe ------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorResult:
    as_of: datetime
    # One row per ticker, one column per metric.
    metrics: pd.DataFrame
    # Metrics common to every ticker.
    context: dict[str, float]
    regime: str
    regime_reasons: list[str]
    levels: dict[str, Levels] = field(default_factory=dict)

    def long(self) -> pd.DataFrame:
        """The table ``indicators(ticker, as_of, metric, value)``."""
        table = (
            self.metrics.rename_axis("ticker")
            .reset_index()
            .melt(id_vars="ticker", var_name="metric", value_name="value")
        )
        market = pd.DataFrame(
            {
                "ticker": MARKET_ROW,
                "metric": list(self.context),
                "value": list(self.context.values()),
            }
        )
        return pd.concat([table, market], ignore_index=True).assign(as_of=self.as_of)


def compute_indicators(state: MarketState, regime_config: RegimeConfig) -> IndicatorResult:
    """All metrics for every ticker of the universe, plus market context and regime."""
    benchmark = state.daily.get("SPY")
    sector_trend: dict[str, float] = {}
    for etf in SECTOR_ETFS:
        bars = state.daily.get(etf)
        if bars is not None and len(bars) >= 50:
            close = bars["close"].to_numpy(dtype=float)
            sector_trend[etf] = float(close[-1] > _last(sma(close, 50)))

    news_by_ticker: dict[str, list[NewsItem]] = {}
    for item in state.news:
        for ticker in item.tickers:
            news_by_ticker.setdefault(ticker, []).append(item)

    rows: dict[str, dict[str, float]] = {}
    levels: dict[str, Levels] = {}
    above_sma50: list[bool] = []
    new_highs = new_lows = 0
    for ticker in state.universe.index:
        bars = state.daily.get(ticker)
        if bars is None or len(bars) < 2:
            continue
        sector_etf = state.universe.at[ticker, "sector_etf"]
        metrics, ticker_levels = daily_metrics(
            bars, benchmark=benchmark, sector=state.daily.get(sector_etf) if sector_etf else None
        )
        metrics["sector_trend"] = sector_trend.get(sector_etf, NAN) if sector_etf else NAN
        metrics.update(intraday_metrics(state.intraday.get(ticker), state.as_of))
        metrics.update(
            event_metrics(
                ticker,
                state.as_of,
                state.corporate_events.get(ticker, []),
                news_by_ticker.get(ticker, []),
            )
        )
        quality = state.quality.get(ticker)
        metrics["data_quality"] = quality.score if quality else NAN
        rows[ticker] = metrics
        levels[ticker] = ticker_levels
        if not math.isnan(metrics["sma_50"]):
            above_sma50.append(metrics["close"] > metrics["sma_50"])
        if len(bars) >= TRADING_DAYS_PER_YEAR:
            new_highs += bars["high"].iloc[-1] >= metrics["high_52w"]
            new_lows += bars["low"].iloc[-1] <= metrics["low_52w"]

    context = market_context(state, above_sma50)
    context["new_highs_52w"] = float(new_highs)
    context["new_lows_52w"] = float(new_lows)
    context["new_highs_minus_lows_52w"] = float(new_highs - new_lows)
    for etf, trend in sector_trend.items():
        context[f"sector_trend_{etf}"] = trend
    context = {name: float(value) for name, value in context.items()}
    regime, reasons = classify_regime(context, regime_config)
    return IndicatorResult(
        as_of=state.as_of,
        metrics=pd.DataFrame.from_dict(rows, orient="index"),
        context=context,
        regime=regime,
        regime_reasons=reasons,
        levels=levels,
    )


def store_indicators(
    session: Session, result: IndicatorResult, tickers: Iterable[str] | None = None
) -> int:
    """Write metrics to ``indicator_values``. ``tickers`` limits it (the market row is
    always written); values that are not finite numbers are stored as NULL."""
    table = result.long()
    if tickers is not None:
        table = table[table["ticker"].isin({*tickers, MARKET_ROW})]
    rows = [
        {
            "ticker": row.ticker,
            "as_of": result.as_of,
            "metric": row.metric,
            "value": float(row.value) if math.isfinite(row.value) else None,
        }
        for row in table.itertuples(index=False)
    ]
    _upsert(session, models.IndicatorValue, rows, ("ticker", "as_of", "metric"))
    return len(rows)
