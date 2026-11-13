"""State machine, the saved plan, manual confirmations, the account and the shadows."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from core import clock
from db import models
from db.session import session_scope
from engine import account, confirmations, exits, plan_store, states
from engine.ai_client import SimulatedAI
from engine.confirmations import ConfirmationError, NotFound
from engine.portfolio import Portfolio
from engine.recommender import build_plan
from engine.states import InvalidTransition
from tests.test_ai_analyst import analyst
from tests.test_screener_plan import AS_OF, CONFIG, DAY, candidate, plan_for, state_and_indicators

# BUY and TWO are purchases (9 shares each), NEAR is watched, LOW and CAP are not for today.
MIXED = [candidate("BUY", 85.0), candidate("TWO", 83.0), candidate("NEAR", 65.0),
         candidate("LOW", 40.0), candidate("CAP", 88.0)]  # fmt: skip
NEXT_DAY = DAY + timedelta(days=1)


@pytest.fixture(autouse=True)
def _fixed_time():
    with clock.simulated(AS_OF) as simulated:
        yield simulated


@pytest.fixture
def saved(engine):
    """Today's plan, saved: returns the id of each idea by ticker."""
    plan = plan_for(MIXED, caps={"CAP": 4e8})
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan)
    with session_scope(engine) as session:
        return {idea.ticker: idea.id for idea in plan_store.todays_ideas(session, DAY)}


def bar(day: date, o: float, h: float, low: float, c: float) -> exits.Bar:
    return exits.Bar(day, o, h, low, c)


def add_bars(engine, day: date, closes: dict[str, float]) -> None:
    with session_scope(engine) as session:
        session.add_all(
            models.BarDaily(ticker=t, session_date=day, open=c, high=c, low=c, close=c,
                            volume=1e6, source="test", updated_at=clock.now())
            for t, c in closes.items()
        )  # fmt: skip


def state_of(engine, model, row_id: int) -> str:
    with session_scope(engine) as session:
        return session.get(model, row_id).state


def transitions(engine, entity: str, entity_id: int) -> list[tuple]:
    with session_scope(engine) as session:
        rows = session.scalars(
            select(models.StateTransition)
            .where(
                models.StateTransition.entity == entity,
                models.StateTransition.entity_id == str(entity_id),
            )
            .order_by(models.StateTransition.id)
        )
        return [(r.from_state, r.to_state, r.actor, r.reason) for r in rows]


# --- state machine -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity", "start", "end"),
    [
        ("idea", "PROPOSED", "BOUGHT"),
        ("idea", "PROPOSED", "DECLINED"),
        ("idea", "PROPOSED", "EXPIRED"),
        ("idea", "DECLINED", "SHADOW"),
        ("idea", "DECLINED", "NOT_FILLABLE"),
        ("idea", "EXPIRED", "SHADOW"),
        ("idea", "EXPIRED", "NOT_FILLABLE"),
        ("position", "OPEN", "SELL_ALERT"),
        ("position", "SELL_ALERT", "PARTIALLY_CLOSED"),
        ("position", "PARTIALLY_CLOSED", "CLOSED"),
        ("position", "OPEN", "CLOSED"),
        ("position", "SELL_ALERT", "OPEN"),
    ],
)
def test_valid_transitions(entity, start, end):
    assert states.can_transition(entity, start, end)


@pytest.mark.parametrize(
    ("entity", "start", "end"),
    [
        ("idea", "PROPOSED", "SHADOW"),  # a shadow needs a "no" or an expiry first
        ("idea", "PROPOSED", "NOT_FILLABLE"),
        ("idea", "BOUGHT", "SHADOW"),
        ("idea", "BOUGHT", "PROPOSED"),
        ("idea", "SHADOW", "DECLINED"),
        ("idea", "DECLINED", "BOUGHT"),  # only as an explicit correction
        ("idea", "NOT_FILLABLE", "SHADOW"),
        ("position", "CLOSED", "OPEN"),
        ("position", "CLOSED", "CLOSED"),
        ("position", "PARTIALLY_CLOSED", "OPEN"),
    ],
)
def test_invalid_transitions(entity, start, end):
    assert not states.can_transition(entity, start, end)


def test_corrections_are_extra_moves_that_must_be_asked_for():
    assert states.can_transition("idea", "DECLINED", "BOUGHT", correction=True)
    assert states.can_transition("idea", "SHADOW", "BOUGHT", correction=True)
    assert states.can_transition("idea", "BOUGHT", "DECLINED", correction=True)
    assert not states.can_transition("idea", "SHADOW", "DECLINED", correction=True)
    assert not states.can_transition("position", "CLOSED", "OPEN", correction=True)


def test_every_state_of_the_database_is_in_the_machine():
    assert set(states.IDEA_TRANSITIONS) == set(models.IDEA_STATES)
    assert set(states.POSITION_TRANSITIONS) == set(models.POSITION_STATES)


def test_a_transition_is_audited_with_time_actor_and_reason(engine, saved, _fixed_time):
    _fixed_time.advance(timedelta(minutes=40))
    with session_scope(engine) as session:
        idea = session.get(models.Idea, saved["BUY"])
        states.transition(session, "idea", idea, "DECLINED", actor="user", reason="No la compré")

    assert state_of(engine, models.Idea, saved["BUY"]) == "DECLINED"
    assert transitions(engine, "idea", saved["BUY"]) == [
        (None, "PROPOSED", "system", "Plan del día publicado"),
        ("PROPOSED", "DECLINED", "user", "No la compré"),
    ]
    with session_scope(engine) as session:
        last = session.scalars(
            select(models.StateTransition).order_by(models.StateTransition.id.desc())
        ).first()
        assert last.at == AS_OF + timedelta(minutes=40)


