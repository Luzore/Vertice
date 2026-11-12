"""Node 6 (output): what the code does with the AI's answer (ROADMAP section 9).

The AI decides *what*; it never gets to invent a number (principle 3). Each
decision is checked here before it can reach the plan:

* only tickers that were in the dossier count; any other is ignored;
* a finalist the AI did not answer for is discarded ("sin respuesta de la IA");
* every level must be within 0.5% of a valid level. One that is not is moved to
  the nearest valid level when it is a near miss (up to one ATR away) and
  recorded as a correction; further than that the number was made up and the
  idea is discarded;
* after that the order must hold: stop < entry zone < first target < second target.

The controls still run on the validated plan afterwards, and they have the last word.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from engine.dossier import Finalist
from engine.levels import NamedLevel
from engine.trade_plan import TradePlan
from engine.universe import normalize_ticker

TOLERANCE_PCT = 0.5
MAX_CORRECTION_ATR = 1.0
NO_ANSWER = "sin respuesta de la IA"


@dataclass(frozen=True)
class AIDecision:
    ticker: str
    # COMPRAR, VIGILAR or DESCARTAR, after validation.
    verdict: str
    setup: str | None = None
    probability: float | None = None
    confidence: float | None = None
    entry_type: str | None = None
    # The AI's levels once validated; None when the idea was discarded.
    plan: TradePlan | None = None
    watch_trigger: str | None = None
    horizon_days: int | None = None
    thesis: str = ""
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    invalidation: str = ""
    lessons_applied: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    # What the code changed in the AI's answer, in Spanish.
    corrections: tuple[str, ...] = ()
    # Why the code discarded the idea; empty when DESCARTAR is the AI's own verdict.
    discard_reason: str = ""


class _Discard(Exception):
    """The decision cannot be repaired: the idea is dropped with this reason."""


def _snap(
    value: float, valid: Sequence[NamedLevel], atr: float, label: str, corrections: list[str]
) -> tuple[float, NamedLevel]:
    """``value`` if it is within tolerance of a valid level, else the nearest valid
    level (recorded as a correction). Raises when it is not even a near miss."""
    if not valid:
        raise _Discard(f"no hay niveles válidos para {label}")
    nearest = min(valid, key=lambda level: abs(level.price - value))
    gap = abs(value - nearest.price)
    if gap <= nearest.price * TOLERANCE_PCT / 100:
        return round(value, 2), nearest
    if gap > MAX_CORRECTION_ATR * atr:
        raise _Discard(
            f"{label} {value:,.2f} no sale de los niveles válidos: queda a {gap / atr:.1f} ATR "
            f"del más cercano ({nearest.price:,.2f})"
        )
    corrections.append(
        f"{label} {value:,.2f} no coincide con un nivel válido; se corrigió a "
        f"{nearest.price:,.2f} ({nearest.name})"
    )
    return nearest.price, nearest


def _validated_plan(
    raw: Mapping[str, Any], finalist: Finalist, stop_range_atr: tuple[float, float]
) -> tuple[TradePlan, list[str]]:
    """The AI's levels as a plan, corrected where needed. Raises ``_Discard``."""
    valid, rule = finalist.valid, finalist.plan
    atr, reference = valid.atr, valid.reference
    corrections: list[str] = []

    stop, anchor = _snap(float(raw["stop"]), valid.stops, atr, "el stop", corrections)
    low, high = stop_range_atr
    if not low - 1e-9 <= (reference - stop) / atr <= high + 1e-9:
        # Within tolerance of a valid level but outside the distance the controls accept.
        stop = anchor.price

    entry = raw["entrada"]
    entry_min, _ = _snap(
        float(entry["zona_min"]), valid.entries, atr, "la zona mínima", corrections
    )
    entry_max, _ = _snap(
        float(entry["zona_max"]), valid.entries, atr, "la zona máxima", corrections
    )
    no_chase, _ = _snap(
        float(entry["no_perseguir_sobre"]), valid.entries, atr, "el no perseguir", corrections
    )
    if not entry_min <= entry_max <= no_chase:
        corrections.append(
            f"zona de entrada incoherente ({entry_min:,.2f}–{entry_max:,.2f}, no perseguir "
            f"{no_chase:,.2f}); se usa la del plan de reglas"
        )
        entry_min, entry_max, no_chase = rule.entry_min, rule.entry_max, rule.no_chase_above

    targets_valid = valid.targets_for(stop)
    target_1, _ = _snap(
        float(raw["objetivos"][0]), targets_valid, atr, "el primer objetivo", corrections
    )
    target_2, _ = _snap(
        float(raw["objetivos"][1]), targets_valid, atr, "el segundo objetivo", corrections
    )
    if target_2 <= target_1:
        higher = [level for level in targets_valid if level.price > target_1]
        if not higher:
            raise _Discard("el segundo objetivo no queda arriba del primero")
        corrections.append(
            f"el segundo objetivo {target_2:,.2f} no supera al primero; se corrigió a "
            f"{higher[0].price:,.2f} ({higher[0].name})"
        )
        target_2 = higher[0].price

    if stop >= entry_min:
        raise _Discard(f"el stop {stop:,.2f} no queda debajo de la zona de entrada")
    if target_1 <= no_chase:
        raise _Discard(
            f"el primer objetivo {target_1:,.2f} no queda arriba del precio de no perseguir"
        )
    plan = TradePlan(
        entry_min=entry_min,
        entry_max=entry_max,
        no_chase_above=no_chase,
        reference=rule.reference,
        stop=stop,
        stop_level=anchor.anchor,
        stop_level_name=anchor.anchor_name,
        targets=(target_1, target_2),
        atr=rule.atr,
    )
    return plan, corrections


