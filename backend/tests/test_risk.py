"""The risk engine: size, expectancy, streak, brakes, rotation, and the plan that uses them."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine import risk
from engine.ai_client import SimulatedAI
from engine.portfolio import Holding, Portfolio
from engine.recommender import build_plan, render_plan
from engine.risk import (
    AccountHistory,
    conviction_multiplier,
    drawdown_pct,
    evaluate_brakes,
    expectancy,
    position_size,
    propose_rotations,
    remaining_ev_r,
    streak_multiplier,
)
from engine.risk_profile import profile_for
from engine.trade_plan import TradePlan
from tests.test_ai_analyst import analyst, edit
from tests.test_screener_plan import CONFIG, DAY, EIGHT, candidate, plan_for, state_and_indicators

PROFILE = profile_for(5)  # 0.75% of risk, 5 positions, 25% per position, probability 0.50
# The plan of the test candidate: close 50, ATR 1, stop under the support of 48.40.
PLAN = TradePlan(
    entry_min=49.75, entry_max=50.25, no_chase_above=50.75, reference=50.0, stop=48.3,
    stop_level=48.4, stop_level_name="soporte", targets=(53.4, 55.1), atr=1.0,
)  # fmt: skip


def size(plan=PLAN, *, capital=5000.0, cash=5000.0, profile=PROFILE, config=CONFIG, **factors):
    values = {"conviction": 1.0, "controls": 1.0, "streak": 1.0, **factors}
    return position_size(plan, capital=capital, cash=cash, profile=profile, config=config, **values)


# --- conviction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probability", "expected"),
    [(None, 0.5), (0.40, 0.5), (0.50, 0.5), (0.55, 0.75), (0.60, 1.0), (0.65, 1.25), (0.90, 1.25)],
)
def test_conviction_goes_from_half_to_one_and_a_quarter(probability, expected):
    assert conviction_multiplier(probability, min_probability=0.50) == pytest.approx(expected)


def test_conviction_is_measured_from_the_minimum_of_the_profile():
    assert conviction_multiplier(0.60, min_probability=0.60) == 0.5
    assert conviction_multiplier(0.57, min_probability=0.42) == 1.25


# --- expectancy --------------------------------------------------------------------------------


def test_expectancy_by_hand():
    """Entry in the middle of the zone (50.00), stop 48.30 -> R = 1.70. First target
    53.40 -> 2.0R. Costs: buy and sell, each 0.25% commission + 16% tax on it + 0.05%
    slippage = 0.34% of 50 = 0.17; both ways 0.34 -> 0.20R."""
    outlook = expectancy(PLAN, 0.56, min_probability=0.50, costs=CONFIG.costs)

    assert outlook.reward_r == pytest.approx(2.0)
    assert outlook.costs_r == pytest.approx(0.34 / 1.7)
    assert outlook.ev_r == pytest.approx(0.56 * 2.0 - 0.44 - 0.2)
    assert outlook.passes and not outlook.assumed_probability


def test_expectancy_without_the_ai_assumes_the_minimum_probability():
    outlook = expectancy(PLAN, None, min_probability=0.50, costs=CONFIG.costs)

    assert (outlook.probability, outlook.assumed_probability) == (0.50, True)
    assert outlook.ev_r == pytest.approx(0.30)


@pytest.mark.parametrize(
    ("probability", "passes"),
    [(0.44, False), (0.449, False), (0.45, True), (0.4501, True), (0.50, True)],
)
def test_expectancy_filter_needs_fifteen_hundredths_of_an_r(probability, passes):
    # EV = 3p - 1.2, so +0.15R is reached exactly at p = 0.45.
    outlook = expectancy(PLAN, probability, min_probability=0.42, costs=CONFIG.costs)

    assert outlook.passes is passes
    assert risk.MIN_EV_R == 0.15


def test_costs_weigh_more_on_a_tight_stop():
    tight = TradePlan(49.75, 50.25, 50.75, 50.0, 49.2, 49.3, "soporte", (51.2, 52.4), 1.0)

    outlook = expectancy(tight, 0.5, min_probability=0.5, costs=CONFIG.costs)

    # R is only 0.80, so the same 0.34 of costs is 0.43R: the idea is not worth it.
    assert outlook.costs_r == pytest.approx(0.425)
    assert outlook.ev_r == pytest.approx(0.5 * 1.5 - 0.5 - 0.425)
    assert not outlook.passes


# --- size --------------------------------------------------------------------------------------


def test_size_is_the_risk_budget_over_the_worst_case_loss_per_share():
    sizing = size()

    # 0.75% of 5,000 = 37.50; a fill at the top of the zone loses 50.25 - 48.30 = 1.95.
    assert sizing.risk_budget == pytest.approx(37.5)
    assert sizing.shares == 19
    assert sizing.amount == pytest.approx(19 * 50.25)
    assert sizing.risk_usd == pytest.approx(19 * 1.95)
    assert sizing.limited_by is None
    assert sizing.multipliers == {"conviccion": 1.0, "controles": 1.0, "racha": 1.0}


def test_size_multiplies_conviction_controls_and_streak():
    sizing = size(conviction=1.25, controls=0.5, streak=0.5)

    assert sizing.risk_budget == pytest.approx(37.5 * 1.25 * 0.5 * 0.5)
    assert sizing.shares == 6  # 11.72 / 1.95


def test_size_is_capped_by_the_position_limit_of_the_profile():
    # A stop very close: the risk budget alone would buy 5,000 x 1.25% / 0.30 shares.
    close_stop = TradePlan(49.75, 50.25, 50.75, 50.0, 49.95, 50.05, "soporte", (53.0, 55.0), 1.0)

    sizing = size(close_stop, conviction=1.25)

    assert sizing.shares == 24  # 25% of 5,000 = 1,250 -> 24 shares of 50.25
    assert sizing.amount <= 1250.0
    assert sizing.limited_by == "tope de 25% del capital por posición"


def test_size_is_capped_by_cash_and_by_sector_room():
    by_cash = size(cash=300.0)
    by_sector = position_size(
        PLAN, capital=5000.0, cash=5000.0, profile=PROFILE, config=CONFIG,
        conviction=1.0, controls=1.0, streak=1.0, sector_value=1800.0,
    )  # fmt: skip

    assert (by_cash.shares, by_cash.limited_by) == (5, "efectivo disponible")
    # 40% of 5,000 = 2,000 per sector; 1,800 are already there -> 200 left -> 3 shares.
    assert (by_sector.shares, by_sector.limited_by) == (
        3,
        "tope de 40% del capital por sector",
    )


def test_fractional_shares_round_to_hundredths():
    fractional = CONFIG.model_copy(
        update={"capital": CONFIG.capital.model_copy(update={"fractional_shares": True})}
    )

    sizing = size(config=fractional)

    assert sizing.shares == 19.23  # 37.50 / 1.95 = 19.2307...
    assert sizing.risk_usd <= 37.5


@pytest.mark.parametrize("aggressiveness", range(1, 11))
@pytest.mark.parametrize("capital", [800.0, 5000.0, 60_000.0])
@pytest.mark.parametrize("conviction", [0.5, 1.0, 1.25])
def test_no_size_breaks_the_limits_of_the_aggressiveness_profile(
    aggressiveness, capital, conviction
):
    profile = profile_for(aggressiveness)
    for stop in (49.95, 49.2, 48.3, 47.0):
        plan = TradePlan(49.75, 50.25, 50.75, 50.0, stop, stop + 0.1, "soporte", (56.0, 59.0), 1.0)

        sizing = size(plan, capital=capital, cash=capital, profile=profile, conviction=conviction)

        assert sizing.amount <= profile.max_position_pct / 100 * capital + 1e-6
        assert sizing.amount <= capital + 1e-6
        assert sizing.risk_usd <= profile.risk_pct / 100 * capital * conviction + 1e-6
        assert sizing.shares == int(sizing.shares) and sizing.shares >= 0


# --- streak ------------------------------------------------------------------------------------


def curve(*values: float) -> tuple[tuple[date, float], ...]:
    return tuple((DAY + timedelta(days=i), value) for i, value in enumerate(values))


def test_three_losses_in_a_row_halve_the_size_until_half_is_recovered():
    def after(*results):
        return streak_multiplier(AccountHistory(closed_results=results))

    assert after(-20, -30)[0] == 1.0
    assert after(-20, 15, -30, -10)[0] == 1.0  # a gain in between breaks the streak
    multiplier, reason = after(40, -20, -30, -10)
    assert multiplier == 0.5
    assert reason == "3 pérdidas seguidas ($60); el tamaño vuelve al recuperar $30"
    assert after(-20, -30, -10, 29)[0] == 0.5  # 29 back of the 60 lost: not yet
    assert after(-20, -30, -10, 30)[0] == 1.0  # half is back
    assert after(-20, -30, -10, -15, 40)[0] == 0.5  # a further loss has to be recovered too
    assert after(-20, -30, -10, -15, 45)[0] == 1.0


def test_a_fall_of_more_than_five_percent_halves_the_size_until_half_is_recovered():
    def after(*values):
        return streak_multiplier(AccountHistory(equity_curve=curve(*values)))

    assert after(5000, 5100, 4850)[0] == 1.0  # -4.9% from 5,100
    multiplier, reason = after(5000, 5100, 4800)  # -5.9%
    assert multiplier == 0.5
    assert reason == (
        "caída de 5.9% desde el máximo de $5,100; el tamaño vuelve al recuperar $4,950"
    )
    assert after(5000, 5100, 4800, 4700, 4890)[0] == 0.5  # trough 4,700: needs 4,900
    assert after(5000, 5100, 4800, 4700, 4900)[0] == 1.0


def test_no_history_means_normal_size():
    assert streak_multiplier(None) == (1.0, "")
    assert streak_multiplier(AccountHistory()) == (1.0, "")


def test_drawdown_is_measured_from_the_highest_capital():
    history = AccountHistory(equity_curve=curve(5000, 5400, 5200))

    assert drawdown_pct(history) == pytest.approx(100 * 200 / 5400)
    assert drawdown_pct(history, capital=4860.0) == pytest.approx(10.0)
    assert drawdown_pct(None) == 0.0


# --- brakes ------------------------------------------------------------------------------------


def brakes(day=0.0, week=0.0, drawdown=0.0, latched=()):
    return evaluate_brakes(
        day_pnl_pct=day, week_pnl_pct=week, drawdown=drawdown, controls=CONFIG.controls,
        latched=latched,
    )  # fmt: skip


def test_no_brake_in_a_normal_day():
    brake = brakes(day=-1.9, week=-4.9, drawdown=9.9)

    assert (brake.state, brake.active, brake.allows_buying) == ("NORMAL", (), True)


def test_daily_loss_stops_purchases_until_tomorrow():
    brake = brakes(day=-2.0)

    assert (brake.state, brake.active) == ("SOLO_VIGILAR", ("day",))
    assert not brake.allows_buying and not brake.needs_user
    assert brake.text == "pérdida del día de 2.0% (freno en 2%): sin compras nuevas hasta mañana"


def test_weekly_loss_stops_purchases_until_the_user_lifts_it():
    tripped = brakes(week=-5.3)
    next_week = brakes(week=0.0, latched=("week",))

    assert (tripped.state, tripped.active, tripped.needs_user) == ("SOLO_VIGILAR", ("week",), True)
    assert "pérdida de la semana de 5.3%" in tripped.text
    # A new week does not lift it by itself.
    assert (next_week.state, next_week.active) == ("SOLO_VIGILAR", ("week",))
    assert "sin compras hasta que lo reactives" in next_week.text


def test_ten_percent_from_the_peak_pauses_the_system():
    brake = brakes(drawdown=10.0)

    assert (brake.state, brake.active, brake.needs_user) == ("PAUSA", ("drawdown",), True)
    assert "caída de 10.0% desde el máximo (pausa en 10%): revisión obligatoria" in brake.text
    assert brakes(drawdown=3.0, latched=("drawdown",)).state == "PAUSA"


# --- rotation ----------------------------------------------------------------------------------


def holding(ticker="OLD", *, last=104.0, stop=100.0, target=106.0, probability=None) -> Holding:
    return Holding(
        ticker, "XLK", 10.0, 100.0, last, stop, DAY - timedelta(days=3),
        initial_stop=96.0, target=target, probability=probability,
    )  # fmt: skip


def test_what_is_left_in_a_position_in_r():
    # Initial risk 4.00. At 104 with the stop raised to 100 and the target at 106:
    # 0.5 x 0.5R left to gain - 0.5 x 1.0R that can still be given back = -0.25R.
    assert remaining_ev_r(holding(), 0.5) == pytest.approx(-0.25)
    assert remaining_ev_r(holding(probability=0.7), 0.5) == pytest.approx(0.7 * 0.5 - 0.3 * 1.0)
    # Past its target there is nothing left to gain.
    assert remaining_ev_r(holding(last=107.0, stop=104.0), 0.5) == pytest.approx(-0.375)


def test_rotation_is_proposed_when_a_new_idea_beats_the_weakest_position_by_half_an_r():
    weak, strong = holding("WEAK"), holding("STRONG", last=101.0, stop=96.0, target=108.0)

    (rotation,) = propose_rotations([("NEW", 0.40), ("OTHER", 0.20)], [strong, weak], 0.5)

    assert (rotation.sell, rotation.buy, rotation.edge_r) == ("WEAK", "NEW", 0.65)
    assert rotation.text == (
        "vende WEAK para comprar NEW: a WEAK le queda una expectativa de -0.25R y NEW ofrece "
        "+0.40R. La decisión es tuya."
    )


def test_no_rotation_without_a_clear_edge():
    assert propose_rotations([("NEW", 0.25)], [holding()], 0.5) == []  # edge of exactly 0.50R
    assert propose_rotations([], [holding()], 0.5) == []
    assert propose_rotations([("NEW", 0.9)], [], 0.5) == []


# --- the plan with the risk engine -------------------------------------------------------------


def test_with_five_thousand_the_amounts_never_exceed_the_cash():
    for cash in (5000.0, 2600.0, 900.0, 120.0, 0.0):
        for aggressiveness in (1, 5, 10):
            config = CONFIG.model_copy(
                update={
                    "system": CONFIG.system.model_copy(update={"aggressiveness": aggressiveness})
                }
            )
            many = [candidate(f"T{i:02d}", 95.0 - i) for i in range(12)]
            state, indicators = state_and_indicators(many)
            for the_analyst in (None, analyst(SimulatedAI())):
                plan = build_plan(
                    state, indicators, Portfolio(cash=cash), config, candidates=many,
                    analyst=the_analyst,
                )  # fmt: skip
                buys = plan.by_verdict("COMPRAR")
                profile = profile_for(aggressiveness)

                assert sum(idea.amount for idea in buys) <= cash + 1e-6
                assert len(buys) <= profile.max_positions
                for idea in buys:
                    assert idea.amount <= profile.max_position_pct / 100 * cash + 1e-6
                    assert idea.risk_usd <= profile.risk_pct / 100 * cash * 1.25 + 1e-6
                assert len(plan.ideas) >= 5


def test_purchases_are_listed_and_funded_from_the_best_expectancy_down():
    # The lower score has the higher probability, so the better expectancy: it goes first.
    ai = edit("T1", prob_objetivo_antes_que_stop=0.64)
    state, indicators = state_and_indicators(EIGHT[:2])
    held = tuple(
        Holding(f"H{i}", f"X{i}", 10, 247.0, 247.0, 240.0, date(2026, 11, 5)) for i in range(2)
    )
    portfolio = Portfolio(cash=300.0, holdings=held)  # capital 5,240, almost all invested

    plan = build_plan(
        state, indicators, portfolio, CONFIG, candidates=EIGHT[:2], analyst=analyst(ai)
    )
    first, second = plan.ideas

    assert (first.ticker, first.verdict) == ("T1", "COMPRAR")
    assert first.ev_r > second.ev_r
    # T1 took the cash there was (5 shares); T0 waits, and the plan proposes a rotation.
    assert (first.shares, first.limited_by) == (5, "efectivo disponible")
    assert (second.ticker, second.verdict, second.reason) == (
        "T0",
        "VIGILAR",
        "COMPRAR si liberas efectivo",
    )
    assert first.amount <= 300.0
    (rotation,) = plan.rotations
    assert (rotation.sell, rotation.buy) == ("H0", "T0")
    assert "Rotación propuesta: vende H0 para comprar T0" in render_plan(plan, CONFIG, 5240.0)


def test_an_idea_without_enough_expectancy_is_not_a_purchase():
    plan = plan_for([candidate("OK", 85.0), candidate("POOR", 84.0)])
    state, indicators = state_and_indicators([candidate("OK", 85.0), candidate("POOR", 84.0)])
    with_ai = build_plan(
        state, indicators, Portfolio(cash=5000.0), CONFIG,
        candidates=[candidate("OK", 85.0), candidate("POOR", 84.0)],
        analyst=analyst(edit("POOR", prob_objetivo_antes_que_stop=0.44)),
        calibrate=lambda p: p,
    )  # fmt: skip
    aggressive = CONFIG.model_copy(
        update={"system": CONFIG.system.model_copy(update={"aggressiveness": 10})}
    )
    below = build_plan(
        state, indicators, Portfolio(cash=5000.0), aggressive,
        candidates=[candidate("OK", 85.0), candidate("POOR", 84.0)],
        analyst=analyst(edit("POOR", prob_objetivo_antes_que_stop=0.44)),
    )  # fmt: skip
    poor = next(i for i in below.ideas if i.ticker == "POOR")

    assert [i.verdict for i in plan.ideas] == ["COMPRAR", "COMPRAR"]
    assert next(i for i in with_ai.ideas if i.ticker == "POOR").verdict == "VIGILAR"
    # At aggressiveness 10 a probability of 0.44 is allowed (minimum 0.42), but the
    # expectancy is 0.44 x 2.0 - 0.56 - 0.20 = +0.12R: under the +0.15R minimum.
    assert poor.verdict == "VIGILAR"
    assert poor.reason == "expectativa de +0.12R después de costos; el mínimo es +0.15R"
    assert poor.shares == 0


def test_the_calibrated_probability_is_the_one_that_counts():
    state, indicators = state_and_indicators(EIGHT[:1])

    plan = build_plan(
        state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=EIGHT[:1],
        analyst=analyst(edit("T0", prob_objetivo_antes_que_stop=0.70)),
        calibrate=lambda raw: raw - 0.15,  # the AI has been 15 points too optimistic
    )  # fmt: skip
    idea = plan.ideas[0]

    assert idea.ai.probability == 0.70  # what the AI said is kept
    assert idea.probability == pytest.approx(0.55)
    assert idea.multipliers["conviccion"] == pytest.approx(0.75)
    assert idea.ev_r == pytest.approx(0.55 * 2.0 - 0.45 - 0.2)


def test_a_brake_turns_every_purchase_into_watch():
    brake = brakes(day=-2.4)
    state, indicators = state_and_indicators(EIGHT)

    plan = build_plan(
        state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=EIGHT, brake=brake
    )

    assert plan.by_verdict("COMPRAR") == []
    assert len(plan.ideas) >= 5
    assert plan.ideas[0].reason.startswith("freno automático: pérdida del día de 2.4%")
    text = render_plan(plan, CONFIG, capital=5000.0)
    assert "SOLO VIGILAR: pérdida del día de 2.4% (freno en 2%)" in text
    assert "PAUSA: caída de 11.0%" in render_plan(
        build_plan(
            state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=EIGHT,
            brake=brakes(drawdown=11.0),
        ),
        CONFIG,
        capital=5000.0,
    )  # fmt: skip


def test_a_bad_run_halves_every_size_and_the_plan_says_so():
    history = AccountHistory(closed_results=(-20.0, -30.0, -10.0))
    state, indicators = state_and_indicators(EIGHT[:1])

    normal = build_plan(state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=EIGHT[:1])
    reduced = build_plan(
        state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=EIGHT[:1], history=history
    )

    assert (normal.ideas[0].shares, reduced.ideas[0].shares) == (9, 4)
    assert reduced.ideas[0].multipliers["racha"] == 0.5
    assert "Tamaño a la mitad por racha: 3 pérdidas seguidas ($60)" in render_plan(
        reduced, CONFIG, capital=5000.0
    )


def test_the_plan_proposes_a_rotation_when_there_is_no_room_for_a_better_idea():
    # Five positions open (the limit): four healthy ones and one with little left to give.
    healthy = tuple(
        Holding(f"H{i}", f"X{i}", 5.0, 100.0, 101.0, 96.0, DAY - timedelta(days=2),
                initial_stop=96.0, target=108.0)
        for i in range(4)
    )  # fmt: skip
    tired = Holding("TIRED", "X9", 5.0, 100.0, 105.5, 100.0, DAY - timedelta(days=6),
                    initial_stop=96.0, target=106.0)  # fmt: skip
    portfolio = Portfolio(cash=2000.0, holdings=(*healthy, tired))
    candidates = [EIGHT[0], candidate("WEAKIDEA", 50.0)]
    state, indicators = state_and_indicators(candidates)

    plan = build_plan(state, indicators, portfolio, CONFIG, candidates=candidates)
    idea = next(i for i in plan.ideas if i.ticker == "T0")

    # The control of the position limit still blocks the purchase...
    assert idea.verdict == "NO_HOY" and idea.reason.startswith("C15 ")
    # ...but the plan says what could be swapped. TIRED: 0.5 x 0.125R left to gain
    # - 0.5 x 1.375R at risk = -0.625R; the new idea offers +0.30R: an edge of 0.925R.
    (rotation,) = plan.rotations
    assert (rotation.sell, rotation.buy, rotation.edge_r) == ("TIRED", "T0", 0.92)
    assert "La decisión es tuya." in rotation.text
    # A brake means no purchases, so no rotations either.
    braked = build_plan(
        state, indicators, portfolio, CONFIG, candidates=candidates, brake=brakes(day=-3.0)
    )
    assert braked.rotations == ()