def test_an_invalid_transition_changes_nothing_and_puts_the_system_in_caution(engine, saved):
    with pytest.raises(InvalidTransition) as error, states.guarded_session(engine) as session:
        idea = session.get(models.Idea, saved["BUY"])
        idea.thesis = "esto no debe guardarse"
        states.transition(session, "idea", idea, "SHADOW", actor="system", reason="cierre")

    assert str(error.value) == (
        f"transición inválida: la idea {saved['BUY']} está en PROPOSED y no puede pasar a SHADOW"
    )
    # Nothing of that transaction was applied...
    with session_scope(engine) as session:
        idea = session.get(models.Idea, saved["BUY"])
        assert (idea.state, idea.thesis) == ("PROPOSED", None)
        # ...and the incident is waiting for the user.
        (incident,) = states.open_incidents(session)
        assert incident.component == f"incident/idea/{saved['BUY']}"
        assert incident.status == "PRECAUCION" and incident.message == str(error.value)
    assert len(transitions(engine, "idea", saved["BUY"])) == 1

    with session_scope(engine) as session:
        assert states.resolve_incident(session, incident.component, "revisado") is True
        assert states.resolve_incident(session, incident.component) is False
        assert states.open_incidents(session) == []


# --- the saved plan ----------------------------------------------------------------------------


def test_saving_the_plan_keeps_everything_needed_to_reproduce_it(engine, saved):
    with session_scope(engine) as session:
        data = plan_store.plan_as_dict(session, DAY)
        run = session.get(models.Run, data["run_id"])
        candidates = session.scalars(select(models.Candidate)).all()
        results = session.scalars(select(models.ControlResult)).all()

    assert (run.kind, run.regime, run.as_of) == ("premarket", "ALCISTA", AS_OF)
    assert len(data["ideas"]) == 5 and len(candidates) == 5
    assert len(results) == 5 * 22  # the result of every control, for every idea
    assert [(i["ticker"], i["verdict"]) for i in data["ideas"]] == [
        ("BUY", "COMPRAR"), ("TWO", "COMPRAR"), ("NEAR", "VIGILAR"), ("CAP", "NO_HOY"),
        ("LOW", "NO_HOY"),
    ]  # fmt: skip
    buy = data["ideas"][0]
    assert buy["name"] == "BUY Corp." and buy["setup"] == "RUPTURA" and buy["score"] == 85.0
    assert buy["entry"] == {"type": "limite", "min": 49.75, "max": 50.25, "no_chase_above": 50.75}
    assert (buy["stop"], buy["targets"], buy["targets_r"]) == (48.3, [53.4, 55.1], [2.0, 3.0])
    assert (buy["shares"], buy["amount_usd"], buy["risk_usd"]) == (
        9,
        pytest.approx(452.25),
        pytest.approx(17.55),
    )
    assert buy["ev_r"] == pytest.approx(0.30)
    assert buy["multipliers"] == {"conviccion": 0.5, "controles": 1.0, "racha": 1.0}
    assert buy["state"] == "PROPOSED" and buy["no_ai"] is True
    assert len(buy["controls"]) == 22 and buy["notes"] == ["rompe máximo de 20 días"]
    cap = data["ideas"][3]
    assert cap["reason"].startswith("C05 Tamaño de empresa")
    assert [c for c in cap["controls"] if c["result"] == "fail"][0]["id"] == "C05"
    assert data["market_reading"].startswith("Régimen ALCISTA")
    assert data["published_at_label"] == "09:00 NY · 08:00 Monterrey"


def test_a_plan_with_the_ai_keeps_what_the_ai_said(engine):
    state, indicators = state_and_indicators(MIXED)
    plan = build_plan(
        state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=MIXED,
        analyst=analyst(SimulatedAI()),
    )  # fmt: skip
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan)
        data = plan_store.plan_as_dict(session, DAY)
        row = session.scalars(select(models.Idea).where(models.Idea.ticker == "BUY")).one()

    assert data["ai"]["status"] == "ok" and data["ai"]["label"] == "con IA"
    assert (row.no_ai, row.prob_raw, row.prob_calibrated) == (False, 0.575, 0.575)
    assert row.thesis == "IA simulada: RUPTURA con puntaje 85."
    assert row.horizon_days == 5 and row.invalidation == "cierre debajo de 48.30"
    buy = next(idea for idea in data["ideas"] if idea["ticker"] == "BUY")
    assert buy["probability"] == 0.575 and buy["risks"] == ["la IA simulada no analiza riesgos"]


def test_a_new_plan_replaces_todays_until_someone_acted_on_it(engine, saved):
    again = plan_for(MIXED[:3] + [candidate("NEW", 90.0), candidate("OTHER", 30.0)])
    with session_scope(engine) as session:
        plan_store.save_plan(session, again)
    with session_scope(engine) as session:
        tickers = [idea.ticker for idea in plan_store.todays_ideas(session, DAY)]
        assert tickers == ["NEW", "BUY", "TWO", "NEAR", "OTHER"]
        assert len(session.scalars(select(models.Idea)).all()) == 5  # the old ones are gone
        idea_id = plan_store.todays_ideas(session, DAY)[0].id
        confirmations.confirm_declined(session, idea_id)

    with (
        pytest.raises(plan_store.PlanLocked, match="ya tiene confirmaciones"),
        session_scope(engine) as session,
    ):
        plan_store.save_plan(session, again)


