"""The 22 controls: for each one, a case that passes and a case that fails."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time, timedelta

import pytest

from core import clock, settings
from engine import controls as ctl
from engine.data_engine import Quality
from engine.portfolio import Holding, Portfolio
from engine.risk_profile import profile_for
from engine.schemas import EconEvent, NewsItem, Quote
from engine.screener import Candidate
from engine.trade_plan import TradePlan
from tests.conftest import SHIPPED_CONFIG

DAY = date(2026, 11, 10)  # Tuesday
PREMARKET = clock.market_time(DAY, time(9, 0))
IN_SESSION = clock.market_time(DAY, time(11, 0))
CONFIG = settings.load_config(SHIPPED_CONFIG)

GOOD_METRICS = {
    "close": 48.25,
    "atr_14": 1.0,
    "dollar_volume_20d": 150e6,
    "days_to_earnings": 12.0,
    "extension_atr": 1.2,
}
# Entry around 48.25, stop 1.55 below under a support, first target 1.8R away.
GOOD_PLAN = TradePlan(
    entry_min=48.0,
    entry_max=48.5,
    no_chase_above=49.0,
    reference=48.25,
    stop=46.70,
    stop_level=46.80,
    stop_level_name="soporte",
    targets=(51.05, 52.90),
    atr=1.0,
)


def _holding(ticker: str, sector: str = "XLF", value: float = 500.0) -> Holding:
    return Holding(ticker, sector, shares=10, entry_price=value / 10, last_price=value / 10,
                   stop=value / 10 * 0.95, opened_on=date(2026, 11, 5))  # fmt: skip


def _news(category: str, hours_ago: float, headline: str = "headline") -> NewsItem:
    return NewsItem(tickers=("XYZ",), published_at=PREMARKET - timedelta(hours=hours_ago), source="t",
                    headline=headline, category=category,
                    severe=category in ("investigation", "bankruptcy", "offering", "downgrade"))  # fmt: skip


def _config(section: str, **changes):
    return CONFIG.model_copy(update={section: getattr(CONFIG, section).model_copy(update=changes)})


def context(**changes) -> ctl.ControlContext:
    """A candidate that passes every control, with ``changes`` applied on top."""
    metrics = {**GOOD_METRICS, **changes.pop("metrics", {})}
    score = changes.pop("score", 82.0)
    base = ctl.ControlContext(
        as_of=PREMARKET,
        config=CONFIG,
        profile=profile_for(5),
        regime="ALCISTA",
        candidate=Candidate("XYZ", "RUPTURA", score, {}, [], metrics),
        plan=GOOD_PLAN,
        portfolio=Portfolio(cash=5000.0),
        quality=Quality(1.0),
        sector_etf="XLK",
        market_cap_usd=5e9,
        sic_listed=True,
    )
    return replace(base, **changes)


def result_of(control_id: str, ctx: ctl.ControlContext) -> ctl.ControlResult:
    return next(r for r in ctl.run_controls(ctx) if r.id == control_id)


# --- the set of controls -------------------------------------------------------------------


def test_there_are_22_controls_with_the_types_of_the_roadmap():
    ids = [spec.id for spec in ctl.CONTROLS]
    reducing = {spec.id for spec in ctl.CONTROLS if spec.kind == "R"}

    assert ids == [f"C{n:02d}" for n in range(1, 23)]
    assert reducing == {"C09", "C10", "C17"}
    assert len({spec.name for spec in ctl.CONTROLS}) == 22


def test_a_clean_candidate_passes_all_22():
    results = ctl.run_controls(context())

    assert [r.result for r in results] == ["pass"] * 22
    assert not ctl.is_blocked(results)
    assert ctl.size_multiplier(results) == 1.0
    assert all(r.reason for r in results)  # every control explains itself


# --- one failing case (and its passing neighbour) per control ---------------------------------

OLD_QUOTE = Quote(
    ticker="XYZ", ts=IN_SESSION - timedelta(minutes=5), bid=48.2, ask=48.3, source="t"
)
FRESH_QUOTE = Quote(
    ticker="XYZ", ts=IN_SESSION - timedelta(seconds=20), bid=48.24, ask=48.26, source="t"
)
WIDE_QUOTE = Quote(ticker="XYZ", ts=PREMARKET, bid=48.0, ask=48.5, source="t")

FOMC_IN_5H = EconEvent(name="FOMC", country="US", impact="high", source="t",
                       scheduled_at=PREMARKET + timedelta(hours=5))  # fmt: skip
FOMC_IN_7H = EconEvent(name="FOMC", country="US", impact="high", source="t",
                       scheduled_at=PREMARKET + timedelta(hours=7))  # fmt: skip
MINOR_EVENT = EconEvent(name="Inventarios", country="US", impact="low", source="t",
                        scheduled_at=PREMARKET + timedelta(hours=1))  # fmt: skip

FULL = Portfolio(cash=5000.0, holdings=tuple(_holding(f"H{i}", f"S{i}") for i in range(5)))
TWO_IN_SECTOR = Portfolio(cash=5000.0, holdings=(_holding("A", "XLK"), _holding("B", "XLK")))
HEAVY_SECTOR = Portfolio(cash=3000.0, holdings=(_holding("A", "XLK", value=2500.0),))

FAILS = [
    ("C01", {"quality": Quality(0.7, ("falta la barra del 2026-11-09",))}, "calidad de datos 0.70"),
    ("C01", {"quality": None}, "sin datos"),
    ("C01", {"as_of": IN_SESSION, "quote": OLD_QUOTE}, "el precio tiene 300 s"),
    (
        "C02",
        {"as_of": clock.market_time(date(2026, 11, 14), time(9, 0))},
        "NYSE no abre el 2026-11-14",
    ),
    ("C02", {"as_of": clock.market_time(date(2026, 11, 26), time(9, 0))}, "NYSE no abre"),
    ("C03", {"metrics": {"close": 4.99}}, "precio $4.99, menor al mínimo de $5.00"),
    ("C04", {"metrics": {"dollar_volume_20d": 19.9e6}}, "el mínimo es $20 millones"),
    ("C05", {"market_cap_usd": 0.9e9}, "capitalización $900 millones"),
    ("C05", {"market_cap_usd": None}, "capitalización desconocida"),
    ("C06", {"quote": WIDE_QUOTE}, "spread 1.04%"),
    ("C06", {"metrics": {"dollar_volume_20d": 5e6}}, "spread estimado"),
    (
        "C07",
        {"config": _config("universe", sic_only=True), "sic_listed": False},
        "no está en la lista",
    ),
    ("C08", {"metrics": {"days_to_earnings": 3.0}}, "reporta resultados en 3 días hábiles"),
    ("C08", {"metrics": {"days_to_earnings": 0.0}}, "reporta resultados hoy"),
    ("C10", {"regime": "RIESGO_ALTO", "score": 79.0}, "RIESGO ALTO: puntaje 79, se exige 80"),
    ("C11", {"opening_price": 50.26}, "abrió 2.0 ATR arriba"),
    ("C12", {"metrics": {"extension_atr": 3.1}}, "3.1 ATR sobre su promedio"),
    ("C13", {"plan": replace(GOOD_PLAN, targets=(50.50, 52.90))}, "está a 1.5R"),
    ("C13", {"plan": None}, "sin niveles"),
    (
        "C14",
        {"plan": replace(GOOD_PLAN, stop_level=None, stop_level_name=None)},
        "no hay un nivel técnico",
    ),
    ("C14", {"plan": replace(GOOD_PLAN, stop=47.55)}, "stop a 0.7 ATR"),
    ("C14", {"plan": replace(GOOD_PLAN, stop=45.0, targets=(60.0, 70.0))}, "stop a 3.2 ATR"),
    ("C15", {"portfolio": FULL}, "ya hay 5 posiciones abiertas; el límite es 5"),
    ("C16", {"portfolio": TWO_IN_SECTOR}, "ya hay 2 posiciones del sector XLK"),
    ("C16", {"portfolio": HEAVY_SECTOR}, "ya es 45% del capital"),
    ("C18", {"portfolio": Portfolio(cash=48.0)}, "1 acción cuesta $48.50"),
    (
        "C19",
        {"portfolio": Portfolio(cash=5000.0, stopped_out={"XYZ": date(2026, 11, 4)})},
        "hace 4 días hábiles",
    ),
    ("C20", {"portfolio": Portfolio(cash=5000.0, day_pnl_pct=-2.0)}, "pérdida del día -2.0%"),
    ("C20", {"portfolio": Portfolio(cash=5000.0, week_pnl_pct=-5.3)}, "pérdida de la semana -5.3%"),
    (
        "C21",
        {"news": [_news("investigation", 10, "SEC opens probe")]},
        "investigación regulatoria: «SEC opens probe»",
    ),
    ("C21", {"news": [_news("offering", 71)]}, "oferta de acciones"),
    ("C21", {"news": [_news("bankruptcy", 1)]}, "quiebra"),
    ("C21", {"news": [_news("downgrade", 5)]}, "rebaja de recomendación"),
    ("C22", {"news": [_news("merger", 20)]}, "fusión o compra con precio fijado"),
    ("C22", {"news": [_news("halt", 2)]}, "suspensión de cotización"),
]


@pytest.mark.parametrize(
    ("control_id", "changes", "expected_reason"),
    FAILS,
    ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(FAILS)],
)
def test_control_fails_and_blocks(control_id, changes, expected_reason):
    ctx = context(
        **{key: (dict(value) if key == "metrics" else value) for key, value in changes.items()}
    )

    result = result_of(control_id, ctx)

    assert result.result == "fail", result.reason
    assert result.effect == "block"
    assert expected_reason in result.reason
    assert ctl.is_blocked(ctl.run_controls(ctx))


PASSES = [
    ("C01", {"quality": Quality(0.8)}),
    ("C01", {"as_of": IN_SESSION, "quote": FRESH_QUOTE}),
    (
        "C02",
        {"as_of": clock.market_time(date(2026, 11, 27), time(9, 0))},
    ),  # early close: still open
    ("C03", {"metrics": {"close": 5.0}}),
    ("C04", {"metrics": {"dollar_volume_20d": 20e6}}),
    ("C05", {"market_cap_usd": 1e9}),
    ("C06", {"quote": Quote(ticker="XYZ", ts=PREMARKET, bid=48.20, ask=48.30, source="t")}),
    ("C07", {"config": _config("universe", sic_only=True), "sic_listed": True}),
    ("C07", {"sic_listed": False}),  # the SIC filter is off by default
    ("C08", {"metrics": {"days_to_earnings": 4.0}}),
    ("C08", {"metrics": {"days_to_earnings": float("nan")}}),
    ("C09", {"econ_events": [FOMC_IN_7H, MINOR_EVENT]}),
    ("C10", {"regime": "NEUTRAL", "score": 40.0}),
    ("C11", {"opening_price": 50.25}),  # exactly 2 ATR above
    ("C11", {"opening_price": 47.0}),  # a gap down is not chasing
    ("C12", {"metrics": {"extension_atr": 3.0}}),
    ("C13", {"plan": replace(GOOD_PLAN, targets=(50.58, 52.90))}),  # just over 1.5R
    ("C14", {"plan": replace(GOOD_PLAN, stop=47.45)}),  # 0.8 ATR
    ("C14", {"plan": replace(GOOD_PLAN, stop=45.25, targets=(60.0, 70.0))}),  # 3.0 ATR
    ("C15", {"portfolio": Portfolio(cash=5000.0, holdings=FULL.holdings[:4])}),
    ("C16", {"portfolio": Portfolio(cash=5000.0, holdings=(_holding("A", "XLK"),))}),
    ("C16", {"sector_etf": None, "portfolio": TWO_IN_SECTOR}),
    ("C17", {"correlations": {"AAA": 0.80, "BBB": 0.2}}),
    ("C18", {"portfolio": Portfolio(cash=48.50)}),
    (
        "C19",
        {"portfolio": Portfolio(cash=5000.0, stopped_out={"XYZ": date(2026, 11, 3)})},
    ),  # 5 days
    ("C19", {"portfolio": Portfolio(cash=5000.0, stopped_out={"OTHER": date(2026, 11, 9)})}),
    ("C20", {"portfolio": Portfolio(cash=5000.0, day_pnl_pct=-1.9, week_pnl_pct=-4.9)}),
    ("C21", {"news": [_news("investigation", 73)]}),  # older than 72 hours
    ("C21", {"news": [_news("merger", 5)]}),  # a merger is C22's business
    ("C22", {"news": [_news("merger", 80)]}),
]


@pytest.mark.parametrize(
    ("control_id", "changes"), PASSES, ids=[f"{c}-{i}" for i, (c, _) in enumerate(PASSES)]
)
def test_control_passes(control_id, changes):
    ctx = context(
        **{key: (dict(value) if key == "metrics" else value) for key, value in changes.items()}
    )

    result = result_of(control_id, ctx)

    assert result.result == "pass", result.reason
    assert result.effect == "none"


def test_every_control_has_a_failing_and_a_passing_test():
    """The acceptance criterion, checked on the test tables themselves."""
    all_ids = {spec.id for spec in ctl.CONTROLS}
    failing = {control_id for control_id, _, _ in FAILS} | {
        "C09",
        "C17",
        "C10",
    }  # see the R tests below
    passing = {control_id for control_id, _ in PASSES}

    assert failing == all_ids
    assert passing == all_ids


# --- type R controls: they reduce instead of blocking ------------------------------------------


@pytest.mark.parametrize(
    ("control_id", "changes", "expected_reason"),
    [
        (
            "C09",
            {"econ_events": [FOMC_IN_5H]},
            "FOMC a las 14:00 NY · 13:00 Monterrey: tamaño a la mitad",
        ),
        (
            "C10",
            {"regime": "RIESGO_ALTO", "score": 80.0},
            "RIESGO ALTO: puntaje 80, tamaño a la mitad",
        ),
        ("C17", {"correlations": {"AAA": 0.81, "BBB": 0.3}}, "correlación 0.81 con AAA"),
    ],
)
def test_reducing_control_halves_the_size_without_blocking(control_id, changes, expected_reason):
    ctx = context(**changes)
    results = ctl.run_controls(ctx)
    result = next(r for r in results if r.id == control_id)

    assert (result.result, result.kind, result.effect) == ("warn", "R", "reduce")
    assert expected_reason in result.reason
    assert not ctl.is_blocked(results)
    assert ctl.size_multiplier(results) == 0.5


def test_reductions_stack_down_to_a_quarter():
    two = ctl.run_controls(context(econ_events=[FOMC_IN_5H], correlations={"AAA": 0.9}))
    three = ctl.run_controls(
        context(
            econ_events=[FOMC_IN_5H], correlations={"AAA": 0.9}, regime="RIESGO_ALTO", score=90.0
        )
    )

    assert ctl.size_multiplier(two) == 0.25
    assert ctl.size_multiplier(three) == 0.25  # never below a quarter


def test_missing_live_quote_in_session_is_a_note_not_a_block():
    result = result_of("C01", context(as_of=IN_SESSION, quote=None))

    assert (result.result, result.effect) == ("warn", "none")


def test_blocking_reasons_name_the_control():
    results = ctl.run_controls(context(market_cap_usd=None, metrics={"days_to_earnings": 1.0}))

    assert ctl.blocking_reasons(results) == [
        "C05 Tamaño de empresa: capitalización desconocida",
        "C08 Resultados: reporta resultados en 1 días hábiles; no se compra a 3 días o menos",
    ]


def test_controls_veto_whatever_the_score():
    """Principle 5: a perfect score does not get past a blocking control."""
    results = ctl.run_controls(context(score=100.0, metrics={"days_to_earnings": 1.0}))

    assert ctl.is_blocked(results)


def test_fractional_shares_lower_the_cash_needed():
    config = _config("capital", fractional_shares=True)

    assert result_of("C18", context(config=config, portfolio=Portfolio(cash=1.0))).result == "pass"
    assert result_of("C18", context(config=config, portfolio=Portfolio(cash=0.40))).result == "fail"


@pytest.mark.parametrize(
    ("price", "dollar_volume", "expected"),
    [(50.0, 20e6, 0.266), (50.0, 100e6, 0.13), (200.0, 1000e6, 0.0398), (5.0, 20e6, 0.446)],
)
def test_estimated_spread(price, dollar_volume, expected):
    assert ctl.estimated_spread_pct(price, dollar_volume) == pytest.approx(expected, abs=0.001)
