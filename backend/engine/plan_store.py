"""Node 8 (storage): the daily plan in the database (principles 7 and 8).

Saving a plan writes the run, every listed candidate with the result of each
control, and one idea per entry with everything needed to reproduce the decision
later. Reading it back gives what the dashboard and Telegram show.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from core import clock
from core.settings import AppConfig
from db import models
from engine import controls as ctl
from engine import states
from engine.recommender import DailyPlan, Idea

VERDICT_AUDIT = "idea_verdict"


class PlanLocked(Exception):
    """Today's plan already has confirmations: it cannot be replaced."""


def _idea_details(idea: Idea, rank: int) -> dict[str, Any]:
    plan, decision, outlook = idea.plan, idea.ai, idea.expectancy
    details: dict[str, Any] = {
        "name": idea.name,
        "sector_etf": idea.sector_etf,
        "rank": rank,
        "notes": list(idea.notes),
        "breakdown": idea.breakdown,
        "size_multiplier": idea.size_multiplier,
        "multipliers": idea.multipliers,
        "limited_by": idea.limited_by,
        "warnings": [f"{c.id} {c.reason}" for c in idea.controls if c.effect == "reduce"],
    }
    if plan is not None:
        details |= {
            "reference": plan.reference,
            "atr": plan.atr,
            "stop_level": plan.stop_level,
            "stop_level_name": plan.stop_level_name,
            "targets_r": [round(plan.r_multiple(target), 2) for target in plan.targets],
        }
    if outlook is not None:
        details |= {
            "reward_r": round(outlook.reward_r, 4),
            "costs_r": round(outlook.costs_r, 4),
            "assumed_probability": outlook.assumed_probability,
        }
    if decision is not None:
        details |= {
            "ai_verdict": decision.verdict,
            "confidence": decision.confidence,
            "corrections": list(decision.corrections),
            "missing_data": list(decision.missing_data),
        }
    return details


def _idea_row(
    idea: Idea, plan: DailyPlan, run_id: int, candidate_id: int, rank: int
) -> models.Idea:
    levels, decision = idea.plan, idea.ai
    call_id = plan.review.call_id if plan.review is not None and not idea.no_ai else None
    return models.Idea(
        run_id=run_id,
        candidate_id=candidate_id,
        ai_call_id=call_id,
        plan_date=plan.plan_date,
        ticker=idea.ticker,
        setup=idea.setup,
        score=idea.score,
        verdict=idea.verdict,
        no_ai=idea.no_ai,
        prob_raw=decision.probability if decision is not None else None,
        prob_calibrated=idea.probability,
        entry_type=(decision.entry_type if decision is not None and decision.plan else None)
        or ("limite" if levels is not None else None),
        entry_min=levels.entry_min if levels is not None else None,
        entry_max=levels.entry_max if levels is not None else None,
        no_chase_above=levels.no_chase_above if levels is not None else None,
        watch_trigger=idea.watch_trigger,
        stop=levels.stop if levels is not None else None,
        targets=list(levels.targets) if levels is not None else None,
        horizon_days=decision.horizon_days if decision is not None else None,
        shares=idea.shares,
        amount_usd=idea.amount,
        risk_usd=idea.risk_usd,
        ev_r=round(idea.ev_r, 4) if idea.ev_r is not None else None,
        thesis=decision.thesis if decision is not None else None,
        reasons=list(decision.reasons) if decision is not None else None,
        risks=list(decision.risks) if decision is not None else None,
        invalidation=decision.invalidation if decision is not None else None,
        lessons_applied=list(decision.lessons_applied) if decision is not None else [],
        state="PROPOSED",
        created_at=clock.now(),
        reason=idea.reason or None,
        details=_idea_details(idea, rank),
    )


def _plan_stats(plan: DailyPlan) -> dict[str, Any]:
    review, brake = plan.review, plan.brake
    return {
        "plan_date": plan.plan_date.isoformat(),
        "market_reading": plan.market_reading,
        "regime_reasons": list(plan.regime_reasons),
        "events_today": [
            {"name": event.name, "at": event.scheduled_at.isoformat(), "impact": event.impact}
            for event in plan.events_today
        ],
        "screened": plan.screened,
        "with_setup": plan.with_setup,
        "ai": {
            "status": review.status,
            "label": review.label,
            "note": review.note,
            "reading": review.market_reading,
            "call_id": review.call_id,
            "ignored_tickers": list(review.ignored_tickers),
        }
        if review is not None
        else None,
        "rotations": [
            {"sell": r.sell, "buy": r.buy, "edge_r": r.edge_r, "text": r.text}
            for r in plan.rotations
        ],
        "brake": {"state": brake.state, "reasons": list(brake.reasons)}
        if brake is not None
        else None,
        "streak_note": plan.streak_note,
    }