def test_recheck_after_the_open_demotes_purchases_that_opened_too_high(engine, saved):
    with session_scope(engine) as session:
        changed = plan_store.recheck_open(
            session, CONFIG, DAY, {"BUY": 51.2, "TWO": 52.6, "NEAR": 55.0}
        )

    assert changed == [
        ("BUY", "VIGILAR", "abrió en $51.20, arriba del precio de no perseguir ($50.75)"),
        ("TWO", "NO_HOY", "C11 Gap: abrió 2.6 ATR arriba del cierre; el máximo es 2: no perseguir"),
    ]
    with session_scope(engine) as session:
        buy = session.get(models.Idea, saved["BUY"])
        assert (buy.verdict, buy.shares, buy.state) == ("VIGILAR", 0, "PROPOSED")
    assert transitions(engine, "idea_verdict", saved["BUY"]) == [
        ("COMPRAR", "VIGILAR", "system",
         "revisión tras la apertura: abrió en $51.20, arriba del precio de no perseguir ($50.75)"),
    ]  # fmt: skip
    # The saved plan and a plan rebuilt with the opening prices agree.
    rebuilt = plan_for(MIXED, caps={"CAP": 4e8}, opening_prices={"BUY": 51.2, "TWO": 52.6})
    by_ticker = {idea.ticker: idea.verdict for idea in rebuilt.ideas}
    assert (by_ticker["BUY"], by_ticker["TWO"]) == ("VIGILAR", "NO_HOY")


def test_recheck_leaves_alone_what_opened_inside_the_zone_or_was_already_bought(engine, saved):
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["TWO"], price=50.2, qty=9)
    with session_scope(engine) as session:
        assert plan_store.recheck_open(session, CONFIG, DAY, {"BUY": 50.1, "TWO": 51.9}) == []


# --- "La compré" -------------------------------------------------------------------------------


def test_bought_creates_the_real_position_with_the_price_actually_paid(engine, saved, _fixed_time):
    _fixed_time.advance(timedelta(minutes=50))
    with session_scope(engine) as session:
        position = confirmations.confirm_bought(
            session, saved["BUY"], price=50.10, qty=9, commission=1.31, note="Banorte"
        )
        position_id = position.id

    with session_scope(engine) as session:
        position = session.get(models.Position, position_id)
        assert (position.is_shadow, position.state, position.ticker) == (False, "OPEN", "BUY")
        assert (position.qty_open, position.avg_price, position.stop) == (9, 50.10, 48.3)
        assert position.targets == [53.4, 55.1]
        # R is recalculated from the real price: 50.10 - 48.30.
        assert position.initial_r == pytest.approx(1.80)
        assert position.opened_at == AS_OF + timedelta(minutes=50)
        (fill,) = session.scalars(select(models.Fill)).all()
        assert (fill.side, fill.price, fill.qty, fill.commission, fill.origin) == (
            "buy", 50.10, 9, 1.31, "user",
        )  # fmt: skip
        (confirmation,) = session.scalars(select(models.Confirmation)).all()
        assert (confirmation.kind, confirmation.idea_id, confirmation.position_id) == (
            "bought", saved["BUY"], position_id,
        )  # fmt: skip
        assert confirmation.note == "Banorte" and confirmation.data["price"] == 50.10
    assert state_of(engine, models.Idea, saved["BUY"]) == "BOUGHT"
    assert transitions(engine, "idea", saved["BUY"])[-1] == (
        "PROPOSED", "BOUGHT", "user", "La compré",
    )  # fmt: skip
    assert transitions(engine, "position", position_id) == [
        (None, "OPEN", "user", "compra confirmada")
    ]


def test_a_second_purchase_adds_to_the_position(engine, saved):
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["BUY"], price=50.00, qty=6)
    with session_scope(engine) as session:
        position = confirmations.confirm_bought(session, saved["BUY"], price=50.30, qty=3)
        assert position.qty_open == 9
        assert position.avg_price == pytest.approx(50.10)
        assert position.initial_r == pytest.approx(1.80)
        assert position.tracking["initial_shares"] == 9
        assert len(session.scalars(select(models.Position)).all()) == 1


@pytest.mark.parametrize(
    ("ticker", "values", "message"),
    [
        ("BUY", {"price": 48.3, "qty": 9}, "debe ser mayor al stop"),
        ("BUY", {"price": 50.1, "qty": 0}, "mayores a cero"),
        ("BUY", {"price": 50.1, "qty": 9, "commission": -1}, "mayores a cero"),
    ],
)
def test_a_purchase_that_makes_no_sense_is_refused(engine, saved, ticker, values, message):
    with pytest.raises(ConfirmationError, match=message), session_scope(engine) as session:
        confirmations.confirm_bought(session, saved[ticker], **values)

    assert state_of(engine, models.Idea, saved[ticker]) == "PROPOSED"
    with session_scope(engine) as session:
        assert session.scalars(select(models.Position)).all() == []


