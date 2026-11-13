"""Dossier, valid levels, validation of the AI's answer and the plan with the AI."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from core import clock
from db import models
from db.session import session_scope
from engine.ai_analyst import Analyst, AnalystReview
from engine.ai_client import TASK_ANALYST, SimulatedAI
from engine.ai_gateway import AIGateway, MemoryLedger
from engine.ai_validation import NO_ANSWER, validate_decisions
from engine.dossier import (
    Finalist,
    LessonNote,
    SetupStats,
    build_premarket_dossier,
    extract_json_blocks,
    load_active_lessons,
)
from engine.indicators import Levels
from engine.levels import valid_levels
from engine.news_ai import NewsClassification, classify_news, refine
from engine.portfolio import Holding, Portfolio
from engine.recommender import build_plan, render_plan
from engine.risk_profile import profile_for
from engine.schemas import CorporateEvent, EconEvent, NewsItem
from engine.trade_plan import build_trade_plan
from tests.conftest import REPO_ROOT
from tests.test_screener_plan import AS_OF, CONFIG, DAY, candidate, state_and_indicators

PROFILE = profile_for(CONFIG.system.aggressiveness)
STOP_RANGE = (CONFIG.controls.stop_min_atr, CONFIG.controls.stop_max_atr)


@pytest.fixture(autouse=True)
def _fixed_time():
    with clock.simulated(AS_OF) as simulated:
        yield simulated


def gateway(client, *, ledger=None, **ai_changes) -> AIGateway:
    return AIGateway(
        client,
        CONFIG.ai.model_copy(update=ai_changes),
        ledger if ledger is not None else MemoryLedger(),
        prompts_dir=REPO_ROOT / "prompts",
        schemas_dir=REPO_ROOT / "schemas",
    )


def analyst(client=None, **kwargs) -> Analyst:
    return Analyst(gateway(client or SimulatedAI()), **kwargs)


def finalist(ticker: str = "ABC", score: float = 82.0, **metric_changes) -> Finalist:
    """The test candidate: close 50, ATR 1, a support at 48.40 and no resistance."""
    cand = candidate(ticker, score, **metric_changes)
    levels = Levels(supports=(48.4,))
    plan = build_trade_plan(cand.metrics, levels, PROFILE)
    return Finalist(
        candidate=cand,
        name=f"{ticker} Corp.",
        sector_etf="XLK",
        plan=plan,
        valid=valid_levels(cand.metrics, levels, CONFIG.controls, plan),
    )


def answer(ticker: str = "ABC", **changes) -> dict:
    """A well-formed decision that uses the rule-based plan of the test candidate."""
    decision = {
        "ticker": ticker,
        "veredicto": "COMPRAR",
        "setup": "RUPTURA",
        "prob_objetivo_antes_que_stop": 0.58,
        "confianza": 0.6,
        "entrada": {
            "tipo": "limite",
            "zona_min": 49.75,
            "zona_max": 50.25,
            "no_perseguir_sobre": 50.75,
        },
        "disparador_vigilar": None,
        "stop": 48.3,
        "objetivos": [53.4, 55.1],
        "horizonte_dias": 5,
        "tesis": "Ruptura con volumen en tendencia alcista.",
        "razones": ["cierra en máximo de 20 días"],
        "riesgos": ["el mercado puede girar"],
        "invalidacion": "cierre debajo de 48.30",
        "lecciones_aplicadas": [],
        "datos_faltantes": [],
    }
    entry = changes.pop("entrada", None)
    decision.update(changes)
    if entry:
        decision["entrada"] = {**decision["entrada"], **entry}
    return decision


def validated(*decisions: dict, finalists=None, **kwargs):
    return validate_decisions(
        {"lectura_mercado": "x", "decisiones": list(decisions)},
        finalists or [finalist()],
        stop_range_atr=STOP_RANGE,
        **kwargs,
    )


# --- valid levels ------------------------------------------------------------------------------


def test_valid_stops_are_technical_levels_at_the_distance_the_controls_accept():
    valid = finalist().valid

    # Under the support of 48.40 (1.7 ATR). The 20-day average is too close (0.5 ATR)
    # and the 50-day one too far (3.1 ATR): neither is offered.
    assert [(s.price, s.name, s.anchor, s.anchor_name) for s in valid.stops] == [
        (48.3, "bajo el soporte de 48.40", 48.4, "soporte")
    ]
    assert (valid.reference, valid.atr) == (50.0, 1.0)


def test_valid_entries_stay_within_one_atr_of_the_close():
    valid = finalist().valid
    prices = [level.price for level in valid.entries]

    assert min(prices) == 49.0 and max(prices) == 51.0
    assert {49.75, 50.0, 50.25, 50.75} <= set(prices)
    assert 49.6 in prices  # the 20-day average
    assert 48.4 not in prices  # the support is 1.6 ATR away


def test_valid_targets_are_resistances_atr_multiples_and_r_multiples():
    cand = candidate("ABC", 82.0)
    levels = Levels(supports=(48.4,), resistances=(49.0, 52.2, 57.9))
    valid = valid_levels(cand.metrics, levels, CONFIG.controls)

    names = {level.price: level.name for level in valid.targets}
    assert names[52.2] == "resistencia" and names[57.9] == "resistencia"
    assert 49.0 not in names  # below the price: not a target
    assert names[53.0] == "3 ATR sobre el cierre"
    by_r = {level.price: level.name for level in valid.targets_for(48.3)}
    assert by_r[53.4] == "2R sobre el cierre" and by_r[55.1] == "3R sobre el cierre"


def test_no_valid_levels_without_price_and_atr():
    assert valid_levels({"close": 50.0}, Levels(), CONFIG.controls) is None
    assert valid_levels({"close": 50.0, "atr_14": 0.0}, Levels(), CONFIG.controls) is None


# --- validation of the AI's answer -------------------------------------------------------------


def test_an_answer_that_uses_valid_levels_is_kept_as_it_is():
    decisions, ignored = validated(answer())
    decision = decisions["ABC"]

    assert ignored == ()
    assert decision.verdict == "COMPRAR" and decision.corrections == ()
    assert (decision.probability, decision.confidence, decision.horizon_days) == (0.58, 0.6, 5)
    plan = decision.plan
    assert (plan.entry_min, plan.entry_max, plan.no_chase_above) == (49.75, 50.25, 50.75)
    assert (plan.stop, plan.stop_level, plan.stop_level_name) == (48.3, 48.4, "soporte")
    assert plan.targets == (53.4, 55.1)
    assert plan == finalist().plan


def test_a_stop_within_half_a_percent_of_a_valid_level_is_accepted():
    decision = validated(answer(stop=48.1))[0]["ABC"]  # 0.41% from 48.30

    assert decision.plan.stop == 48.1
    assert decision.corrections == ()


def test_an_invented_stop_that_is_a_near_miss_is_corrected_to_the_nearest_valid_level():
    decision = validated(answer(stop=47.6))[0]["ABC"]  # 1.4% and 0.7 ATR from 48.30

    assert decision.verdict == "COMPRAR"
    assert decision.plan.stop == 48.3
    assert decision.corrections == (
        "el stop 47.60 no coincide con un nivel válido; se corrigió a 48.30 "
        "(bajo el soporte de 48.40)",
    )


@pytest.mark.parametrize("stop", [45.0, 49.5, 51.0, 12.34])
def test_an_invented_stop_far_from_every_valid_level_discards_the_idea(stop):
    decision = validated(answer(stop=stop))[0]["ABC"]

    assert decision.verdict == "DESCARTAR"
    assert decision.plan is None
    assert decision.discard_reason.startswith(
        f"la IA propuso niveles que no se pudieron validar: el stop {stop:,.2f} no sale de "
        "los niveles válidos"
    )


def test_invented_targets_are_corrected_or_discard_the_idea():
    near = validated(answer(objetivos=[53.4, 57.5]))[0]["ABC"]  # 0.7 from the 4R target
    far = validated(answer(objetivos=[53.4, 70.0]))[0]["ABC"]

    assert near.plan.targets == (53.4, 56.8)
    assert near.corrections == (
        "el segundo objetivo 57.50 no coincide con un nivel válido; se corrigió a 56.80 "
        "(4R sobre el cierre)",
    )
    assert far.verdict == "DESCARTAR"
    assert "el segundo objetivo 70.00 no sale de los niveles válidos" in far.discard_reason


def test_an_entry_zone_out_of_order_is_replaced_by_the_rule_plan():
    decision = validated(answer(entrada={"zona_min": 50.5, "zona_max": 49.75}))[0]["ABC"]

    plan = decision.plan
    assert (plan.entry_min, plan.entry_max, plan.no_chase_above) == (49.75, 50.25, 50.75)
    assert "zona de entrada incoherente" in decision.corrections[0]


def test_a_second_target_that_does_not_beat_the_first_is_moved_up():
    decision = validated(answer(objetivos=[53.4, 53.0]))[0]["ABC"]

    assert decision.plan.targets == (53.4, 54.0)
    assert "el segundo objetivo 53.00 no supera al primero" in decision.corrections[0]


def test_a_target_inside_the_entry_zone_discards_the_idea():
    decision = validated(
        answer(objetivos=[51.0, 53.4], entrada={"zona_max": 50.75, "no_perseguir_sobre": 51.0})
    )[0]["ABC"]

    assert decision.verdict == "DESCARTAR"
    assert "el primer objetivo 51.00 no queda arriba del precio de no perseguir" in (
        decision.discard_reason
    )


def test_only_tickers_of_the_dossier_count_and_the_first_answer_stands():
    decisions, ignored = validated(
        answer("ABC", veredicto="VIGILAR", disparador_vigilar="cierre arriba de 50.25"),
        answer("TSLA"),
        answer(" abc ", veredicto="COMPRAR"),
    )

    assert set(decisions) == {"ABC"}
    assert ignored == ("TSLA",)
    assert decisions["ABC"].verdict == "VIGILAR"
    assert decisions["ABC"].watch_trigger == "cierre arriba de 50.25"


def test_a_finalist_the_ai_did_not_answer_for_is_discarded():
    decisions, _ = validated(answer("ABC"), finalists=[finalist("ABC"), finalist("XYZ")])

    missing = decisions["XYZ"]
    assert (missing.verdict, missing.discard_reason, missing.plan) == ("DESCARTAR", NO_ANSWER, None)
    assert NO_ANSWER == "sin respuesta de la IA"


def test_the_ai_own_discard_keeps_its_reasons_and_needs_no_levels():
    decision = validated(
        answer(veredicto="DESCARTAR", stop=1.0, riesgos=["resultados en dos días"])
    )[0]["ABC"]

    assert decision.verdict == "DESCARTAR" and decision.discard_reason == ""
    assert decision.risks == ("resultados en dos días",)


@pytest.mark.parametrize("field", ["prob_objetivo_antes_que_stop", "confianza"])
@pytest.mark.parametrize("value", [1.2, -0.1])
def test_a_probability_outside_zero_to_one_discards_the_idea(field, value):
    decision = validated(answer(**{field: value}))[0]["ABC"]

    assert decision.verdict == "DESCARTAR"
    assert decision.discard_reason == "la IA dio una probabilidad fuera de 0 a 1"


def test_lessons_that_are_not_in_force_are_ignored():
    decision = validated(answer(lecciones_aplicadas=["L-012", "L-999"]), lesson_ids={"L-012"})[0][
        "ABC"
    ]

    assert decision.lessons_applied == ("L-012",)
    assert decision.corrections == ("lecciones que no están en vigor, ignoradas: L-999",)


# --- dossier -----------------------------------------------------------------------------------


def news(headline: str, hours_ago: float, **fields) -> NewsItem:
    return NewsItem(
        tickers=("T0",),
        published_at=AS_OF - timedelta(hours=hours_ago),
        source="alpaca",
        headline=headline,
        **fields,
    )


def dossier_for(candidates=None, *, portfolio=None, lessons=(), history=None, **state_changes):
    candidates = candidates or [candidate("T0", 85.0), candidate("T1", 78.0, setup="PULLBACK")]
    state, indicators = state_and_indicators(candidates, sectors={"T0": "XLK", "T1": "XLF"})
    state = replace(state, **state_changes)
    captured = {}

    class Capture(Analyst):
        def review(self, state, indicators, portfolio, profile, config, finalists):
            captured["finalists"] = finalists
            captured["dossier"] = build_premarket_dossier(
                state, indicators, portfolio, profile, config, finalists,
                lessons=lessons, setup_history=history,
            )  # fmt: skip
            return AnalystReview("disabled")

    build_plan(
        state, indicators, portfolio or Portfolio(cash=5000.0), CONFIG,
        candidates=candidates, analyst=Capture(gateway(SimulatedAI())),
    )  # fmt: skip
    return captured["dossier"], captured["finalists"]


def test_dossier_has_the_five_sections_and_both_time_zones():
    dossier, _ = dossier_for()

    assert dossier.text.startswith("# Dossier premercado · 2026-11-10\n")
    assert "Hora: 09:00 Nueva York · 08:00 Monterrey" in dossier.text
    for title in (
        "## 1. Mercado y régimen",
        "## 2. Eventos macro (hoy y próximos 3 días)",
        "## 3. Cartera",
        "## 4. Lecciones en vigor",
        "## 5. Candidatos (2)",
        "### T0 · RUPTURA · puntaje 85",
        "### T1 · PULLBACK · puntaje 78",
    ):
        assert title in dossier.text
    assert dossier.tickers == ("T0", "T1")
    market = extract_json_blocks(dossier.text)[0]
    assert market["regimen"] == "ALCISTA"
    assert market["motivos_del_regimen"] == ["SPY arriba de sus promedios"]
    assert market["mercado"] == {"vix": 15.2, "pct_acciones_sobre_promedio_50d": 62.0}
    assert market["exigencia"] == {
        "puntaje_minimo": 70.0, "probabilidad_minima": 0.5, "modo": "recommend",
    }  # fmt: skip


def test_dossier_candidate_has_score_metrics_levels_and_the_rule_plan():
    dossier, _ = dossier_for()
    block = next(b for b in extract_json_blocks(dossier.text) if b.get("ticker") == "T0")

    assert (block["empresa"], block["sector_etf"], block["setup"]) == ("T0 Corp.", "XLK", "RUPTURA")
    assert block["puntaje"] == 85.0 and block["desglose_del_puntaje"] == {"tendencia": 25.0}
    assert block["motivos_del_setup"] == ["rompe máximo de 20 días"]
    metrics = block["metricas"]
    assert len(metrics) >= 15
    assert (metrics["ultimo_cierre"], metrics["atr_14"], metrics["rsi_2"]) == (50.0, 1.0, 70.0)
    assert metrics["volumen_relativo_20d"] == 1.8
    assert block["niveles_validos"]["stops"] == [
        {"precio": 48.3, "nivel": "bajo el soporte de 48.40"}
    ]
    assert {"precio": 50.0, "nivel": "último cierre"} in block["niveles_validos"]["entrada"]
    assert "veces R" in block["niveles_validos"]["objetivos_por_r"]
    assert block["plan_de_reglas"] == {
        "zona_min": 49.75, "zona_max": 50.25, "no_perseguir_sobre": 50.75, "stop": 48.3,
        "nivel_del_stop": "soporte", "stop_en_atr": 1.7, "objetivos": [53.4, 55.1],
        "recompensa_riesgo_al_objetivo_1": 2.0,
    }  # fmt: skip
    assert block["historial_del_setup"] == "sin historial todavía"
    assert block["noticias"] == [] and block["eventos_proximos"] == []


def test_dossier_shows_up_to_five_news_newest_first_with_date_source_and_sentiment():
    items = [news(f"Titular {i}", hours_ago=i + 1, sentiment=0.1 * i) for i in range(7)]
    items.append(news("x" * 400, hours_ago=0.5, summary="resumen " * 80, sentiment=-0.4))

    dossier, _ = dossier_for(news=items)
    block = next(b for b in extract_json_blocks(dossier.text) if b.get("ticker") == "T0")

    shown = block["noticias"]
    assert len(shown) == 5
    assert shown[0]["fecha"] == "2026-11-10 08:30 NY"
    assert (shown[0]["fuente"], shown[0]["sentimiento"]) == ("alpaca", -0.4)
    assert len(shown[0]["titular"]) == 220 and shown[0]["titular"].endswith("…")
    assert len(shown[0]["resumen"]) <= 240
    assert [n["titular"] for n in shown[1:]] == [f"Titular {i}" for i in range(4)]


def test_dossier_shows_coming_events_control_warnings_and_setup_history():
    earnings = CorporateEvent(
        ticker="T0", event_type="earnings", event_date=DAY + timedelta(days=6),
        payload={"kind": "scheduled", "hour": "amc"}, source="finnhub",
    )  # fmt: skip
    past = CorporateEvent(
        ticker="T0", event_type="earnings", event_date=DAY - timedelta(days=80), source="sec",
    )  # fmt: skip
    cpi = EconEvent(
        name="CPI", country="US", scheduled_at=clock.market_time(DAY, time(10, 0)), impact="high",
        source="fred",
    )  # fmt: skip

    dossier, finalists = dossier_for(
        corporate_events={"T0": [past, earnings]},
        econ_events=[cpi],
        history={"RUPTURA": SetupStats(trades=24, win_rate_pct=45.83, expectancy_r=0.214)},
    )
    blocks = extract_json_blocks(dossier.text)
    block = next(b for b in blocks if isinstance(b, dict) and b.get("ticker") == "T0")

    assert block["eventos_proximos"] == [
        {"tipo": "resultados", "fecha": "2026-11-16", "momento": "después del cierre"}
    ]
    assert block["historial_del_setup"] == {
        "operaciones": 24, "tasa_de_acierto_pct": 45.8, "expectativa_en_r": 0.21,
    }  # fmt: skip
    # A CPI at 10:00 halves the size (control C09, type R): the AI is told.
    (warning,) = block["avisos_de_controles"]
    assert (warning["control"], warning["efecto"]) == ("C09", "reduce el tamaño a la mitad")
    assert blocks[1] == [
        {
            "evento": "CPI", "fecha": "2026-11-10", "hora_nueva_york": "10:00",
            "hora_monterrey": "09:00", "impacto": "high", "es_hoy": True,
        }
    ]  # fmt: skip
    assert all(f.controls for f in finalists)


def test_dossier_shows_the_portfolio_with_the_open_result_in_r():
    held = Holding("HELD", "XLK", 10.0, 100.0, 104.0, 100.0, DAY - timedelta(days=3),
                   initial_stop=98.0)  # fmt: skip
    portfolio = Portfolio(cash=3960.0, holdings=(held,), week_pnl_pct=0.8)

    dossier, _ = dossier_for(portfolio=portfolio)
    account = extract_json_blocks(dossier.text)[1]

    assert (account["efectivo_usd"], account["capital_usd"]) == (3960.0, 5000.0)
    assert account["posiciones_abiertas"] == [
        {
            "ticker": "HELD", "sector_etf": "XLK", "precio_de_entrada": 100.0,
            "precio_actual": 104.0, "stop": 100.0, "resultado_en_r": 2.0, "pct_del_capital": 20.8,
        }
    ]  # fmt: skip
    assert account["pct_del_capital_por_sector"] == {"XLK": 20.8}
    assert account["resultado_de_la_semana_pct"] == 0.8


def test_dossier_only_shows_the_lessons_that_apply_today():
    lessons = [
        LessonNote("L-001", "Las rupturas sin volumen fallan", {"setup": "RUPTURA"}),
        LessonNote("L-002", "Los rebotes fallan en riesgo alto", {"regime": "RIESGO_ALTO"}),
        LessonNote("L-003", "Energía no sostiene los gaps", {"sector_etf": "XLE"}),
        LessonNote("L-004", "Esperar la primera media hora", {}),
    ]

    dossier, _ = dossier_for(lessons=lessons)

    shown = extract_json_blocks(dossier.text)[2]
    assert [lesson["id"] for lesson in shown] == ["L-001", "L-004"]
    assert shown[0] == {
        "id": "L-001", "texto": "Las rupturas sin volumen fallan", "ambito": {"setup": "RUPTURA"},
    }  # fmt: skip


def test_fingerprint_ignores_the_time_of_day_but_not_the_data(_fixed_time):
    first, _ = dossier_for()
    candidates = [candidate("T0", 85.0), candidate("T1", 78.0, setup="PULLBACK")]
    state, indicators = state_and_indicators(candidates, sectors={"T0": "XLK", "T1": "XLF"})
    finalists = dossier_for()[1]
    later = build_premarket_dossier(
        replace(state, as_of=AS_OF + timedelta(minutes=10)), indicators, Portfolio(cash=5000.0),
        PROFILE, CONFIG, finalists,
    )  # fmt: skip
    other, _ = dossier_for([candidate("T0", 86.0), candidate("T1", 78.0, setup="PULLBACK")])

    assert "Hora: 09:10 Nueva York" in later.text and later.text != first.text
    assert later.fingerprint == first.fingerprint
    assert other.fingerprint != first.fingerprint


def test_only_lessons_the_database_holds_as_active_reach_the_dossier(engine):
    def lesson(identifier, status, **evidence):
        return models.Lesson(
            id=identifier, kind="pattern", statement=f"hipótesis {identifier}",
            scope={"setup": "RUPTURA"}, test={}, effect={"type": "dossier_text"}, status=status,
            proposed_at=datetime(2026, 9, 1, tzinfo=UTC), **evidence,
        )  # fmt: skip

    with session_scope(engine) as session:
        session.add(lesson("L-001", "active", cases_total=25, cases_forward=12, confidence=0.7))
        session.add(lesson("L-002", "hypothesis", cases_total=30, cases_forward=15, confidence=0.9))
    with session_scope(engine) as session:
        lessons = load_active_lessons(session)

    assert [(note.id, note.statement, note.scope) for note in lessons] == [
        ("L-001", "hipótesis L-001", {"setup": "RUPTURA"})
    ]


# --- the plan with the AI ----------------------------------------------------------------------

MIXED = [candidate("BUY", 85.0), candidate("NEAR", 65.0), candidate("LOW", 40.0),
         candidate("CAP", 88.0), candidate("EARN", 80.0, days_to_earnings=1.0)]  # fmt: skip


def plan_with(client, candidates=MIXED, *, portfolio=None, config=CONFIG, caps=None, **kwargs):
    state, indicators = state_and_indicators(candidates, caps=caps or {"CAP": 4e8})
    the_analyst = Analyst(gateway(client, **kwargs))
    plan = build_plan(
        state, indicators, portfolio or Portfolio(cash=5000.0), config,
        candidates=candidates, analyst=the_analyst,
    )  # fmt: skip
    return plan, (state, indicators)


def edit(ticker: str, **changes):
    """A simulated AI whose answer for ``ticker`` is changed."""

    def mutate(output, request):
        output = copy.deepcopy(output)
        for decision in output["decisiones"]:
            if decision["ticker"] == ticker:
                entry = changes.get("entrada")
                decision.update({k: v for k, v in changes.items() if k != "entrada"})
                if entry:
                    decision["entrada"].update(entry)
        return output

    return SimulatedAI(mutate=mutate)


def verdicts(plan) -> dict[str, str]:
    return {idea.ticker: idea.verdict for idea in plan.ideas}


def test_only_candidates_that_pass_the_controls_reach_the_ai():
    ai = SimulatedAI()

    plan, _ = plan_with(ai)

    (call,) = ai.calls
    assert call.task == TASK_ANALYST and call.model == "sonnet"
    tickers = [b["ticker"] for b in extract_json_blocks(call.dossier) if "ticker" in b]
    assert tickers == ["BUY", "NEAR", "LOW"]  # CAP and EARN were blocked by a control
    assert plan.used_ai and plan.review.status == "ok"
    assert plan.review.market_reading == "IA simulada. Régimen ALCISTA."


def test_plan_with_the_simulated_ai_matches_the_rules_and_marks_who_decided():
    with_ai, (state, indicators) = plan_with(SimulatedAI())
    rules = build_plan(state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=MIXED)

    assert verdicts(with_ai) == verdicts(rules) == {
        "BUY": "COMPRAR", "NEAR": "VIGILAR", "CAP": "NO_HOY", "EARN": "NO_HOY", "LOW": "NO_HOY",
    }  # fmt: skip
    by_ticker = {idea.ticker: idea for idea in with_ai.ideas}
    assert not by_ticker["BUY"].no_ai and by_ticker["BUY"].ai.probability == pytest.approx(0.575)
    # A candidate a control blocked never had an AI decision.
    assert by_ticker["CAP"].no_ai and by_ticker["CAP"].ai is None
    assert all(idea.no_ai and idea.ai is None for idea in rules.ideas)
    # The size comes from the code. The AI's probability (0.575) only moves the conviction
    # factor: 0.875 of the base risk of 37.50 over 1.95 per share -> 16 shares; without
    # the AI the factor is 0.5 -> 9 shares.
    rule_buy = rules.by_verdict("COMPRAR")[0]
    assert by_ticker["BUY"].multipliers["conviccion"] == pytest.approx(0.875)
    assert (by_ticker["BUY"].shares, rule_buy.shares) == (16, 9)
    assert by_ticker["BUY"].amount == pytest.approx(16 * 50.25)


def test_controls_veto_an_ai_purchase():
    """The AI picks a first target only 1 ATR away: control C13 (reward of at least
    1.5R) blocks it, whatever the AI says."""
    ai = edit("BUY", veredicto="COMPRAR", prob_objetivo_antes_que_stop=0.9, objetivos=[51.5, 55.1])

    plan, _ = plan_with(ai)
    idea = next(i for i in plan.ideas if i.ticker == "BUY")

    assert idea.verdict == "NO_HOY"
    assert idea.reason.startswith("C13 ")
    assert idea.ai.verdict == "COMPRAR"  # what the AI said is kept for the audit
    assert idea.shares == 0


def test_the_ai_cannot_revive_a_candidate_a_control_blocked():
    def mutate(output, request):
        output["decisiones"].append(answer("CAP", prob_objetivo_antes_que_stop=0.95))
        return output

    plan, _ = plan_with(SimulatedAI(mutate=mutate))
    idea = next(i for i in plan.ideas if i.ticker == "CAP")

    assert idea.verdict == "NO_HOY" and idea.reason.startswith("C05 ")
    assert plan.review.ignored_tickers == ("CAP",)


def test_an_ai_purchase_below_the_minimum_probability_is_only_watched():
    plan, _ = plan_with(edit("BUY", prob_objetivo_antes_que_stop=0.44))
    idea = next(i for i in plan.ideas if i.ticker == "BUY")

    assert idea.verdict == "VIGILAR"
    assert idea.reason == (
        "la IA estima 44% de probabilidad de tocar el objetivo antes que el stop; el mínimo es 50%"
    )


def test_an_ai_purchase_below_the_minimum_score_is_not_a_purchase():
    plan, _ = plan_with(edit("NEAR", veredicto="COMPRAR", prob_objetivo_antes_que_stop=0.9))
    idea = next(i for i in plan.ideas if i.ticker == "NEAR")

    assert idea.verdict == "VIGILAR"
    assert idea.reason == "puntaje 65, cerca del mínimo de 70"


def test_ai_watch_and_discard_set_the_verdict():
    watch, _ = plan_with(
        edit("BUY", veredicto="VIGILAR", disparador_vigilar="cierre de 5 min arriba de 50.25")
    )
    discard, _ = plan_with(
        edit("BUY", veredicto="DESCARTAR", riesgos=["volumen insuficiente en la ruptura"])
    )
    watched = next(i for i in watch.ideas if i.ticker == "BUY")
    discarded = next(i for i in discard.ideas if i.ticker == "BUY")

    assert watched.verdict == "VIGILAR"
    assert watched.reason == "la IA pide esperar una confirmación"
    assert watched.watch_trigger == "cierre de 5 min arriba de 50.25"
    assert discarded.verdict == "NO_HOY"
    assert discarded.reason == "la IA lo descartó: volumen insuficiente en la ruptura"


def test_an_invented_stop_is_corrected_in_the_plan_or_the_idea_is_dropped():
    corrected, _ = plan_with(edit("BUY", stop=47.6))
    dropped, _ = plan_with(edit("BUY", stop=41.0))
    fixed = next(i for i in corrected.ideas if i.ticker == "BUY")
    gone = next(i for i in dropped.ideas if i.ticker == "BUY")

    assert fixed.verdict == "COMPRAR" and fixed.plan.stop == 48.3
    assert "el stop 47.60 no coincide con un nivel válido" in fixed.ai.corrections[0]
    assert gone.verdict == "NO_HOY"
    assert "el stop 41.00 no sale de los niveles válidos" in gone.reason
    assert "Corregido por el código: el stop 47.60" in render_plan(corrected, CONFIG, 5000.0)


def test_a_finalist_missing_from_the_answer_is_not_bought():
    def mutate(output, request):
        output["decisiones"] = [d for d in output["decisiones"] if d["ticker"] != "BUY"]
        return output

    plan, _ = plan_with(SimulatedAI(mutate=mutate))
    idea = next(i for i in plan.ideas if i.ticker == "BUY")

    assert (idea.verdict, idea.reason) == ("NO_HOY", "sin respuesta de la IA")


def test_two_invalid_answers_publish_the_plan_with_rules_marked_without_ai():
    ai = SimulatedAI(script=["invalid", "invalid"])

    plan, (state, indicators) = plan_with(ai)
    rules = build_plan(state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=MIXED)

    assert len(ai.calls) == 2
    assert not plan.used_ai and plan.review.status == "failed"
    assert verdicts(plan) == verdicts(rules)
    assert all(idea.no_ai for idea in plan.ideas)
    text = render_plan(plan, CONFIG, capital=5000.0)
    assert "Veredictos por reglas (solo reglas): la IA falló dos veces" in text
    assert "COMPRAR  BUY  (RUPTURA · puntaje 85 · sin IA)" in text


def test_without_quota_the_plan_is_published_with_rules():
    ai = SimulatedAI()

    plan, _ = plan_with(ai, daily_call_budget=0)

    assert ai.calls == []
    assert plan.review.status == "no_quota" and plan.review.label == "SIN CUOTA IA"
    assert verdicts(plan)["BUY"] == "COMPRAR"
    assert all(idea.no_ai for idea in plan.ideas)
    assert "Veredictos por reglas (SIN CUOTA IA): SIN CUOTA IA: 0 de 0 consultas usadas hoy." in (
        render_plan(plan, CONFIG, capital=5000.0)
    )


def test_no_finalists_means_no_call():
    ai = SimulatedAI()
    blocked = [candidate(f"B{i}", 80.0 - i) for i in range(5)]

    plan, _ = plan_with(ai, blocked, caps={c.ticker: 4e8 for c in blocked})

    assert ai.calls == []
    assert plan.review.status == "no_finalists"
    assert plan.by_verdict("COMPRAR") == []


def test_candidates_beyond_the_finalists_can_be_watched_but_not_bought():
    many = [candidate(f"T{i:02d}", 95.0 - i) for i in range(12)]
    ai = SimulatedAI()

    plan, _ = plan_with(ai, many, portfolio=Portfolio(cash=50_000.0))

    sent = [b["ticker"] for b in extract_json_blocks(ai.calls[0].dossier) if "ticker" in b]
    assert sent == [f"T{i:02d}" for i in range(10)]
    by_ticker = {idea.ticker: idea for idea in plan.ideas}
    for ticker in ("T10", "T11"):
        assert by_ticker[ticker].verdict == "VIGILAR"
        assert by_ticker[ticker].reason == "no entró entre los 10 finalistas que analizó la IA"
        assert by_ticker[ticker].no_ai


def test_watch_only_mode_never_buys_even_if_the_ai_says_so():
    watch_only = CONFIG.model_copy(
        update={"system": CONFIG.system.model_copy(update={"mode": "watch_only"})}
    )

    plan, _ = plan_with(SimulatedAI(), config=watch_only)

    assert plan.by_verdict("COMPRAR") == []
    assert verdicts(plan)["BUY"] == "VIGILAR"


def test_the_recheck_reuses_the_review_without_asking_the_ai_again():
    ai = SimulatedAI()
    published, (state, indicators) = plan_with(ai)

    rechecked = build_plan(
        state, indicators, Portfolio(cash=5000.0), CONFIG, candidates=MIXED,
        opening_prices={"BUY": 51.2}, review=published.review,
    )  # fmt: skip
    idea = next(i for i in rechecked.ideas if i.ticker == "BUY")

    assert len(ai.calls) == 1
    assert rechecked.review is published.review
    assert idea.verdict == "VIGILAR" and "abrió en $51.20" in idea.reason
    assert idea.ai is not None and not idea.no_ai


def test_plan_text_shows_what_the_ai_said():
    plan, _ = plan_with(SimulatedAI())

    text = render_plan(plan, CONFIG, capital=5000.0)

    assert "Lectura de la IA: IA simulada. Régimen ALCISTA." in text
    assert "COMPRAR  BUY  (RUPTURA · puntaje 85 · prob. 0.57)" in text
    # 0.575 x 2.0R - 0.425 - 0.20R of costs = 0.525R.
    assert "Expectativa: +0.52R después de costos" in text
    assert (
        "IA: 57% de probabilidad de tocar el objetivo antes que el stop · confianza 50% · "
        "horizonte 5 días"
    ) in text
    assert "Tesis: IA simulada: RUPTURA con puntaje 85." in text
    assert "Se invalida si: cierre debajo de 48.30" in text
    assert "NO HOY  CAP  (RUPTURA · puntaje 88 · sin IA)" in text


# --- news classified by the AI -----------------------------------------------------------------


def test_news_are_classified_in_batches_of_twenty():
    ai = SimulatedAI()
    items = [(i, news(f"Company {i} beats estimates and raises guidance", 1.0)) for i in range(45)]

    classified, note = classify_news(gateway(ai), items)

    assert note == ""
    assert len(ai.calls) == 3
    assert {call.model for call in ai.calls} == {"haiku"}
    assert [len(extract_json_blocks(call.dossier)[0]) for call in ai.calls] == [20, 20, 5]
    assert sorted(classified) == list(range(45))
    assert classified[0].sentiment > 0


def test_news_keep_their_rule_values_when_the_ai_cannot_be_used():
    items = [(1, news("Company beats estimates", 1.0))]

    classified, note = classify_news(gateway(SimulatedAI(), daily_call_budget=0), items)

    assert classified == {}
    assert note.startswith("SIN CUOTA IA")


def test_the_ai_can_flag_a_headline_as_severe_but_never_clear_it():
    plain = news("Company to sell 10 million shares", 1.0, sentiment=0.0)
    severe = news("SEC opens fraud probe", 1.0, sentiment=-0.8, category="investigation",
                  severe=True)  # fmt: skip

    flagged = refine(plain, NewsClassification(1, -0.2, "oferta_acciones", True))
    cleared = refine(severe, NewsClassification(2, 0.6, "producto", False))
    refined = refine(plain, NewsClassification(3, 0.4, "producto", False))

    assert (flagged.category, flagged.severe, flagged.sentiment) == ("offering", True, -0.2)
    # The rules found it severe: it stays severe and its sentiment cannot turn positive.
    assert (cleared.category, cleared.severe, cleared.sentiment) == ("investigation", True, 0.0)
    assert (refined.category, refined.severe, refined.sentiment) == (None, False, 0.4)