def plan_run_id(session: Session, plan_date: date) -> int | None:
    """The run whose plan is in force for ``plan_date`` (the latest one saved)."""
    return session.scalar(
        select(func.max(models.Idea.run_id)).where(models.Idea.plan_date == plan_date)
    )


def _remove_unconfirmed_plan(session: Session, plan_date: date) -> None:
    """Drop an earlier plan of the same day so a new one can replace it, which is
    only allowed while nobody has acted on it."""
    ideas = list(session.scalars(select(models.Idea).where(models.Idea.plan_date == plan_date)))
    if not ideas:
        return
    idea_ids = [idea.id for idea in ideas]
    touched = any(idea.state != "PROPOSED" for idea in ideas) or session.scalar(
        select(func.count())
        .select_from(models.Confirmation)
        .where(models.Confirmation.idea_id.in_(idea_ids))
    )
    if touched:
        raise PlanLocked(
            f"el plan del {plan_date.isoformat()} ya tiene confirmaciones y no se puede reemplazar"
        )
    run_ids = {idea.run_id for idea in ideas}
    candidate_ids = select(models.Candidate.id).where(models.Candidate.run_id.in_(run_ids))
    session.execute(delete(models.Idea).where(models.Idea.id.in_(idea_ids)))
    session.execute(
        delete(models.ControlResult).where(models.ControlResult.candidate_id.in_(candidate_ids))
    )
    session.execute(delete(models.Candidate).where(models.Candidate.run_id.in_(run_ids)))
    for run in session.scalars(select(models.Run).where(models.Run.id.in_(run_ids))):
        run.stats = {**(run.stats or {}), "replaced_at": clock.now().isoformat()}


def save_plan(
    session: Session,
    plan: DailyPlan,
    *,
    kind: str = "premarket",
    started_at: datetime | None = None,
) -> int:
    """Write the plan and return its run id. Replaces an earlier plan of the same
    day unless the user already confirmed something on it (``PlanLocked``)."""
    _remove_unconfirmed_plan(session, plan.plan_date)
    run = models.Run(
        kind=kind,
        as_of=plan.as_of,
        started_at=started_at or clock.now(),
        finished_at=clock.now(),
        regime=plan.regime,
        stats=_plan_stats(plan),
    )
    session.add(run)
    session.flush()
    for rank, idea in enumerate(plan.ideas, start=1):
        candidate = models.Candidate(
            run_id=run.id,
            ticker=idea.ticker,
            setup=idea.setup,
            score=min(100.0, max(0.0, idea.score)),
            breakdown=idea.breakdown,
        )
        session.add(candidate)
        session.flush()
        session.add_all(
            models.ControlResult(
                candidate_id=candidate.id,
                control=result.id,
                result=result.result,
                reason=result.reason,
            )
            for result in idea.controls
        )
        row = _idea_row(idea, plan, run.id, candidate.id, rank)
        session.add(row)
        session.flush()
        states.record_creation(
            session, "idea", row, actor="system", reason="Plan del día publicado"
        )
    return run.id


def todays_ideas(session: Session, plan_date: date) -> list[models.Idea]:
    """The ideas of the plan in force for ``plan_date``, in plan order."""
    run_id = plan_run_id(session, plan_date)
    if run_id is None:
        return []
    ideas = session.scalars(select(models.Idea).where(models.Idea.run_id == run_id))
    return sorted(ideas, key=lambda idea: (idea.details or {}).get("rank", idea.id))