def test_a_purchase_in_the_future_or_of_an_unknown_idea_is_refused(engine, saved):
    with pytest.raises(ConfirmationError, match="futuro"), session_scope(engine) as session:
        confirmations.confirm_bought(
            session, saved["BUY"], price=50.1, qty=9, filled_at=AS_OF + timedelta(hours=3)
        )
    with pytest.raises(NotFound), session_scope(engine) as session:
        confirmations.confirm_bought(session, 999_999, price=50.1, qty=9)


def test_correcting_a_purchase_keeps_the_original_and_recalculates(engine, saved):
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["BUY"], price=50.10, qty=9)
        original = session.scalars(select(models.Confirmation)).one().id
    with session_scope(engine) as session:
        position = confirmations.correct_purchase(
            session, original, price=50.40, qty=8, note="me equivoqué de precio"
        )
        assert (position.avg_price, position.qty_open) == (50.40, 8)
        assert position.initial_r == pytest.approx(2.10)

    with session_scope(engine) as session:
        first, second = session.scalars(
            select(models.Confirmation).order_by(models.Confirmation.id)
        ).all()
        assert first.data["price"] == 50.10 and first.corrects_id is None  # untouched
        assert second.corrects_id == first.id
        assert second.data["before"] == {"price": 50.10, "qty": 9, "commission": 0.0}
        assert second.note == "me equivoqué de precio"


# --- "No la compré" and corrections ------------------------------------------------------------


def test_declined_is_recorded_with_its_reason_and_a_repeat_changes_nothing(engine, saved):
    with session_scope(engine) as session:
        confirmations.confirm_declined(session, saved["BUY"], reason="sin efectivo")
        confirmations.confirm_declined(session, saved["BUY"], reason="otro")

    assert state_of(engine, models.Idea, saved["BUY"]) == "DECLINED"
    assert transitions(engine, "idea", saved["BUY"])[1:] == [
        ("PROPOSED", "DECLINED", "user", "No la compré: sin efectivo")
    ]
    with session_scope(engine) as session:
        (confirmation,) = session.scalars(select(models.Confirmation)).all()
        assert (confirmation.kind, confirmation.data["reason"]) == ("declined", "sin efectivo")


def test_a_no_can_be_corrected_to_a_yes_and_it_is_audited(engine, saved):
    with session_scope(engine) as session:
        confirmations.confirm_declined(session, saved["BUY"])
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9)

    assert transitions(engine, "idea", saved["BUY"])[-1] == (
        "DECLINED", "BOUGHT", "user", "corrección: La compré",
    )  # fmt: skip
    with session_scope(engine) as session:
        declined, bought = session.scalars(
            select(models.Confirmation).order_by(models.Confirmation.id)
        ).all()
        assert bought.corrects_id == declined.id


def test_a_yes_entered_by_mistake_can_be_undone_while_nothing_was_sold(engine, saved):
    with session_scope(engine) as session:
        position_id = confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9).id
    with session_scope(engine) as session:
        confirmations.confirm_declined(session, saved["BUY"], reason="otro")

    assert state_of(engine, models.Idea, saved["BUY"]) == "DECLINED"
    with session_scope(engine) as session:
        position = session.get(models.Position, position_id)
        assert (position.state, position.qty_open) == ("CLOSED", 0)
        assert account.is_voided(position)
        # A cancelled purchase never took cash.
        assert account.cash_balance(session, CONFIG) == 5000.0
        assert account.real_positions(session) == []


def test_a_purchase_with_sales_cannot_be_undone(engine, saved):
    with session_scope(engine) as session:
        position_id = confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9).id
        confirmations.confirm_sold(session, position_id, price=53.4, qty=4)
    with (
        pytest.raises(ConfirmationError, match="ya tiene ventas"),
        session_scope(engine) as session,
    ):
        confirmations.confirm_declined(session, saved["BUY"])

    assert state_of(engine, models.Idea, saved["BUY"]) == "BOUGHT"


# --- "Vendí" -----------------------------------------------------------------------------------


def test_partial_and_total_sales_move_the_position_through_its_states(engine, saved, _fixed_time):
    with session_scope(engine) as session:
        position_id = confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9).id
    _fixed_time.advance(timedelta(days=1))
    with session_scope(engine) as session:
        position = confirmations.confirm_sold(
            session, position_id, price=53.4, qty=4, reason="objetivo"
        )
        assert (position.state, position.qty_open, position.closed_at) == (
            "PARTIALLY_CLOSED", 5, None,
        )  # fmt: skip
        confirmations.confirm_sold(session, position_id, price=54.0, qty=2)
    _fixed_time.advance(timedelta(days=1))
    with session_scope(engine) as session:
        position = confirmations.confirm_sold(session, position_id, price=55.1, qty=3)
        assert (position.state, position.qty_open) == ("CLOSED", 0)
        assert position.closed_at == AS_OF + timedelta(days=2)

    assert transitions(engine, "position", position_id) == [
        (None, "OPEN", "user", "compra confirmada"),
        ("OPEN", "PARTIALLY_CLOSED", "user", "Vendí 4 a $53.40 (objetivo)"),
        ("PARTIALLY_CLOSED", "CLOSED", "user", "Vendí 3 a $55.10"),
    ]


def test_selling_more_than_is_left_is_refused(engine, saved):
    with session_scope(engine) as session:
        position_id = confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9).id
    with pytest.raises(ConfirmationError, match="solo quedan 9"), session_scope(engine) as session:
        confirmations.confirm_sold(session, position_id, price=53.4, qty=10)
    with pytest.raises(NotFound), session_scope(engine) as session:
        confirmations.confirm_sold(session, 999_999, price=53.4, qty=1)


