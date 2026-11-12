"""Every indicator against known values.

Two kinds of evidence per formula: small cases worked out by hand (the arithmetic
is in the comments), and a comparison with a plain step-by-step implementation of
the textbook definition on random data, which guards the fast vectorized code.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from core import clock
from core.settings import RegimeConfig
from engine import indicators as ind
from engine.schemas import CorporateEvent, NewsItem

NAN = float("nan")

# Wilder's classic RSI example (closing prices).
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
    46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]  # fmt: skip


def approx(expected, tolerance=1e-9):
    return pytest.approx(expected, abs=tolerance, nan_ok=True)


def _bars(closes, *, highs=None, lows=None, opens=None, volumes=None, start="2025-01-02"):
    closes = np.asarray(closes, dtype=float)
    index = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes if opens is None else opens,
            "high": closes if highs is None else highs,
            "low": closes if lows is None else lows,
            "close": closes,
            "volume": np.full(len(closes), 1000.0) if volumes is None else volumes,
        },
        index=index,
    )


# --- step-by-step reference implementations -------------------------------------------


def ref_smoothed(values, n, alpha):
    out = [NAN] * len(values)
    if len(values) < n:
        return out
    current = sum(values[:n]) / n
    out[n - 1] = current
    for index in range(n, len(values)):
        current = alpha * values[index] + (1 - alpha) * current
        out[index] = current
    return out


def ref_rsi(closes, n):
    out = [NAN] * len(closes)
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain, avg_loss = ref_smoothed(gains, n, 1 / n), ref_smoothed(losses, n, 1 / n)
    for i in range(len(gains)):
        if math.isnan(avg_gain[i]):
            continue
        if avg_loss[i] == 0:
            out[i + 1] = 50.0 if avg_gain[i] == 0 else 100.0
        else:
            out[i + 1] = 100 - 100 / (1 + avg_gain[i] / avg_loss[i])
    return out


def ref_true_range(high, low, close):
    out = [high[0] - low[0]]
    for i in range(1, len(high)):
        out.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return out


def ref_adx(high, low, close, n):
    plus_dm, minus_dm = [], []
    for i in range(1, len(high)):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    ranges = ref_smoothed(ref_true_range(high, low, close)[1:], n, 1 / n)
    plus = ref_smoothed(plus_dm, n, 1 / n)
    minus = ref_smoothed(minus_dm, n, 1 / n)
    dx = []
    for i in range(len(plus)):
        if math.isnan(plus[i]):
            dx.append(NAN)
            continue
        plus_di, minus_di = 100 * plus[i] / ranges[i], 100 * minus[i] / ranges[i]
        total = plus_di + minus_di
        dx.append(0.0 if total == 0 else 100 * abs(plus_di - minus_di) / total)
    valid = [value for value in dx if not math.isnan(value)]
    smoothed = ref_smoothed(valid, n, 1 / n)
    return [NAN] * (len(high) - len(smoothed)) + smoothed


@pytest.fixture
def random_bars():
    rng = np.random.default_rng(20261110)
    close = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, 320)))
    spread = close * rng.uniform(0.005, 0.03, 320)
    high = close + spread * rng.uniform(0.2, 1.0, 320)
    low = close - spread * rng.uniform(0.2, 1.0, 320)
    volume = rng.uniform(1e5, 5e6, 320).round()
    return _bars(close, highs=high, lows=low, opens=close * (1 + rng.normal(0, 0.004, 320)),
                 volumes=volume)  # fmt: skip


# --- moving averages --------------------------------------------------------------------


def test_sma():
    values = ind.sma(range(1, 11), 3)

    assert np.isnan(values[:2]).all()
    assert values[2] == approx(2.0)  # (1 + 2 + 3) / 3
    assert values[-1] == approx(9.0)  # (8 + 9 + 10) / 3


def test_ema_is_seeded_with_the_simple_average():
    # n = 3 -> alpha = 0.5. Seed = mean(2, 4, 6) = 4; then 0.5*8 + 0.5*4 = 6; 0.5*10 + 0.5*6 = 8.
    values = ind.ema([2, 4, 6, 8, 10], 3)

    assert values == approx([NAN, NAN, 4.0, 6.0, 8.0])


def test_wilder_smoothing():
    # n = 3 -> alpha = 1/3. Seed = mean(3, 6, 9) = 6; then 6 + (12 - 6)/3 = 8.
    assert ind.wilder([3, 6, 9, 12], 3) == approx([NAN, NAN, 6.0, 8.0])


def test_smoothing_skips_leading_gaps_and_needs_enough_history():
    assert ind.ema([NAN, NAN, 2, 4, 6, 8], 3) == approx([NAN, NAN, NAN, NAN, 4.0, 6.0])
    assert np.isnan(ind.ema([1, 2], 3)).all()


@pytest.mark.parametrize("n", [2, 9, 14, 26])
def test_ema_and_wilder_match_the_step_by_step_definition(random_bars, n):
    close = random_bars["close"].tolist()

    assert ind.ema(close, n) == approx(ref_smoothed(close, n, 2 / (n + 1)), 1e-9)
    assert ind.wilder(close, n) == approx(ref_smoothed(close, n, 1 / n), 1e-9)


# --- RSI --------------------------------------------------------------------------------


def test_rsi_classic_wilder_example():
    # First 14 changes: gains sum 3.34 (avg 0.238571), losses sum 1.40 (avg 0.10).
    # RS = 2.38571 -> RSI = 100 - 100/3.38571 = 70.46. Then 66.25 and 66.48.
    values = ind.rsi(WILDER_CLOSES, 14)

    assert np.isnan(values[:14]).all()
    assert values[14] == approx(70.46, 0.01)
    assert values[15] == approx(66.25, 0.01)
    assert values[16] == approx(66.48, 0.01)


def test_rsi_two_period_by_hand():
    # Changes +1, -1, +2. Seed: avg gain 0.5, avg loss 0.5 -> RSI 50.
    # Next: avg gain (0.5 + 2)/2 = 1.25, avg loss 0.5/2 = 0.25 -> RS 5 -> 83.3333.
    assert ind.rsi([10, 11, 10, 12], 2) == approx([NAN, NAN, 50.0, 83.3333333], 1e-6)


def test_rsi_extremes():
    assert ind.rsi(range(1, 30), 14)[-1] == approx(100.0)  # only gains
    assert ind.rsi(range(30, 1, -1), 14)[-1] == approx(0.0)  # only losses
    assert ind.rsi([5.0] * 30, 14)[-1] == approx(50.0)  # no movement


@pytest.mark.parametrize("n", [2, 14])
def test_rsi_matches_the_step_by_step_definition(random_bars, n):
    close = random_bars["close"].tolist()

    assert ind.rsi(close, n) == approx(ref_rsi(close, n), 1e-8)


# --- MACD -------------------------------------------------------------------------------


def test_macd_of_a_constant_price_is_zero():
    line, signal, histogram = ind.macd([42.0] * 60)

    assert line[-1] == approx(0.0)
    assert signal[-1] == approx(0.0)
    assert histogram[-1] == approx(0.0)
    assert np.isnan(line[:25]).all() and not np.isnan(line[25])
    assert np.isnan(signal[:33]).all() and not np.isnan(signal[33])  # 26 + 9 - 2


def test_macd_of_a_straight_line():
    # An EMA of a line rising b per bar trails it by b*(n-1)/2, so the MACD line is
    # b*(25/2 - 11/2) = 7b. With b = 0.5 that is 3.5, and the histogram is 0.
    line, signal, histogram = ind.macd(100 + 0.5 * np.arange(80))

    assert line[-1] == approx(3.5, 1e-9)
    assert signal[-1] == approx(3.5, 1e-9)
    assert histogram[-1] == approx(0.0, 1e-9)


def test_macd_matches_the_step_by_step_definition(random_bars):
    close = random_bars["close"].tolist()
    fast, slow = ref_smoothed(close, 12, 2 / 13), ref_smoothed(close, 26, 2 / 27)
    expected_line = [f - s for f, s in zip(fast, slow, strict=True)]
    valid = [value for value in expected_line if not math.isnan(value)]
    expected_signal = [NAN] * 25 + ref_smoothed(valid, 9, 0.2)

    line, signal, histogram = ind.macd(close)

    assert line == approx(expected_line, 1e-9)
    assert signal == approx(expected_signal, 1e-9)
    assert histogram[-1] == approx(expected_line[-1] - expected_signal[-1], 1e-9)


# --- true range, ATR, ADX ---------------------------------------------------------------

HIGH, LOW, CLOSE = [10, 11, 12, 11], [8, 9, 9, 10], [9, 10, 11, 10.5]


def test_true_range_by_hand():
    # Bar 0: 10-8 = 2. Bar 1: max(2, |11-9|, |9-9|) = 2. Bar 2: max(3, |12-10|, |9-10|) = 3.
    # Bar 3: max(1, |11-11|, |10-11|) = 1.
    assert ind.true_range(HIGH, LOW, CLOSE) == approx([2.0, 2.0, 3.0, 1.0])


def test_true_range_includes_gaps():
    # Previous close 10, then a bar from 12 to 13: the gap counts, TR = 13 - 10 = 3.
    assert ind.true_range([10.5, 13], [9.5, 12], [10, 12.5])[1] == approx(3.0)


def test_atr_by_hand():
    # ATR(3): seed = (2 + 2 + 3)/3 = 2.3333; next = 2.3333 + (1 - 2.3333)/3 = 1.8889.
    assert ind.atr(HIGH, LOW, CLOSE, 3) == approx([NAN, NAN, 7 / 3, 17 / 9])


def test_atr_matches_the_step_by_step_definition(random_bars):
    high, low, close = (random_bars[c].tolist() for c in ("high", "low", "close"))

    assert ind.atr(high, low, close, 14) == approx(
        ref_smoothed(ref_true_range(high, low, close), 14, 1 / 14), 1e-9
    )


def test_adx_of_a_steady_uptrend():
    # Every bar is 1 higher: +DM = 1, -DM = 0, TR = 1.5 -> +DI = 66.67, -DI = 0, DX = ADX = 100.
    steps = np.arange(60, dtype=float)
    adx, plus_di, minus_di = ind.adx(10 + steps, 9 + steps, 9.5 + steps, 14)

    assert plus_di[-1] == approx(200 / 3, 1e-9)
    assert minus_di[-1] == approx(0.0)
    assert adx[-1] == approx(100.0, 1e-9)
    assert np.isnan(adx[:27]).all() and not np.isnan(adx[27])  # needs 2n bars


def test_adx_of_a_steady_downtrend_and_of_no_trend():
    steps = np.arange(60, dtype=float)
    adx, plus_di, minus_di = ind.adx(100 - steps, 99 - steps, 99.5 - steps, 14)
    assert (plus_di[-1], minus_di[-1], adx[-1]) == approx((0.0, 200 / 3, 100.0), 1e-9)

    flat = np.full(60, 10.0)
    adx, plus_di, minus_di = ind.adx(flat + 0.5, flat - 0.5, flat, 14)
    assert (plus_di[-1], minus_di[-1], adx[-1]) == approx((0.0, 0.0, 0.0))


def test_adx_matches_the_step_by_step_definition(random_bars):
    high, low, close = (random_bars[c].tolist() for c in ("high", "low", "close"))

    adx, _, _ = ind.adx(high, low, close, 14)

    assert adx == approx(ref_adx(high, low, close, 14), 1e-8)


# --- volume -----------------------------------------------------------------------------


def test_obv_by_hand():
    # Up +200, flat 0, down -400, up +500.
    assert ind.obv([10, 11, 11, 10, 12], [100, 200, 300, 400, 500]) == approx(
        [0.0, 200.0, 200.0, -200.0, 300.0]
    )


def test_mfi_by_hand():
    # Typical prices 10, 11, 10, 12 with volume 100: flows +1100, -1000, +1200.
    # Window of 2 at bar 2: 100 - 100/(1 + 1100/1000) = 52.381. At bar 3: 54.5455.
    prices = [10, 11, 10, 12]
    values = ind.mfi(prices, prices, prices, [100] * 4, 2)

    assert values == approx([NAN, NAN, 52.3809524, 54.5454545], 1e-6)


def test_mfi_extremes():
    rising, falling = list(range(1, 30)), list(range(30, 1, -1))
    volume = [100] * 29

    assert ind.mfi(rising, rising, rising, volume, 14)[-1] == approx(100.0)
    assert ind.mfi(falling, falling, falling, volume, 14)[-1] == approx(0.0)


def test_cmf_by_hand():
    # Bar 1 closes at its high: multiplier +1, x100 = +100. Bar 2 (8 to 10, close 8.5):
    # ((0.5) - (1.5))/2 = -0.5, x300 = -150. CMF(2) = (100 - 150)/400 = -0.125.
    values = ind.cmf([10, 10], [8, 8], [10, 8.5], [100, 300], 2)

    assert values[-1] == approx(-0.125)


def test_cmf_extremes():
    high, low = [10.0] * 25, [8.0] * 25
    volume = [500.0] * 25

    assert ind.cmf(high, low, high, volume, 20)[-1] == approx(1.0)  # closes at the high
    assert ind.cmf(high, low, low, volume, 20)[-1] == approx(-1.0)  # closes at the low
    assert ind.cmf(high, low, [9.0] * 25, volume, 20)[-1] == approx(0.0)  # closes mid-range
    assert ind.cmf(high, high, high, volume, 20)[-1] == approx(0.0)  # no range at all


# --- volatility -------------------------------------------------------------------------


def test_bollinger_by_hand():
    # 2, 4, 4, 6: mean 4, population deviation sqrt(8/4) = 1.41421.
    middle, upper, lower = ind.bollinger([2, 4, 4, 6], 4, 2.0)

    assert middle[-1] == approx(4.0)
    assert upper[-1] == approx(4 + 2 * math.sqrt(2))
    assert lower[-1] == approx(4 - 2 * math.sqrt(2))


def test_keltner_channel_is_ema_plus_and_minus_atr():
    flat = np.full(40, 20.0)
    middle, upper, lower = ind.keltner(flat + 1, flat - 1, flat, 20, 1.5)

    # Constant bars with range 2: EMA 20, ATR 2, so the channel is 20 +/- 3.
    assert (middle[-1], upper[-1], lower[-1]) == approx((20.0, 23.0, 17.0))


def test_realized_volatility():
    assert ind.realized_volatility(100 * 1.01 ** np.arange(30), 10)[-1] == approx(0.0, 1e-9)

    # Alternating +1% and -1% in log terms: deviation x*sqrt(n/(n-1)), annualized.
    x = math.log(1.01)
    prices = 100 * np.exp(np.cumsum([x, -x] * 6))
    expected = x * math.sqrt(10 / 9) * math.sqrt(252) * 100

    assert ind.realized_volatility(np.concatenate(([100.0], prices)), 10)[-1] == approx(
        expected, 1e-9
    )


def test_beta():
    rng = np.random.default_rng(7)
    market = rng.normal(0, 0.01, 60)

    assert ind.beta(2 * market, market) == approx(2.0)
    assert ind.beta(market, market) == approx(1.0)
    assert ind.beta(-0.5 * market, market) == approx(-0.5)
    assert math.isnan(ind.beta(market, np.zeros(60)))  # a benchmark that does not move


def test_pct_return_and_slope():
    assert ind.pct_return([100, 104, 110], 2) == approx(10.0)
    assert ind.pct_return([100, 104, 110], 1) == approx(110 / 104 * 100 - 100)
    assert math.isnan(ind.pct_return([100, 110], 5))
    assert ind.linear_slope([1, 3, 5, 7]) == approx(2.0)
    assert ind.linear_slope([5, 5, 5]) == approx(0.0)


# --- levels -----------------------------------------------------------------------------


def test_pivot_points():
    highs = [1, 2, 3, 5, 3, 2, 1, 2, 4, 2, 1]
    lows = [9, 8, 7, 5, 7, 8, 9, 8, 6, 8, 9]

    pivot_highs, pivot_lows = ind.pivot_points(highs, lows, span=2)

    assert pivot_highs == [3, 8]  # the 5 and the 4
    assert pivot_lows == [3, 8]  # the 5 and the 6


def test_the_latest_bars_cannot_be_pivots_yet():
    """Confirming a swing high needs the bars after it; those have not happened."""
    highs = [1, 2, 3, 2, 1, 2, 9]

    pivot_highs, _ = ind.pivot_points(highs, highs, span=2)

    assert pivot_highs == [2]  # the 9 at the end is not confirmed


def test_support_and_resistance_around_the_last_close():
    #        idx: 0   1   2   3   4   5   6   7   8   9  10  11  12
    highs = [10, 11, 14, 11, 10, 11, 17, 12, 11, 12, 12, 12, 12]
    lows = [9, 10, 12, 9, 6, 9, 15, 10, 8, 10, 11, 10.5, 11]
    closes = [9.5, 10.5, 13, 10, 7, 10, 16, 11, 9, 11, 12, 11, 11.5]

    levels = ind.support_resistance(_bars(closes, highs=highs, lows=lows), atr_value=0.0, span=2)

    assert levels.resistances == (14.0, 17.0)  # swing highs above 11.5, nearest first
    assert levels.supports == (8.0, 6.0)  # swing lows below 11.5, nearest first
    assert (levels.nearest_support, levels.nearest_resistance) == (8.0, 14.0)


def test_levels_closer_than_half_an_atr_count_as_one():
    highs = [10, 11, 14, 11, 10, 11, 14.3, 12, 11, 12, 12, 12, 12]
    closes = [9.5, 10.5, 13, 10, 9, 10, 13, 11, 10, 11, 12, 11, 11.5]

    wide = ind.support_resistance(_bars(closes, highs=highs), atr_value=1.0, span=2)
    narrow = ind.support_resistance(_bars(closes, highs=highs), atr_value=0.2, span=2)

    assert wide.resistances == (14.0,)  # 14.0 and 14.3 are within 0.5
    assert narrow.resistances == (14.0, 14.3)


def test_no_levels_means_nan():
    levels = ind.Levels()

    assert math.isnan(levels.nearest_support) and math.isnan(levels.nearest_resistance)


# --- metrics of one ticker ----------------------------------------------------------------

EXPECTED_DAILY_METRICS = {
    # trend
    "close", "sma_20", "sma_50", "sma_200", "ema_9", "ema_21", "sma50_slope_20d", "adx_14",
    "plus_di_14", "minus_di_14", "adx_change_5d",
    # momentum
    "rsi_14", "rsi_2", "macd_line", "macd_signal", "macd_hist", "ret_1d", "ret_5d", "ret_20d",
    "ret_60d", "down_days_streak",
    # relative strength
    "rs_20d_vs_spy", "rs_60d_vs_spy", "rs_20d_vs_sector",
    # volume
    "rvol_20d", "dollar_volume_20d", "obv_slope_20d", "mfi_14", "cmf_20",
    "up_down_volume_ratio_20d",
    # volatility
    "atr_14", "atr_pct", "realized_vol_10d", "realized_vol_20d", "bb_percent_b", "bb_width",
    "squeeze", "squeeze_recent", "range_vs_atr", "gap_pct", "gap_atr", "beta_60d",
    # levels
    "donchian_high_20", "donchian_low_20", "prior_high_20d", "dist_52w_high_pct", "high_52w",
    "low_52w", "extension_atr", "nearest_support", "nearest_resistance",
}  # fmt: skip


def test_daily_metrics_cover_every_group(random_bars):
    benchmark = _bars(100 * 1.0005 ** np.arange(320))

    metrics, levels = ind.daily_metrics(random_bars, benchmark=benchmark, sector=benchmark)

    assert set(metrics) == EXPECTED_DAILY_METRICS
    missing = [name for name, value in metrics.items() if math.isnan(value)]
    assert missing in ([], ["nearest_support"], ["nearest_resistance"])
    assert isinstance(levels, ind.Levels)


def test_daily_metrics_on_a_series_with_known_answers():
    # 300 sessions rising exactly 1 per day from 100, range of 2, constant volume,
    # then a last day with double volume that opens with a gap.
    steps = np.arange(300, dtype=float)
    close = 100 + steps
    volume = np.full(300, 1_000.0)
    volume[-1] = 2_000.0
    opens = close - 0.5
    opens[-1] = close[-2] + 2  # opens 2 above the previous close
    bars = _bars(close, highs=close + 1, lows=close - 1, opens=opens, volumes=volume)
    benchmark = _bars(200 + 0.5 * steps)  # rises more slowly

    m, _ = ind.daily_metrics(bars, benchmark=benchmark, sector=None)

    assert m["close"] == approx(399.0)
    assert m["sma_20"] == approx(399 - 9.5)  # the mean of a line trails by (n-1)/2
    assert m["sma_50"] == approx(399 - 24.5)
    assert m["sma_200"] == approx(399 - 99.5)
    assert m["ema_9"] == approx(399 - 4.0, 1e-9)
    assert m["ema_21"] == approx(399 - 10.0, 1e-9)
    assert m["sma50_slope_20d"] == approx((374.5 / 354.5 - 1) * 100)
    assert m["rsi_14"] == approx(100.0)
    assert m["adx_14"] == approx(100.0, 1e-6)
    assert m["macd_line"] == approx(7.0, 1e-9)  # 7 * slope
    assert m["macd_hist"] == approx(0.0, 1e-9)
    assert m["ret_1d"] == approx((399 / 398 - 1) * 100)
    assert m["ret_20d"] == approx((399 / 379 - 1) * 100)
    assert m["down_days_streak"] == 0.0
    assert m["rs_20d_vs_spy"] == approx((399 / 379 - 1) * 100 - (349.5 / 339.5 - 1) * 100)
    assert math.isnan(m["rs_20d_vs_sector"])  # no sector ETF given
    assert m["rvol_20d"] == approx(2.0)  # 2000 against an average of 1000
    assert m["dollar_volume_20d"] == approx(float(np.mean(close[-20:] * volume[-20:])))
    assert m["cmf_20"] == approx(0.0)  # every close is mid-range
    assert m["atr_14"] == approx(2.0, 1e-9)  # high-low = 2 and no bigger gap
    assert m["atr_pct"] == approx(2 / 399 * 100, 1e-9)
    assert m["range_vs_atr"] == approx(1.0, 1e-9)
    assert m["gap_pct"] == approx(2 / 398 * 100)
    assert m["gap_atr"] == approx(1.0, 1e-9)  # a gap of 2 with an ATR of 2
    assert m["donchian_high_20"] == approx(400.0)
    assert m["donchian_low_20"] == approx(379.0)
    assert m["prior_high_20d"] == approx(399.0)
    assert m["dist_52w_high_pct"] == approx((399 / 400 - 1) * 100)
    assert m["extension_atr"] == approx(9.5 / 2, 1e-9)  # 9.5 above the SMA20, ATR 2
    assert math.isinf(m["up_down_volume_ratio_20d"]) or math.isnan(m["up_down_volume_ratio_20d"])


def test_down_days_streak_and_up_down_volume():
    close = [10, 11, 12, 13, 12, 11, 10] + [10 + i for i in range(1, 16)] + [24, 23, 22]
    volume = [100.0] * len(close)
    volume[-1] = 400.0

    m, _ = ind.daily_metrics(_bars(close, volumes=volume))

    assert m["down_days_streak"] == 3.0
    # Last 20 changes: 14 up days, 5 down days (two of the early ones, three at the end)
    # and one flat... counted from volume: up 15*100, down (2 + 2)*100 + 400.
    change = np.diff(close[-21:])
    up = sum(v for v, c in zip(volume[-20:], change, strict=True) if c > 0)
    down = sum(v for v, c in zip(volume[-20:], change, strict=True) if c < 0)
    assert m["up_down_volume_ratio_20d"] == approx(up / down)


def test_short_history_gives_nan_instead_of_wrong_numbers():
    m, levels = ind.daily_metrics(_bars([10, 11, 12, 11, 12]))

    for name in ("sma_20", "sma_200", "rsi_14", "adx_14", "atr_14", "rvol_20d", "beta_60d",
                 "donchian_high_20", "dist_52w_high_pct", "macd_line"):  # fmt: skip
        assert math.isnan(m[name]), name
    assert m["close"] == 12.0
    assert m["ret_1d"] == approx((12 / 11 - 1) * 100)
    assert levels == ind.Levels()


def test_squeeze_is_bollinger_inside_keltner():
    # A long, perfectly quiet stretch: the bands collapse inside the channel.
    quiet = np.full(80, 50.0)
    m, _ = ind.daily_metrics(_bars(quiet, highs=quiet + 0.5, lows=quiet - 0.5))
    assert m["squeeze"] == 1.0 and m["squeeze_recent"] == 1.0

    # Violent closes with small daily ranges: the bands are far outside the channel.
    wild = 50 + 10 * np.sin(np.arange(80))
    m, _ = ind.daily_metrics(_bars(wild, highs=wild + 0.1, lows=wild - 0.1))
    assert m["squeeze"] == 0.0


def test_beta_against_the_benchmark():
    rng = np.random.default_rng(3)
    market_returns = rng.normal(0, 0.01, 120)
    market = 100 * np.cumprod(1 + market_returns)
    stock = 50 * np.cumprod(1 + 1.5 * market_returns)

    m, _ = ind.daily_metrics(_bars(stock), benchmark=_bars(market))

    assert m["beta_60d"] == approx(1.5, 1e-9)


# --- intraday -----------------------------------------------------------------------------


def _intraday(day: date, rows: list[tuple[time, float, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([clock.market_time(day, at) for at, _, _ in rows])
    prices = [price for _, price, _ in rows]
    return pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices,
         "volume": [volume for _, _, volume in rows]},
        index=index,
    )  # fmt: skip


def test_price_against_vwap_by_hand():
    day = date(2026, 11, 10)
    bars = _intraday(day, [(time(9, 30), 100.0, 300.0), (time(9, 35), 104.0, 100.0)])

    m = ind.intraday_metrics(bars, clock.market_time(day, time(9, 40)))

    # VWAP = (100*300 + 104*100)/400 = 101. Last price 104 -> +2.9703%.
    assert m["price_vs_vwap_pct"] == approx((104 / 101 - 1) * 100)
    assert math.isnan(m["rvol_time_of_day"])  # no earlier days to compare with


def test_relative_volume_at_the_same_time_of_day():
    today = date(2026, 11, 10)
    frames = []
    for offset in range(1, 7):  # six earlier sessions
        day = pd.bdate_range(end=today - timedelta(days=1), periods=6)[-offset].date()
        frames.append(_intraday(day, [(time(9, 30), 50.0, 1000.0), (time(9, 35), 50.0, 1000.0),
                                      (time(15, 55), 50.0, 9000.0)]))  # fmt: skip
    frames.append(_intraday(today, [(time(9, 30), 50.0, 3000.0), (time(9, 35), 50.0, 3000.0)]))
    bars = pd.concat(frames).sort_index()

    m = ind.intraday_metrics(bars, clock.market_time(today, time(9, 40)))

    # By 09:35 earlier days had traded 2000; today 6000. The late-day 9000 does not count.
    assert m["rvol_time_of_day"] == approx(3.0)


def test_intraday_metrics_without_bars():
    m = ind.intraday_metrics(None, datetime(2026, 11, 10, 15, 0, tzinfo=UTC))

    assert math.isnan(m["price_vs_vwap_pct"]) and math.isnan(m["rvol_time_of_day"])


# --- events -------------------------------------------------------------------------------

AS_OF = clock.market_time(date(2026, 11, 10), time(8, 45))  # a Tuesday, before the open


def _event(event_type: str, day: date, **payload) -> CorporateEvent:
    return CorporateEvent(
        ticker="XYZ", event_type=event_type, event_date=day, payload=payload, source="t"
    )


def _news(hours_ago: float, sentiment: float | None, *, ticker="XYZ", category=None) -> NewsItem:
    return NewsItem(
        tickers=(ticker,),
        published_at=AS_OF - timedelta(hours=hours_ago),
        source="t",
        headline=f"headline {hours_ago}",
        sentiment=sentiment,
        category=category,
    )


def test_days_to_earnings_counts_trading_days():
    def days(day: date) -> float:
        events = [_event("earnings", day, kind="scheduled")]
        return ind.event_metrics("XYZ", AS_OF, events, [])["days_to_earnings"]

    assert days(date(2026, 11, 10)) == 0.0  # reports today
    assert days(date(2026, 11, 11)) == 1.0
    assert days(date(2026, 11, 13)) == 3.0  # Friday
    assert days(date(2026, 11, 16)) == 4.0  # Monday: the weekend does not count
    assert days(date(2026, 11, 27)) == 12.0  # Thanksgiving in between does not count
    assert math.isnan(ind.event_metrics("XYZ", AS_OF, [], [])["days_to_earnings"])


def test_past_earnings_dates_are_ignored_and_the_nearest_wins():
    events = [
        _event("earnings", date(2026, 8, 4), kind="scheduled"),
        _event("earnings", date(2027, 2, 2), kind="scheduled"),
        _event("earnings", date(2026, 11, 12), kind="scheduled"),
    ]

    assert ind.event_metrics("XYZ", AS_OF, events, [])["days_to_earnings"] == 2.0


def test_latest_eps_surprise():
    events = [
        _event("earnings", date(2026, 6, 30), kind="surprise", surprise_pct=-2.5),
        _event("earnings", date(2026, 9, 30), kind="surprise", surprise_pct=3.6372),
    ]

    assert ind.event_metrics("XYZ", AS_OF, events, [])["eps_surprise_pct"] == approx(3.6372)


def test_news_sentiment_by_window():
    news = [
        _news(2, 0.8),
        _news(20, -0.2),
        _news(30, -1.0),  # outside 24 h, inside 72 h
        _news(100, 1.0),  # outside both
        _news(1, 1.0, ticker="OTHER"),  # another company
        _news(3, None),  # not scored
    ]

    m = ind.event_metrics("XYZ", AS_OF, [], news)

    assert m["news_sentiment_24h"] == approx((0.8 - 0.2) / 2)
    assert m["news_sentiment_72h"] == approx((0.8 - 0.2 - 1.0) / 3)


def test_news_from_the_future_of_the_clock_is_never_counted():
    later = _news(-5, -1.0)  # published 5 hours after the clock time

    m = ind.event_metrics("XYZ", AS_OF, [], [later])

    assert math.isnan(m["news_sentiment_24h"])
    assert m["news_count_anomaly"] == approx(1.0)


def test_news_count_anomaly():
    # 14 quiet days with one headline each, then 9 headlines in the last day.
    quiet = [_news(24 * day + 12, 0.0) for day in range(1, 15)]
    burst = [_news(hour, 0.0) for hour in range(1, 10)]

    busy = ind.event_metrics("XYZ", AS_OF, [], quiet + burst)["news_count_anomaly"]
    normal = ind.event_metrics("XYZ", AS_OF, [], [*quiet, _news(3, 0.0)])["news_count_anomaly"]

    assert busy == approx((9 + 1) / (1 + 1))  # 5 times the usual flow
    assert normal == approx(1.0)
    assert ind.event_metrics("XYZ", AS_OF, [], [])["news_count_anomaly"] == approx(1.0)


def test_insider_purchases_in_the_last_90_days():
    events = [
        _event("form_4", date(2026, 11, 3), code="P", shares=1000, price=50.0),  # bought 50,000
        _event("form_4", date(2026, 10, 1), code="P", shares=200, price=45.0),  # bought 9,000
        _event("form_4", date(2026, 11, 5), code="S", shares=-5000, price=52.0),  # a sale
        _event("form_4", date(2026, 7, 1), code="P", shares=900, price=40.0),  # too old
    ]

    assert ind.event_metrics("XYZ", AS_OF, events, [])["insider_buy_value_90d"] == approx(59_000.0)


def test_recent_offering_from_a_filing_or_from_news():
    base = ind.event_metrics("XYZ", AS_OF, [], [])
    filed = ind.event_metrics("XYZ", AS_OF, [_event("offering", date(2026, 10, 20))], [])
    old = ind.event_metrics("XYZ", AS_OF, [_event("offering", date(2026, 9, 1))], [])
    in_news = ind.event_metrics("XYZ", AS_OF, [], [_news(10, -0.5, category="offering")])

    assert (base["recent_offering"], old["recent_offering"]) == (0.0, 0.0)
    assert (filed["recent_offering"], in_news["recent_offering"]) == (1.0, 1.0)


# --- regime -------------------------------------------------------------------------------

REGIME = RegimeConfig(
    vix_bullish_max=20, vix_high_risk_min=28, vix_spike_5d_pct=25, breadth_bullish_min=55
)


def _context(**changes) -> dict[str, float]:
    bullish = {
        "spy_above_sma50": 1.0,
        "spy_above_sma200": 1.0,
        "vix_level": 15.0,
        "vix_change_5d_pct": 2.0,
        "breadth_pct_above_sma50": 65.0,
    }
    return {**bullish, **changes}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "ALCISTA"),
        ({"vix_level": 19.9, "breadth_pct_above_sma50": 55.1}, "ALCISTA"),
        ({"vix_level": 20.0}, "NEUTRAL"),  # VIX must be below 20
        ({"breadth_pct_above_sma50": 55.0}, "NEUTRAL"),  # breadth must be above 55
        ({"spy_above_sma50": 0.0}, "NEUTRAL"),
        ({"vix_level": 28.0}, "NEUTRAL"),  # high risk starts above 28
        ({"vix_level": 28.1}, "RIESGO_ALTO"),
        ({"spy_above_sma200": 0.0}, "RIESGO_ALTO"),
        ({"vix_change_5d_pct": 25.1}, "RIESGO_ALTO"),
        ({"vix_change_5d_pct": 25.0}, "ALCISTA"),  # must rise more than 25%
        (
            {"spy_above_sma200": 0.0, "vix_level": 12.0, "breadth_pct_above_sma50": 90.0},
            "RIESGO_ALTO",
        ),
    ],
)
def test_regime(changes, expected):
    regime, reasons = ind.classify_regime(_context(**changes), REGIME)

    assert regime == expected
    assert reasons  # the plan always says why


def test_regime_reasons_are_specific():
    _, risk = ind.classify_regime(_context(vix_level=31.4, vix_change_5d_pct=40.0), REGIME)
    assert risk == ["VIX en 31.4, arriba de 28", "VIX subió 40% en 5 días"]

    _, neutral = ind.classify_regime(_context(breadth_pct_above_sma50=41.0), REGIME)
    assert neutral == ["solo 41% de las acciones sobre su promedio de 50 días (mínimo 55%)"]


def test_regime_with_missing_data_is_neutral_and_says_what_is_missing():
    regime, reasons = ind.classify_regime(_context(vix_level=NAN), REGIME)

    assert regime == "NEUTRAL"
    assert reasons == ["datos incompletos para clasificar: falta VIX"]


def test_high_risk_evidence_wins_over_missing_data():
    regime, _ = ind.classify_regime(_context(spy_above_sma200=0.0, vix_level=NAN), REGIME)

    assert regime == "RIESGO_ALTO"