def _texts(values: Any) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in values or () if str(v).strip())


def _decision(
    raw: Mapping[str, Any],
    finalist: Finalist,
    stop_range_atr: tuple[float, float],
    lesson_ids: Collection[str],
) -> AIDecision:
    ticker = finalist.ticker
    probability, confidence = float(raw["prob_objetivo_antes_que_stop"]), float(raw["confianza"])
    corrections: list[str] = []
    applied = _texts(raw.get("lecciones_aplicadas"))
    unknown = [lesson for lesson in applied if lesson not in lesson_ids]
    if unknown:
        corrections.append(f"lecciones que no están en vigor, ignoradas: {', '.join(unknown)}")
    decision = AIDecision(
        ticker=ticker,
        verdict=str(raw["veredicto"]),
        setup=raw.get("setup"),
        probability=probability,
        confidence=confidence,
        entry_type=raw["entrada"].get("tipo"),
        watch_trigger=(raw.get("disparador_vigilar") or None),
        horizon_days=int(raw["horizonte_dias"]),
        thesis=str(raw.get("tesis", "")).strip(),
        reasons=_texts(raw.get("razones")),
        risks=_texts(raw.get("riesgos")),
        invalidation=str(raw.get("invalidacion", "")).strip(),
        lessons_applied=tuple(lesson for lesson in applied if lesson in lesson_ids),
        missing_data=_texts(raw.get("datos_faltantes")),
    )
    if not (0.0 <= probability <= 1.0 and 0.0 <= confidence <= 1.0):
        return replace(
            decision,
            verdict="DESCARTAR",
            probability=None,
            confidence=None,
            discard_reason="la IA dio una probabilidad fuera de 0 a 1",
        )
    if decision.verdict == "DESCARTAR":
        return replace(decision, corrections=tuple(corrections))
    try:
        plan, fixed = _validated_plan(raw, finalist, stop_range_atr)
    except _Discard as problem:
        return replace(
            decision,
            verdict="DESCARTAR",
            corrections=tuple(corrections),
            discard_reason=f"la IA propuso niveles que no se pudieron validar: {problem}",
        )
    return replace(decision, plan=plan, corrections=(*corrections, *fixed))


def validate_decisions(
    output: Mapping[str, Any],
    finalists: Sequence[Finalist],
    *,
    stop_range_atr: tuple[float, float],
    lesson_ids: Collection[str] = (),
) -> tuple[dict[str, AIDecision], tuple[str, ...]]:
    """One decision per finalist, plus the tickers the AI returned that were not asked."""
    by_ticker = {finalist.ticker: finalist for finalist in finalists}
    decisions: dict[str, AIDecision] = {}
    ignored: list[str] = []
    for raw in output.get("decisiones", ()):
        ticker = normalize_ticker(str(raw.get("ticker", "")))
        if ticker not in by_ticker:
            ignored.append(ticker)
            continue
        if ticker in decisions:
            continue  # a repeated ticker: the first answer stands
        decisions[ticker] = _decision(raw, by_ticker[ticker], stop_range_atr, lesson_ids)
    for ticker in by_ticker:
        if ticker not in decisions:
            decisions[ticker] = AIDecision(
                ticker=ticker, verdict="DESCARTAR", discard_reason=NO_ANSWER
            )
    return decisions, tuple(ignored)
