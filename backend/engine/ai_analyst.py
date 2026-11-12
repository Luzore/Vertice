"""Node 6: the AI analyst (ROADMAP section 9).

Builds the dossier of the day's finalists, asks the AI through the gateway and
returns one validated decision per finalist. When the AI cannot be used (off, out
of quota, or failing) the review says so and the plan is published with rule
verdicts marked "sin IA".

The analyst never sizes a position and never overrides a control: it returns
verdicts and levels, and the recommender applies the controls to them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from core import settings
from core.settings import AppConfig
from db.session import session_scope
from engine.ai_client import TASK_ANALYST, AIClient, ClaudeCLI, SimulatedAI
from engine.ai_gateway import AIGateway, DatabaseLedger, Ledger, MemoryLedger
from engine.ai_validation import AIDecision, validate_decisions
from engine.data_engine import MarketState
from engine.dossier import (
    Finalist,
    LessonNote,
    SetupStats,
    build_premarket_dossier,
    load_active_lessons,
)
from engine.indicators import IndicatorResult
from engine.portfolio import Portfolio
from engine.risk_profile import RiskProfile
from engine.universe import normalize_ticker

# Review statuses and how the plan names them.
STATUS_LABELS = {
    "ok": "con IA",
    "cache": "con IA (respuesta reutilizada)",
    "disabled": "IA apagada",
    "no_quota": "SIN CUOTA IA",
    "failed": "solo reglas",
    "rules_only": "solo reglas",
    "no_finalists": "sin finalistas",
    "not_used": "sin IA",
}


@dataclass(frozen=True)
class AnalystReview:
    """What the analyst said about the finalists of one run."""

    # ok, cache, disabled, no_quota, failed, rules_only or no_finalists.
    status: str
    # Why there are no decisions, in Spanish; empty when there are.
    note: str = ""
    market_reading: str | None = None
    decisions: Mapping[str, AIDecision] = field(default_factory=dict)
    # Tickers the AI returned that were not in the dossier.
    ignored_tickers: tuple[str, ...] = ()
    call_id: int | None = None
    dossier_fingerprint: str | None = None

    @property
    def used_ai(self) -> bool:
        return self.status in ("ok", "cache")

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class Analyst:
    """Asks the AI about the finalists. One per run."""

    def __init__(
        self,
        gateway: AIGateway,
        *,
        lessons: Sequence[LessonNote] = (),
        setup_history: Mapping[str, SetupStats] | None = None,
    ) -> None:
        self.gateway = gateway
        self._lessons = tuple(lessons)
        self._setup_history = setup_history

    def review(
        self,
        state: MarketState,
        indicators: IndicatorResult,
        portfolio: Portfolio,
        profile: RiskProfile,
        config: AppConfig,
        finalists: Sequence[Finalist],
    ) -> AnalystReview:
        if not finalists:
            return AnalystReview(
                "no_finalists",
                note="ningún candidato pasó los controles: no se consultó a la IA",
            )
        dossier = build_premarket_dossier(
            state,
            indicators,
            portfolio,
            profile,
            config,
            finalists,
            lessons=self._lessons,
            setup_history=self._setup_history,
        )
        expected = set(dossier.tickers)

        def check(output: Any) -> str | None:
            returned = {normalize_ticker(str(d.get("ticker", ""))) for d in output["decisiones"]}
            if not returned & expected:
                return "la respuesta no trae ninguno de los candidatos del dossier"
            return None

        result = self.gateway.call(
            TASK_ANALYST, dossier.text, fingerprint=dossier.fingerprint, check=check
        )
        if not result.ok:
            return AnalystReview(
                result.status, note=result.note, dossier_fingerprint=dossier.fingerprint
            )
        decisions, ignored = validate_decisions(
            result.output,
            finalists,
            stop_range_atr=(config.controls.stop_min_atr, config.controls.stop_max_atr),
            lesson_ids={lesson.id for lesson in self._lessons},
        )
        return AnalystReview(
            result.status,
            market_reading=str(result.output.get("lectura_mercado", "")).strip() or None,
            decisions=decisions,
            ignored_tickers=ignored,
            call_id=result.call_id,
            dossier_fingerprint=dossier.fingerprint,
        )


def runs_dir() -> Path:
    return settings.repo_root() / "runs"


def build_gateway(
    config: AppConfig,
    *,
    client: AIClient | None = None,
    ledger: Ledger | None = None,
    engine: Engine | None = None,
    dossier_dir: Path | None = None,
    run_id: int | None = None,
) -> AIGateway:
    """The gateway as the live system uses it: Claude Code CLI from ``runs/ai_sandbox/``
    and the ``ai_calls`` table. Pass ``client`` and ``ledger`` to replace either."""
    secrets = settings.get_secrets()
    if client is None:
        api_key = secrets.anthropic_api_key
        client = ClaudeCLI(
            runs_dir() / "ai_sandbox",
            api_key=api_key.get_secret_value() if api_key is not None else None,
        )
    if ledger is None:
        ledger = DatabaseLedger(engine) if engine is not None else MemoryLedger()
    root = settings.repo_root()
    return AIGateway(
        client,
        config.ai,
        ledger,
        prompts_dir=root / "prompts",
        schemas_dir=root / "schemas",
        dossier_dir=dossier_dir,
        run_id=run_id,
        secret_values=secrets.secret_values(),
    )


def simulated_analyst(config: AppConfig) -> Analyst:
    """An analyst backed by the simulated AI, with nothing written to disk: for
    replay and tests."""
    return Analyst(build_gateway(config, client=SimulatedAI(), ledger=MemoryLedger()))


def live_analyst(config: AppConfig, engine: Engine, *, run_id: int | None = None) -> Analyst:
    """The analyst of live operation: real AI, calls recorded in the database,
    dossiers saved under ``runs/dossiers/`` and the lessons the database has active."""
    with session_scope(engine) as session:
        lessons = load_active_lessons(session)
    gateway = build_gateway(
        config, engine=engine, dossier_dir=runs_dir() / "dossiers", run_id=run_id
    )
    return Analyst(gateway, lessons=lessons)