def test_selling_a_closed_position_is_an_invalid_transition(engine, saved):
    with session_scope(engine) as session:
        position_id = confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9).id
        confirmations.confirm_sold(session, position_id, price=53.4, qty=9)

    with pytest.raises(InvalidTransition), states.guarded_session(engine) as session:
        confirmations.confirm_sold(session, position_id, price=55.0, qty=1)

    with session_scope(engine) as session:
        (incident,) = states.open_incidents(session)
        assert incident.component == f"incident/position/{position_id}"
        assert len(session.scalars(select(models.Fill)).all()) == 2  # no third fill


# --- the account -------------------------------------------------------------------------------


def test_cash_is_what_was_deposited_minus_purchases_plus_sales(engine, saved):
    with session_scope(engine) as session:
        assert account.cash_balance(session, CONFIG) == 5000.0
        position_id = confirmations.confirm_bought(
            session, saved["BUY"], price=50.10, qty=9, commission=1.31
        ).id
    with session_scope(engine) as session:
        assert account.cash_balance(session, CONFIG) == pytest.approx(5000 - 450.90 - 1.31)
        confirmations.confirm_sold(session, position_id, price=53.40, qty=4, commission=0.62)
    with session_scope(engine) as session:
        assert account.cash_balance(session, CONFIG) == pytest.approx(
            5000 - 450.90 - 1.31 + 213.60 - 0.62
        )


def test_portfolio_shows_open_real_positions_at_their_last_close(engine, saved, _fixed_time):
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["BUY"], price=50.10, qty=9)
    add_bars(engine, DAY, {"BUY": 51.0})
    _fixed_time.set(clock.market_time(NEXT_DAY, time(8, 0)))

    with session_scope(engine) as session:
        portfolio = account.load_portfolio(session, CONFIG)

    (holding,) = portfolio.holdings
    assert (holding.ticker, holding.shares, holding.entry_price) == ("BUY", 9, 50.10)
    assert (holding.last_price, holding.stop, holding.initial_stop) == (
        51.0, 48.3, pytest.approx(48.3),
    )  # fmt: skip
    assert (holding.target, holding.opened_on, holding.sector_etf) == (53.4, DAY, "S0")
    assert holding.r_now == pytest.approx(0.5)
    assert portfolio.cash == pytest.approx(5000 - 450.90)
    assert portfolio.capital == pytest.approx(5000 - 450.90 + 9 * 51.0)


def test_the_next_plan_only_spends_the_cash_that_is_left(engine, saved, _fixed_time):
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["BUY"], price=50.10, qty=90)  # $4,509
    add_bars(engine, DAY, {"BUY": 50.1})
    _fixed_time.set(clock.market_time(NEXT_DAY, time(9, 0)))
    with session_scope(engine) as session:
        portfolio = account.load_portfolio(session, CONFIG)
    candidates = [candidate(f"N{i}", 90.0 - i) for i in range(6)]
    state, indicators = state_and_indicators(candidates)

    plan = build_plan(state, indicators, portfolio, CONFIG, candidates=candidates)

    assert portfolio.cash == pytest.approx(491.0)
    assert sum(idea.amount for idea in plan.by_verdict("COMPRAR")) <= portfolio.cash
    assert len(plan.ideas) >= 5
    waiting = [i for i in plan.ideas if i.reason == "COMPRAR si liberas efectivo"]
    assert waiting  # the plan says what would be bought with more cash


def test_history_has_the_closed_trades_and_the_capital_of_each_day(engine, saved):
    with session_scope(engine) as session:
        position_id = confirmations.confirm_bought(
            session, saved["BUY"], price=50.0, qty=10, commission=1.0
        ).id
        confirmations.confirm_sold(session, position_id, price=48.3, qty=10, commission=1.0)
        session.add_all(
            models.MetricsDaily(day=DAY - timedelta(days=offset), equity_real=value)
            for offset, value in ((2, 5000.0), (1, 5040.0))
        )
    with session_scope(engine) as session:
        history = account.load_history(session)

    assert history.closed_results == (pytest.approx(-19.0),)  # -17 and 2 of commissions
    assert history.equity_curve == (
        (DAY - timedelta(days=2), 5000.0),
        (DAY - timedelta(days=1), 5040.0),
    )


def test_day_and_week_results_come_from_the_recorded_capital(engine):
    monday = DAY - timedelta(days=DAY.weekday())
    with session_scope(engine) as session:
        session.add_all(
            [
                models.MetricsDaily(day=monday - timedelta(days=3), equity_real=5200.0),
                models.MetricsDaily(day=monday, equity_real=5100.0),
            ]
        )
    with session_scope(engine) as session:
        portfolio = account.load_portfolio(session, CONFIG)

    # Capital now 5,000: -1.96% against yesterday's close, -3.85% against last week's.
    assert portfolio.day_pnl_pct == pytest.approx((5000 / 5100 - 1) * 100)
    assert portfolio.week_pnl_pct == pytest.approx((5000 / 5200 - 1) * 100)