def recheck_open(
    session: Session, config: AppConfig, plan_date: date, opening_prices: dict[str, float]
) -> list[tuple[str, str, str]]:
    """The 09:45 review of the saved plan: a purchase that opened above its no-chase
    price becomes VIGILAR, and one that gapped too far is no longer for today.
    Returns ``(ticker, new verdict, reason)`` for each idea that changed."""
    changed = []
    for idea in todays_ideas(session, plan_date):
        opening = opening_prices.get(idea.ticker)
        if idea.verdict != "COMPRAR" or idea.state != "PROPOSED" or opening is None:
            continue
        details = idea.details or {}
        result, why = ctl.opening_gap(
            opening, details["reference"], details["atr"], config.controls.max_gap_atr
        )
        if result == "fail":
            verdict, reason = "NO_HOY", f"C11 Gap: {why}"
        elif opening > idea.no_chase_above:
            verdict = "VIGILAR"
            reason = (
                f"abrió en ${opening:,.2f}, arriba del precio de no perseguir "
                f"(${idea.no_chase_above:,.2f})"
            )
        else:
            continue
        session.add(
            models.StateTransition(
                entity=VERDICT_AUDIT,
                entity_id=str(idea.id),
                from_state=idea.verdict,
                to_state=verdict,
                at=clock.now(),
                actor="system",
                reason=f"revisión tras la apertura: {reason}",
            )
        )
        idea.verdict, idea.reason = verdict, reason
        idea.shares, idea.amount_usd, idea.risk_usd = 0.0, 0.0, 0.0
        changed.append((idea.ticker, verdict, reason))
    return changed


# --- reading -----------------------------------------------------------------------------------


def idea_as_dict(idea: models.Idea, controls: list[models.ControlResult] | None = None) -> dict:
    details = idea.details or {}
    data = {
        "id": idea.id,
        "ticker": idea.ticker,
        "name": details.get("name"),
        "sector_etf": details.get("sector_etf"),
        "setup": idea.setup,
        "score": idea.score,
        "breakdown": details.get("breakdown"),
        "verdict": idea.verdict,
        "reason": idea.reason,
        "state": idea.state,
        "no_ai": idea.no_ai,
        "probability": idea.prob_calibrated,
        "probability_raw": idea.prob_raw,
        "entry": {
            "type": idea.entry_type,
            "min": idea.entry_min,
            "max": idea.entry_max,
            "no_chase_above": idea.no_chase_above,
        },
        "stop": idea.stop,
        "targets": idea.targets,
        "targets_r": details.get("targets_r"),
        "horizon_days": idea.horizon_days,
        "watch_trigger": idea.watch_trigger,
        "shares": idea.shares,
        "amount_usd": idea.amount_usd,
        "risk_usd": idea.risk_usd,
        "ev_r": idea.ev_r,
        "multipliers": details.get("multipliers"),
        "limited_by": details.get("limited_by"),
        "notes": details.get("notes", []),
        "warnings": details.get("warnings", []),
        "thesis": idea.thesis,
        "reasons": idea.reasons or [],
        "risks": idea.risks or [],
        "invalidation": idea.invalidation,
        "lessons_applied": idea.lessons_applied or [],
        "corrections": details.get("corrections", []),
    }
    if controls is not None:
        data["controls"] = [
            {"id": c.control, "result": c.result, "reason": c.reason} for c in controls
        ]
    return data


def plan_as_dict(session: Session, plan_date: date) -> dict | None:
    """The plan in force for ``plan_date`` as plain data, or None when there is none."""
    run_id = plan_run_id(session, plan_date)
    if run_id is None:
        return None
    run = session.get(models.Run, run_id)
    ideas = todays_ideas(session, plan_date)
    controls: dict[int, list[models.ControlResult]] = {}
    candidate_ids = [idea.candidate_id for idea in ideas if idea.candidate_id is not None]
    for result in session.scalars(
        select(models.ControlResult)
        .where(models.ControlResult.candidate_id.in_(candidate_ids))
        .order_by(models.ControlResult.control)
    ):
        controls.setdefault(result.candidate_id, []).append(result)
    stats = run.stats or {}
    return {
        "plan_date": plan_date.isoformat(),
        "run_id": run.id,
        "published_at": run.as_of.isoformat(),
        "published_at_label": clock.dual_label(run.as_of),
        "regime": run.regime,
        **{
            key: stats.get(key)
            for key in (
                "market_reading",
                "regime_reasons",
                "events_today",
                "screened",
                "with_setup",
                "ai",
                "rotations",
                "brake",
                "streak_note",
            )
        },  # fmt: skip
        "ideas": [idea_as_dict(idea, controls.get(idea.candidate_id, [])) for idea in ideas],
    }
