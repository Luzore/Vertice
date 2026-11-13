"""The daily runs on a synthetic market: premarket plan, recheck after the open and close."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time, timedelta

import pytest
from sqlalchemy import select

from core import clock, market_calendar, settings
from db import models
from db.session import session_scope
from engine import account, daily, plan_store
from engine.ai_analyst import simulated_analyst
from engine.indicators import Levels
from engine.risk_profile import profile_for
from engine.screener import Candidate
from tests.conftest import SHIPPED_CONFIG
from tests.test_screener_plan import STRONG

CONFIG = settings.load_config(SHIPPED_CONFIG)
DAY = date(2026, 11, 11)  # a Wednesday with two more sessions after it in the data
PUBLISH = clock.market_time(DAY, time(9, 0))


@pytest.fixture(autouse=True)
def _at_publication_time():
    with clock.simulated(PUBLISH) as simulated:
        yield simulated


@pytest.fixture
def busy(market, monkeypatch):
    """The synthetic market with eight breakout candidates built on its real prices,
    so the daily runs have purchases, rechecks and shadows to work with (a random
    walk on its own never passes the 22 controls)."""
    real_indicators = daily.compute_indicators

    def indicators_with_levels(state, regime_config):
        result = real_indicators(state, regime_config)
        levels = {
            ticker: Levels(supports=(round(float(result.metrics.loc[ticker, "close"]) * 0.968, 2),))
            for ticker in list(result.metrics.index)[:8]
        }
        return replace(result, levels={**result.levels, **levels}, regime="ALCISTA")

    def breakouts(indicators, screener_config):
        candidates = []
        for rank, ticker in enumerate(list(indicators.metrics.index)[:8]):
            close = float(indicators.metrics.loc[ticker, "close"])
            metrics = {
                **STRONG, "close": close, "atr_14": close * 0.02, "atr_pct": 2.0,
                "sma_20": close * 0.992, "ema_21": close * 0.992, "sma_50": close * 0.92,
                "sma_200": close * 0.8, "prior_close_max_20d": close * 0.99, "extension_atr": 0.4,
            }  # fmt: skip
            candidates.append(
                Candidate(ticker, "RUPTURA", 90.0 - rank, {"tendencia": 25.0},
                          ["rompe máximo de 20 días"], metrics)
            )  # fmt: skip
        return candidates

    monkeypatch.setattr(daily, "compute_indicators", indicators_with_levels)
    monkeypatch.setattr(daily, "screen", breakouts)
    return market


def saved_ideas(engine, day: date = DAY) -> list[models.Idea]:
    with session_scope(engine) as session:
        return plan_store.todays_ideas(session, day)


def test_premarket_builds_and_saves_the_plan_of_the_day(market):
    run_id, plan = daily.run_premarket(market, CONFIG)

    ideas = saved_ideas(market)
    assert plan.plan_date == DAY and len(plan.ideas) >= 5
    assert [i.ticker for i in ideas] == [i.ticker for i in plan.ideas]
    assert {i.run_id for i in ideas} == {run_id}
    assert {i.state for i in ideas} == {"PROPOSED"}
    with session_scope(market) as session:
        run = session.get(models.Run, run_id)
        assert (run.kind, run.as_of, run.regime) == ("premarket", PUBLISH, plan.regime)
        assert run.stats["plan_date"] == "2026-11-11"
        data = plan_store.plan_as_dict(session, DAY)
    assert len(data["ideas"]) >= 5
    # With 5,000 in cash the suggested amounts never add up to more than that, and no
    # size breaks the limits of the aggressiveness profile.
    profile = profile_for(CONFIG.system.aggressiveness)
    buys = [i for i in ideas if i.verdict == "COMPRAR"]
    assert sum(i.amount_usd for i in buys) <= 5000.0
    assert len(buys) <= profile.max_positions
    assert all(i.amount_usd <= profile.max_position_pct / 100 * 5000 + 1e-6 for i in buys)
    assert all(i.risk_usd <= profile.risk_pct / 100 * 5000 * 1.25 + 1e-6 for i in buys)
    # Everything that is not a purchase says why.
    assert all(i.reason for i in ideas if i.verdict != "COMPRAR")


def test_premarket_with_purchases_never_spends_more_than_the_cash(busy):
    _, plan = daily.run_premarket(busy, CONFIG)

    ideas = saved_ideas(busy)
    profile = profile_for(CONFIG.system.aggressiveness)
    buys = [i for i in ideas if i.verdict == "COMPRAR"]
    assert len(buys) == profile.max_positions == 5 and len(ideas) == 8
    assert 0 < sum(i.amount_usd for i in buys) <= 5000.0
    for idea in buys:
        assert idea.shares >= 1 and idea.amount_usd <= 0.25 * 5000
        assert idea.risk_usd <= 0.0075 * 5000 * 1.25
        assert idea.ev_r >= 0.15
    # Purchases first, by expectancy; the rest wait for room.
    assert [i.verdict for i in ideas] == ["COMPRAR"] * 5 + ["VIGILAR"] * 3
    assert {i.reason for i in ideas[5:]} == {"sin espacio: el límite es de 5 posiciones"}


def test_premarket_with_the_ai_records_the_review_and_refines_the_news(market):
    analyst = simulated_analyst(CONFIG)

    _, plan = daily.run_premarket(market, CONFIG, analyst=analyst)

    assert plan.review is not None
    with session_scope(market) as session:
        data = plan_store.plan_as_dict(session, DAY)
        refined = session.scalars(
            select(models.News).where(models.News.classified_by == "ai")
        ).all()
    assert data["ai"]["status"] == plan.review.status
    # Only news of the last 72 hours about candidates are sent to the AI, once.
    since = PUBLISH - timedelta(hours=CONFIG.controls.severe_news_hours)
    assert all(since < row.published_at <= PUBLISH for row in refined)
    calls_before = analyst.gateway.calls_made
    daily.run_premarket(market, CONFIG, analyst=analyst)
    news_calls = [c for c in analyst.gateway._ledger.records if c.task == "news_classifier"]  # noqa: SLF001
    assert len([c for c in news_calls if c.status == "ok"]) <= calls_before


def test_no_plan_on_a_day_without_market(market, _at_publication_time):
    _at_publication_time.set(clock.market_time(date(2026, 11, 14), time(9, 0)))  # a Saturday

    with pytest.raises(daily.NoMarket, match="NYSE no abre el 2026-11-14"):
        daily.run_premarket(market, CONFIG)

    assert saved_ideas(market, date(2026, 11, 14)) == []


def test_premarket_respects_the_real_account(market):
    # A week that is going badly: 5,000 today against 5,400 at last week's close.
    monday = DAY - timedelta(days=DAY.weekday())
    with session_scope(market) as session:
        session.add(models.MetricsDaily(day=monday - timedelta(days=3), equity_real=5400.0))
        session.add(models.MetricsDaily(day=DAY - timedelta(days=1), equity_real=5000.0))

    _, plan = daily.run_premarket(market, CONFIG)

    assert plan.brake.state == "SOLO_VIGILAR" and plan.brake.active == ("week",)
    assert plan.by_verdict("COMPRAR") == []
    assert len(plan.ideas) >= 5
    with session_scope(market) as session:
        assert account.latched_brakes(session) == ("week",)
        assert plan_store.plan_as_dict(session, DAY)["brake"]["state"] == "SOLO_VIGILAR"


def test_recheck_uses_the_opening_prices_of_the_day(busy):
    market = busy
    daily.run_premarket(market, CONFIG)
    buys = [i for i in saved_ideas(market) if i.verdict == "COMPRAR"]

    changed = daily.run_recheck(market, CONFIG, day=DAY)
    forced = daily.run_recheck(
        market, CONFIG, day=DAY, opening_prices={i.ticker: i.no_chase_above + 0.01 for i in buys}
    )

    after = {i.ticker: i for i in saved_ideas(market)}
    assert len(buys) == 5 and len(changed) == 1  # one of them opened above its no-chase price
    for ticker, verdict, reason in changed:
        assert verdict == "VIGILAR" and after[ticker].verdict == verdict
        assert "arriba del precio de no perseguir" in reason
    # Whatever was still a purchase and opens a cent above its no-chase price is demoted.
    still = {i.ticker for i in buys} - {ticker for ticker, _, _ in changed}
    assert {ticker for ticker, _, _ in forced} == still
    assert all(after[ticker].verdict != "COMPRAR" for ticker in still)


def test_close_settles_the_day_and_records_the_capital(busy, _at_publication_time):
    market = busy
    daily.run_premarket(market, CONFIG)
    buys = [i.ticker for i in saved_ideas(market) if i.verdict == "COMPRAR"]
    _at_publication_time.set(clock.market_time(DAY, time(16, 15)))

    summary = daily.run_close(market, CONFIG, day=DAY)

    assert summary.note == "" and summary.day == DAY
    ideas = saved_ideas(market)
    assert "PROPOSED" not in {i.state for i in ideas}
    # Every purchase idea nobody answered ended as a shadow or as not fillable.
    assert len(buys) == 5 and summary.ideas["shadow"]
    assert sorted(summary.ideas["shadow"] + summary.ideas["not_fillable"]) == sorted(buys)
    assert {i.state for i in ideas if i.ticker in buys} <= {"SHADOW", "NOT_FILLABLE"}
    assert {i.state for i in ideas if i.ticker not in buys} <= {"EXPIRED"}
    with session_scope(market) as session:
        metrics = session.get(models.MetricsDaily, DAY)
        spy = session.scalar(
            select(models.BarDaily.close).where(
                models.BarDaily.ticker == "SPY", models.BarDaily.session_date == DAY
            )
        )
        shadows = session.scalars(
            select(models.Position).where(models.Position.is_shadow.is_(True))
        ).all()
        # No real purchase was confirmed: the real capital is untouched by the shadows.
        assert (metrics.equity_real, metrics.spy_close, metrics.drawdown) == (5000.0, spy, 0.0)
        assert sorted(p.ticker for p in shadows) == sorted(summary.ideas["shadow"])
        assert account.real_positions(session) == []
    assert summary.equity == 5000.0


def test_close_follows_the_shadows_on_the_following_days(busy, _at_publication_time):
    market = busy
    days = market_calendar.trading_days_between(date(2026, 11, 2), date(2026, 11, 13))
    sales: list[str] = []
    for day in days:
        _at_publication_time.set(clock.market_time(day, time(9, 0)))
        daily.run_premarket(market, CONFIG)
        daily.run_recheck(market, CONFIG, day=day)
        _at_publication_time.set(clock.market_time(day, time(16, 15)))
        summary = daily.run_close(market, CONFIG, day=day)
        sales += [reason for reasons in summary.shadow_sales.values() for reason in reasons]
        with session_scope(market) as session:
            open_shadows = session.scalars(
                select(models.Position.ticker).where(
                    models.Position.is_shadow.is_(True), models.Position.state != "CLOSED"
                )
            ).all()
            # Never two open shadows of the same ticker.
            assert len(open_shadows) == len(set(open_shadows))

    # In ten days the exit rules did their work on the shadows.
    assert {"saltó el stop", "alcanzó el objetivo 1"} <= set(sales)
    with session_scope(market) as session:
        shadows = session.scalars(
            select(models.Position).where(models.Position.is_shadow.is_(True))
        ).all()
        fills = account.fills_of(session, (p.id for p in shadows))
        metrics = session.scalars(select(models.MetricsDaily)).all()
        for position in shadows:
            own = fills[position.id]
            bought = sum(f.qty for f in own if f.side == "buy")
            sold = sum(f.qty for f in own if f.side == "sell")
            # What is open is what was bought minus what the exit rules sold.
            assert position.qty_open == pytest.approx(bought - sold)
            assert (position.state == "CLOSED") == (position.qty_open == 0)
            assert position.stop >= position.avg_price - position.initial_r - 1e-9  # never down
            assert all(f.origin == "virtual" for f in own)
        assert len([p for p in shadows if p.state == "CLOSED"]) >= 3
        assert len([p for p in shadows if p.state != "CLOSED"]) >= 1
        assert [m.day for m in sorted(metrics, key=lambda m: m.day)] == days
        assert {m.equity_real for m in metrics} == {5000.0}
        # No idea of a settled day is left waiting for an answer.
        waiting = session.scalars(select(models.Idea).where(models.Idea.state == "PROPOSED")).all()
        assert waiting == []


def test_close_without_the_bars_of_the_day_says_so_and_changes_nothing(market):
    daily.run_premarket(market, CONFIG)
    with session_scope(market) as session:
        session.query(models.BarDaily).filter(models.BarDaily.session_date == DAY).delete()

    summary = daily.run_close(market, CONFIG, day=DAY)

    assert summary.note == "faltan las barras del 2026-11-11: descarga los datos y repite el cierre"
    assert {i.state for i in saved_ideas(market)} == {"PROPOSED"}
    with session_scope(market) as session:
        assert session.get(models.MetricsDaily, DAY) is None


def test_daily_commands(busy, db_url, monkeypatch, capsys, _at_publication_time):
    monkeypatch.setenv("DATABASE_URL", db_url)

    assert daily.main(["premarket", "--ai", "simulated"]) == 0
    printed = capsys.readouterr().out
    assert "Plan del día · mié 11 nov 2026" in printed

    assert daily.main(["recheck", "--date", "2026-11-11"]) == 0
    assert capsys.readouterr().out.strip()

    _at_publication_time.set(clock.market_time(DAY, time(16, 15)))
    assert daily.main(["close", "--date", "2026-11-11"]) == 0
    printed = capsys.readouterr().out
    assert "Cierre del 2026-11-11: capital $5,000.00" in printed
    assert "Sombras nuevas:" in printed and "No ejecutables:" in printed

    # The plan of a day that already has answers cannot be replaced.
    _at_publication_time.set(PUBLISH)
    assert daily.main(["premarket", "--ai", "none"]) == 1
    assert "ya tiene confirmaciones y no se puede reemplazar" in capsys.readouterr().out