def test_the_weekly_brake_stays_on_until_the_user_lifts_it(engine, _fixed_time):
    monday = DAY - timedelta(days=DAY.weekday())
    with session_scope(engine) as session:
        session.add(models.MetricsDaily(day=monday - timedelta(days=3), equity_real=5300.0))
        session.add(models.MetricsDaily(day=DAY - timedelta(days=1), equity_real=5000.0))

    def brake():
        with session_scope(engine) as session:
            portfolio = account.load_portfolio(session, CONFIG)
            return account.current_brake(session, CONFIG, portfolio, account.load_history(session))

    tripped = brake()  # 5,000 against 5,300: -5.7% this week
    assert (tripped.state, tripped.active) == ("SOLO_VIGILAR", ("week",))
    # The next week starts from a new reference, but the brake is still on.
    with session_scope(engine) as session:
        session.add(models.MetricsDaily(day=monday + timedelta(days=4), equity_real=5000.0))
    _fixed_time.set(AS_OF + timedelta(days=7))
    assert brake().active == ("week",)

    with session_scope(engine) as session:
        assert account.lift_brake(session, "week", capital=5000.0, note="leí el resumen") is True
        assert account.lift_brake(session, "week", capital=5000.0) is False
    assert brake().state == "NORMAL"


def test_lifting_the_weekly_brake_does_not_trip_it_again_for_the_same_loss(engine):
    monday = DAY - timedelta(days=DAY.weekday())
    with session_scope(engine) as session:
        session.add(models.MetricsDaily(day=monday - timedelta(days=3), equity_real=5300.0))
        session.add(models.MetricsDaily(day=DAY - timedelta(days=1), equity_real=5000.0))
        session.flush()
        portfolio = account.load_portfolio(session, CONFIG)
        account.current_brake(session, CONFIG, portfolio, account.load_history(session))
        account.lift_brake(session, "week", capital=5000.0)
    with session_scope(engine) as session:
        portfolio = account.load_portfolio(session, CONFIG)
        brake = account.current_brake(session, CONFIG, portfolio, account.load_history(session))

    assert portfolio.week_pnl_pct < -5 and brake.state == "NORMAL"


def test_the_pause_measures_the_fall_again_from_where_the_user_lifted_it(engine):
    with session_scope(engine) as session:
        session.add(models.MetricsDaily(day=DAY - timedelta(days=30), equity_real=5600.0))

    def brake(capital: float):
        with session_scope(engine) as session:
            history = account.load_history(session)
            return account.current_brake(session, CONFIG, Portfolio(cash=capital), history)

    assert brake(5000.0).state == "PAUSA"  # -10.7% from 5,600
    with session_scope(engine) as session:
        account.lift_brake(session, "drawdown", capital=5000.0)
    assert brake(5000.0).state == "NORMAL"
    assert brake(4600.0).state == "NORMAL"  # -8% from the 5,000 at which it was lifted
    assert brake(4500.0).state == "PAUSA"  # -10% from there


# --- shadows -----------------------------------------------------------------------------------


def settle(engine, bars, day=DAY):
    with session_scope(engine) as session:
        return confirmations.settle_day(session, CONFIG, day, bars)


def shadow_of(session, idea_id: int) -> models.Position:
    return session.scalars(
        select(models.Position).where(
            models.Position.idea_id == idea_id, models.Position.is_shadow.is_(True)
        )
    ).one()


def test_at_the_close_unanswered_purchases_become_shadows_if_the_price_reached_the_zone(
    engine, saved
):
    with session_scope(engine) as session:
        confirmations.confirm_declined(session, saved["BUY"], reason="llegué tarde")

    outcome = settle(
        engine,
        {
            "BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8),  # opened inside the zone
            "TWO": bar(DAY, 51.0, 52.0, 50.6, 51.5),  # never came back to 50.25
            "NEAR": bar(DAY, 50.0, 50.5, 49.5, 50.2),
        },
    )

    assert outcome == {
        "expired": ["TWO", "NEAR", "CAP", "LOW"],
        "shadow": ["BUY"],
        "not_fillable": ["TWO"],
        "repeated": [],
    }
    assert transitions(engine, "idea", saved["BUY"])[1:] == [
        ("PROPOSED", "DECLINED", "user", "No la compré: llegué tarde"),
        ("DECLINED", "SHADOW", "system", "sombra con entrada teórica en $50.20"),
    ]
    assert transitions(engine, "idea", saved["TWO"])[1:] == [
        ("PROPOSED", "EXPIRED", "system", "sin respuesta al cierre"),
        ("EXPIRED", "NOT_FILLABLE", "system",
         "no ejecutable: el precio no volvió a la zona de entrada"),
    ]  # fmt: skip
    # An idea that was never a purchase just expires: no shadow for it.
    assert transitions(engine, "idea", saved["NEAR"])[-1] == (
        "PROPOSED", "EXPIRED", "system", "no era una compra propuesta",
    )  # fmt: skip
    with session_scope(engine) as session:
        shadow = shadow_of(session, saved["BUY"])
        assert (shadow.state, shadow.qty_open, shadow.avg_price) == ("OPEN", 9, 50.2)
        assert (shadow.stop, shadow.initial_r) == (48.3, pytest.approx(1.9))
        (fill,) = account.fills_of(session, [shadow.id])[shadow.id]
        # A virtual fill with the modelled costs: 0.34% of 9 x 50.20.
        assert (fill.origin, fill.side, fill.price, fill.qty) == ("virtual", "buy", 50.2, 9)
        assert fill.commission == pytest.approx(451.8 * 0.0034)
        # Shadows never touch the real cash.
        assert account.cash_balance(session, CONFIG) == 5000.0
        assert len(session.scalars(select(models.Position)).all()) == 1


def test_a_purchase_the_user_confirmed_is_not_shadowed(engine, saved):
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, saved["BUY"], price=50.1, qty=9)

    outcome = settle(engine, {"BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8)})

    assert "BUY" not in outcome["shadow"] + outcome["expired"]
    assert state_of(engine, models.Idea, saved["BUY"]) == "BOUGHT"
    with session_scope(engine) as session:
        assert (
            session.scalars(
                select(models.Position).where(models.Position.is_shadow.is_(True))
            ).all()
            == []
        )


def test_a_shadow_stopped_on_its_entry_day_is_closed_at_the_stop(engine, saved):
    settle(engine, {"BUY": bar(DAY, 50.2, 50.4, 48.0, 48.2)})

    with session_scope(engine) as session:
        shadow = shadow_of(session, saved["BUY"])
        fills = account.fills_of(session, [shadow.id])[shadow.id]
        assert (shadow.state, shadow.qty_open) == ("CLOSED", 0)
        assert [(f.side, f.price, f.qty) for f in fills] == [("buy", 50.2, 9), ("sell", 48.3, 9)]
        assert shadow.tracking["exit_reason"] == exits.STOP


def test_shadows_are_followed_with_the_exit_rules_day_by_day(engine, saved):
    settle(engine, {"BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8)})
    day_2, day_3 = NEXT_DAY, NEXT_DAY + timedelta(days=1)

    with session_scope(engine) as session:
        first = confirmations.update_shadows(
            session, CONFIG, day_2, {"BUY": bar(day_2, 51.0, 53.6, 50.9, 53.5)}
        )
    with session_scope(engine) as session:
        shadow = shadow_of(session, saved["BUY"])
        # First target: half (rounded down) is sold and the stop moves up to the entry
        # price; the high of 53.60 is +1.8R, so the trailing stop takes over: 53.60 - 2.5 ATR.
        assert first == {"BUY": [exits.TARGET_1]}
        assert (shadow.state, shadow.qty_open) == ("PARTIALLY_CLOSED", 5)
        assert shadow.stop == pytest.approx(51.1)
        assert shadow.tracking["target_1_done"] is True and shadow.tracking["days_held"] == 1
        second = confirmations.update_shadows(
            session, CONFIG, day_3, {"BUY": bar(day_3, 54.0, 55.3, 53.8, 55.2)}
        )
    with session_scope(engine) as session:
        shadow = shadow_of(session, saved["BUY"])
        fills = account.fills_of(session, [shadow.id])[shadow.id]
        assert second == {"BUY": [exits.TARGET_2]}
        assert (shadow.state, shadow.qty_open) == ("CLOSED", 0)
        assert [(f.side, f.price, f.qty, f.origin) for f in fills] == [
            ("buy", 50.2, 9, "virtual"), ("sell", 53.4, 4, "virtual"), ("sell", 55.1, 5, "virtual"),
        ]  # fmt: skip
        # Net result of the shadow, costs included: the same arithmetic as the replay.
        assert account.net_result(fills) == pytest.approx(
            4 * 53.4 + 5 * 55.1 - 9 * 50.2 - (451.8 + 213.6 + 275.5) * 0.0034
        )
    assert transitions(engine, "position", shadow.id) == [
        (None, "OPEN", "system", "sombra: entrada teórica"),
        ("OPEN", "PARTIALLY_CLOSED", "system", exits.TARGET_1),
        ("PARTIALLY_CLOSED", "CLOSED", "system", exits.TARGET_2),
    ]


def test_a_ticker_with_an_open_shadow_does_not_get_a_second_one(engine, saved, _fixed_time):
    settle(engine, {"BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8)})
    # The next day the plan proposes BUY again and nobody answers.
    _fixed_time.set(AS_OF + timedelta(days=1))
    tomorrow = replace(
        plan_for([candidate("BUY", 85.0)] + MIXED[2:]),
        plan_date=NEXT_DAY,
        as_of=AS_OF + timedelta(days=1),
    )
    with session_scope(engine) as session:
        plan_store.save_plan(session, tomorrow)

    outcome = settle(engine, {"BUY": bar(NEXT_DAY, 50.1, 50.9, 49.8, 50.5)}, day=NEXT_DAY)

    assert (outcome["shadow"], outcome["repeated"]) == ([], ["BUY"])
    with session_scope(engine) as session:
        (again,) = [i for i in plan_store.todays_ideas(session, NEXT_DAY) if i.ticker == "BUY"]
        assert again.state == "NOT_FILLABLE"
        assert len(session.scalars(select(models.Position)).all()) == 1
    assert transitions(engine, "idea", again.id)[-1][3] == (
        "no se abre otra sombra: ya hay una abierta de BUY"
    )


def test_a_shadow_is_sold_before_an_earnings_report(engine, saved):
    settle(engine, {"BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8)})

    with session_scope(engine) as session:
        activity = confirmations.update_shadows(
            session, CONFIG, NEXT_DAY, {"BUY": bar(NEXT_DAY, 50.9, 51.4, 50.5, 51.0)},
            reports_before_next_open=lambda ticker: ticker == "BUY",
        )  # fmt: skip

    assert activity == {"BUY": [exits.EARNINGS]}


def test_a_late_yes_on_a_shadowed_idea_cancels_the_shadow(engine, saved, _fixed_time):
    settle(engine, {"BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8)})
    _fixed_time.advance(timedelta(days=1))

    with session_scope(engine) as session:
        confirmations.confirm_bought(
            session, saved["BUY"], price=50.3, qty=9, filled_at=AS_OF + timedelta(hours=2)
        )

    assert transitions(engine, "idea", saved["BUY"])[-1] == (
        "SHADOW", "BOUGHT", "user", "corrección: La compré",
    )  # fmt: skip
    with session_scope(engine) as session:
        shadow = shadow_of(session, saved["BUY"])
        assert shadow.state == "CLOSED" and account.is_voided(shadow)
        (real,) = account.real_positions(session)
        assert (real.avg_price, real.qty_open, real.is_shadow) == (50.3, 9, False)


# --- endpoints ---------------------------------------------------------------------------------


@pytest.fixture
def client(saved, db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    with TestClient(create_app()) as test_client:
        yield test_client


def test_plan_endpoint_serves_the_saved_plan(client):
    assert client.get("/api/plan", params={"day": "2026-11-09"}).status_code == 404

    body = client.get("/api/plan", params={"day": DAY.isoformat()}).json()

    assert body["plan_date"] == "2026-11-10" and body["regime"] == "ALCISTA"
    assert len(body["ideas"]) >= 5
    assert [i["verdict"] for i in body["ideas"]][:3] == ["COMPRAR", "COMPRAR", "VIGILAR"]


def test_bought_and_sold_through_the_api(client, saved):
    bought = client.post(
        f"/api/ideas/{saved['BUY']}/bought", json={"price": 50.10, "qty": 9, "commission": 1.31}
    )
    position = bought.json()["position"]

    assert bought.status_code == 200
    assert (position["state"], position["qty_open"], position["is_shadow"]) == ("OPEN", 9, False)
    assert position["initial_r"] == pytest.approx(1.80)
    listed = client.get("/api/positions").json()["positions"]
    assert [p["ticker"] for p in listed] == ["BUY"]
    summary = client.get("/api/account").json()
    assert summary["cash_usd"] == pytest.approx(5000 - 450.90 - 1.31)
    assert summary["open_positions"] == 1 and summary["deposited_usd"] == 5000
    assert summary["ai_budget"] == {"used": 0, "limit": 40, "state": "OK"}

    sold = client.post(
        f"/api/positions/{position['id']}/sold",
        json={"price": 53.4, "qty": 9, "reason": "objetivo"},
    )
    assert sold.json()["position"]["state"] == "CLOSED"
    assert client.get("/api/positions").json()["positions"] == []
    history = client.get(f"/api/ideas/{saved['BUY']}/history").json()
    assert [t["to"] for t in history["transitions"]] == ["PROPOSED", "BOUGHT"]
    assert [c["kind"] for c in history["confirmations"]] == ["bought", "sold"]


def test_declined_through_the_api(client, saved):
    response = client.post(f"/api/ideas/{saved['TWO']}/declined", json={"reason": "sin efectivo"})

    assert response.status_code == 200
    assert response.json()["idea"]["state"] == "DECLINED"


@pytest.mark.parametrize(
    ("body", "status"),
    [({"price": -1, "qty": 9}, 422), ({"qty": 9}, 422), ({"price": 48.0, "qty": 9}, 422)],
)
def test_a_bad_purchase_is_rejected_with_an_explanation(client, saved, body, status):
    response = client.post(f"/api/ideas/{saved['BUY']}/bought", json=body)

    assert response.status_code == status
    assert client.get("/api/positions").json()["positions"] == []


def test_unknown_idea_or_position_is_a_404(client):
    assert client.post("/api/ideas/999/bought", json={"price": 50, "qty": 1}).status_code == 404
    assert client.post("/api/positions/999/sold", json={"price": 50, "qty": 1}).status_code == 404
    assert client.get("/api/ideas/999/history").status_code == 404
    assert client.post("/api/brakes/week/lift").status_code == 404


def test_an_invalid_transition_through_the_api_puts_the_system_in_caution(client, saved, engine):
    assert client.get("/api/status").json()["state"] in ("NORMAL", "PRECAUCION")
    before = client.get("/api/status").json()["incidents"]
    settle(engine, {"BUY": bar(DAY, 50.2, 51.0, 49.9, 50.8)})  # BUY is now a shadow

    response = client.post(f"/api/ideas/{saved['BUY']}/declined", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["system_state"] == "PRECAUCION"
    assert body["detail"] == (
        f"transición inválida: la idea {saved['BUY']} está en SHADOW y no puede pasar a DECLINED"
    )
    status = client.get("/api/status").json()
    assert status["state"] == "PRECAUCION"
    assert len(status["incidents"]) == len(before) + 1
    assert status["incidents"][-1]["component"] == body["incident"]
    assert body["detail"] in status["problems"]
    assert state_of(engine, models.Idea, saved["BUY"]) == "SHADOW"  # nothing was applied

    resolved = client.post(
        "/api/status/incidents/resolve", json={"component": body["incident"], "note": "visto"}
    )
    assert resolved.status_code == 200
    assert client.get("/api/status").json()["incidents"] == []
    assert (
        client.post(
            "/api/status/incidents/resolve", json={"component": body["incident"]}
        ).status_code
        == 404
    )
