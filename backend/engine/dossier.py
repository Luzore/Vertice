"""Node 6 (input): the dossier the AI reads (ROADMAP section 9).

A dossier is Markdown with JSON blocks: the day and its regime, the macro
context and today's events, the portfolio, the lessons in force and, for each
finalist, its setup, score, key metrics, valid levels, news, coming events,
control warnings and the system's own history with that setup.

Every number in it was computed by the code (principle 3). The AI may only pick
among the levels listed under ``niveles_validos``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import clock
from core.settings import AppConfig
from db import models
from engine.controls import ControlResult
from engine.data_engine import MarketState
from engine.indicators import IndicatorResult
from engine.levels import TARGET_R_MULTIPLES, NamedLevel, ValidLevels
from engine.portfolio import Portfolio
from engine.risk_profile import RiskProfile
from engine.schemas import CorporateEvent, NewsItem
from engine.screener import Candidate
from engine.trade_plan import TradePlan

MAX_NEWS_PER_CANDIDATE = 5
HEADLINE_LIMIT = 220
SUMMARY_LIMIT = 240
EVENTS_HORIZON_DAYS = 15
NEWS_BATCH_SIZE = 20

_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)
_TIME_LINE = re.compile(r"^Hora: .*$", re.M)

# Metric of the system -> name shown to the AI. About 15 are present for a typical ticker.
KEY_METRICS = (
    ("close", "ultimo_cierre"),
    ("ret_1d", "cambio_1d_pct"),
    ("ret_5d", "cambio_5d_pct"),
    ("ret_20d", "cambio_20d_pct"),
    ("gap_pct", "gap_pct"),
    ("rs_20d_vs_spy", "fuerza_relativa_20d_vs_spy_pct"),
    ("rs_60d_vs_spy", "fuerza_relativa_60d_vs_spy_pct"),
    ("rs_20d_vs_sector", "fuerza_relativa_20d_vs_sector_pct"),
    ("rsi_14", "rsi_14"),
    ("rsi_2", "rsi_2"),
    ("adx_14", "adx_14"),
    ("macd_hist", "macd_histograma"),
    ("atr_14", "atr_14"),
    ("atr_pct", "atr_pct_del_precio"),
    ("rvol_20d", "volumen_relativo_20d"),
    ("cmf_20", "flujo_de_dinero_cmf_20"),
    ("extension_atr", "extension_sobre_promedio_20d_en_atr"),
    ("sma_20", "promedio_20d"),
    ("sma_50", "promedio_50d"),
    ("sma_200", "promedio_200d"),
    ("dist_52w_high_pct", "distancia_al_maximo_52_semanas_pct"),
    ("beta_60d", "beta_60d"),
    ("days_to_earnings", "dias_para_resultados"),
    ("days_since_earnings", "dias_desde_resultados"),
    ("eps_surprise_pct", "sorpresa_eps_pct"),
    ("news_sentiment_72h", "sentimiento_noticias_72h"),
    ("data_quality", "calidad_de_datos"),
)
MARKET_METRICS = (
    ("vix_level", "vix"),
    ("vix_change_5d_pct", "vix_cambio_5d_pct"),
    ("breadth_pct_above_sma50", "pct_acciones_sobre_promedio_50d"),
    ("new_highs_52w", "nuevos_maximos_52_semanas"),
    ("new_lows_52w", "nuevos_minimos_52_semanas"),
    ("spy_ret_20d", "spy_cambio_20d_pct"),
    ("ust10y_change_5d", "bono_10_anios_cambio_5d"),
    ("curve_10y_2y", "curva_10_anios_menos_2_anios"),
    ("hy_spread", "diferencial_bonos_alto_rendimiento"),
    ("usd_mxn", "usd_mxn"),
)
_EVENT_NAMES = {
    "earnings": "resultados",
    "offering": "oferta de acciones",
    "split": "split",
    "filing_8k": "aviso 8-K",
    "form_4": "operación de directivos",
}
_EARNINGS_HOURS = {"bmo": "antes de la apertura", "amc": "después del cierre"}


@dataclass(frozen=True)
class Finalist:
    """A candidate that passed the blocking controls, with what the AI is shown."""

    candidate: Candidate
    name: str
    sector_etf: str | None
    # The rule-based plan and the levels the AI may choose from.
    plan: TradePlan
    valid: ValidLevels
    controls: tuple[ControlResult, ...] = ()
    news: tuple[NewsItem, ...] = ()
    events: tuple[CorporateEvent, ...] = ()

    @property
    def ticker(self) -> str:
        return self.candidate.ticker


@dataclass(frozen=True)
class LessonNote:
    """A lesson in force, as the dossier shows it."""

    id: str
    statement: str
    scope: Mapping[str, Any]

    def applies(self, regime: str, setups: set[str], sectors: set[str]) -> bool:
        """True when the scope matches today's regime and at least one finalist."""
        if "regime" in self.scope and self.scope["regime"] != regime:
            return False
        if "setup" in self.scope and self.scope["setup"] not in setups:
            return False
        return "sector_etf" not in self.scope or self.scope["sector_etf"] in sectors


@dataclass(frozen=True)
class SetupStats:
    """How the system itself has done with one setup."""

    trades: int
    win_rate_pct: float
    expectancy_r: float


@dataclass(frozen=True)
class Dossier:
    text: str
    # Hash of the content without the time of day: the same data within the cache
    # window is the same dossier.
    fingerprint: str
    tickers: tuple[str, ...]


def extract_json_blocks(text: str) -> list[Any]:
    """Every JSON block of a dossier, parsed, in order."""
    blocks = []
    for match in _JSON_BLOCK.finditer(text):
        try:
            blocks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blocks


def load_active_lessons(session: Session) -> list[LessonNote]:
    """Lessons the database allows to influence decisions (principle 9): only rows
    whose status is ``active``, which its CHECK constraint ties to the evidence."""
    rows = session.scalars(
        select(models.Lesson).where(models.Lesson.status == "active").order_by(models.Lesson.id)
    )
    return [LessonNote(row.id, row.statement, row.scope or {}) for row in rows]


def _pick(source: Mapping[str, float], names: Sequence[tuple[str, str]]) -> dict[str, float]:
    """The metrics that have a value, under the names the AI reads."""
    picked = {}
    for metric, label in names:
        value = source.get(metric)
        if value is not None and not math.isnan(value):
            picked[label] = round(float(value), 3 if abs(value) < 1 else 2)
    return picked


def _json(data: Any) -> str:
    return "```json\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n```"


def _short(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _levels(levels: Sequence[NamedLevel]) -> list[dict[str, Any]]:
    return [{"precio": level.price, "nivel": level.name} for level in levels]


def _candidate_block(
    finalist: Finalist, today, history: Mapping[str, SetupStats] | None
) -> dict[str, Any]:
    candidate, plan, valid = finalist.candidate, finalist.plan, finalist.valid
    latest = sorted(finalist.news, key=lambda item: item.published_at, reverse=True)
    news = [
        {
            "fecha": f"{clock.to_market(item.published_at):%Y-%m-%d %H:%M} NY",
            "fuente": item.source,
            "titular": _short(item.headline, HEADLINE_LIMIT),
            **({"resumen": _short(item.summary, SUMMARY_LIMIT)} if item.summary else {}),
            "sentimiento": item.sentiment,
            **({"categoria": item.category} if item.category else {}),
            **({"grave": True} if item.severe else {}),
        }
        for item in latest[:MAX_NEWS_PER_CANDIDATE]
    ]
    events = []
    for event in sorted(finalist.events, key=lambda e: e.event_date):
        days = (event.event_date - today).days
        if not 0 <= days <= EVENTS_HORIZON_DAYS:
            continue
        entry = {"tipo": _EVENT_NAMES.get(event.event_type, event.event_type)}
        entry["fecha"] = event.event_date.isoformat()
        hour = _EARNINGS_HOURS.get(str(event.payload.get("hour") or ""))
        if hour:
            entry["momento"] = hour
        events.append(entry)
    warnings = [
        {
            "control": result.id,
            "nombre": result.name,
            "efecto": "reduce el tamaño a la mitad" if result.effect == "reduce" else "aviso",
            "motivo": result.reason,
        }
        for result in finalist.controls
        if result.result == "warn"
    ]
    stats = (history or {}).get(candidate.setup or "")
    return {
        "ticker": candidate.ticker,
        "empresa": finalist.name,
        "sector_etf": finalist.sector_etf,
        "setup": candidate.setup,
        "motivos_del_setup": list(candidate.notes),
        "puntaje": candidate.score,
        "desglose_del_puntaje": candidate.breakdown,
        "metricas": _pick(candidate.metrics, KEY_METRICS),
        "niveles_validos": {
            "entrada": _levels(valid.entries),
            "stops": _levels(valid.stops),
            "objetivos": _levels(valid.targets),
            "objetivos_por_r": (
                "también vale el último cierre más "
                + ", ".join(f"{k:g}" for k in TARGET_R_MULTIPLES)
                + " veces R, donde R es el último cierre menos el stop elegido"
            ),
        },
        "plan_de_reglas": {
            "zona_min": plan.entry_min,
            "zona_max": plan.entry_max,
            "no_perseguir_sobre": plan.no_chase_above,
            "stop": plan.stop,
            "nivel_del_stop": plan.stop_level_name or "múltiplo de ATR",
            "stop_en_atr": round(plan.stop_atr, 2),
            "objetivos": list(plan.targets),
            "recompensa_riesgo_al_objetivo_1": round(plan.reward_r, 2),
        },
        "noticias": news,
        "eventos_proximos": events,
        "avisos_de_controles": warnings,
        "historial_del_setup": (
            {
                "operaciones": stats.trades,
                "tasa_de_acierto_pct": round(stats.win_rate_pct, 1),
                "expectativa_en_r": round(stats.expectancy_r, 2),
            }
            if stats is not None
            else "sin historial todavía"
        ),
    }


def build_premarket_dossier(
    state: MarketState,
    indicators: IndicatorResult,
    portfolio: Portfolio,
    profile: RiskProfile,
    config: AppConfig,
    finalists: Sequence[Finalist],
    *,
    lessons: Sequence[LessonNote] = (),
    setup_history: Mapping[str, SetupStats] | None = None,
) -> Dossier:
    """The dossier of the day's finalists for the analyst."""
    today = clock.market_date(state.as_of)
    market = {
        "fecha": today.isoformat(),
        "regimen": indicators.regime,
        "motivos_del_regimen": list(indicators.regime_reasons),
        "mercado": _pick(indicators.context, MARKET_METRICS),
        "exigencia": {
            "puntaje_minimo": profile.min_score,
            "probabilidad_minima": profile.min_probability,
            "modo": config.system.mode,
        },
    }
    events = [
        {
            "evento": event.name,
            "fecha": f"{clock.to_market(event.scheduled_at):%Y-%m-%d}",
            "hora_nueva_york": f"{clock.to_market(event.scheduled_at):%H:%M}",
            "hora_monterrey": f"{clock.to_user(event.scheduled_at):%H:%M}",
            "impacto": event.impact,
            "es_hoy": clock.market_date(event.scheduled_at) == today,
        }
        for event in sorted(state.econ_events, key=lambda e: e.scheduled_at)
        if 0 <= (clock.market_date(event.scheduled_at) - today).days <= 3
    ]
    capital = portfolio.capital
    sectors: dict[str, float] = {}
    for holding in portfolio.holdings:
        key = holding.sector_etf or "sin sector"
        sectors[key] = sectors.get(key, 0.0) + holding.market_value
    account = {
        "efectivo_usd": round(portfolio.cash, 2),
        "capital_usd": round(capital, 2),
        "resultado_del_dia_pct": round(portfolio.day_pnl_pct, 2),
        "resultado_de_la_semana_pct": round(portfolio.week_pnl_pct, 2),
        "posiciones_abiertas": [
            {
                "ticker": holding.ticker,
                "sector_etf": holding.sector_etf,
                "precio_de_entrada": round(holding.entry_price, 2),
                "precio_actual": round(holding.last_price, 2),
                "stop": round(holding.stop, 2),
                "resultado_en_r": round(holding.r_now, 2) if holding.r_now is not None else None,
                "pct_del_capital": round(100 * holding.market_value / capital, 1)
                if capital > 0
                else None,
            }
            for holding in portfolio.holdings
        ],
        "pct_del_capital_por_sector": {
            name: round(100 * value / capital, 1) for name, value in sorted(sectors.items())
        }
        if capital > 0
        else {},
        "maximo_de_posiciones": profile.max_positions,
    }
    setups = {f.candidate.setup for f in finalists if f.candidate.setup}
    finalist_sectors = {f.sector_etf for f in finalists if f.sector_etf}
    in_force = [
        {"id": lesson.id, "texto": lesson.statement, "ambito": dict(lesson.scope)}
        for lesson in lessons
        if lesson.applies(indicators.regime, setups, finalist_sectors)
    ]

    now_ny, now_user = clock.to_market(state.as_of), clock.to_user(state.as_of)
    lines = [
        f"# Dossier premercado · {today.isoformat()}",
        "",
        f"Hora: {now_ny:%H:%M} Nueva York · {now_user:%H:%M} Monterrey",
        "",
        "## 1. Mercado y régimen",
        "",
        _json(market),
        "",
        "## 2. Eventos macro (hoy y próximos 3 días)",
        "",
        _json(events) if events else "Sin eventos macro en estos días.",
        "",
        "## 3. Cartera",
        "",
        _json(account),
        "",
        "## 4. Lecciones en vigor",
        "",
        _json(in_force) if in_force else "No hay lecciones en vigor que apliquen hoy.",
        "",
        f"## 5. Candidatos ({len(finalists)})",
    ]
    for finalist in finalists:
        candidate = finalist.candidate
        lines += [
            "",
            f"### {candidate.ticker} · {candidate.setup} · puntaje {candidate.score:.0f}",
            "",
            _json(_candidate_block(finalist, today, setup_history)),
        ]
    text = "\n".join(lines) + "\n"
    fingerprint = hashlib.sha256(_TIME_LINE.sub("", text).encode("utf-8")).hexdigest()
    return Dossier(text, fingerprint, tuple(f.ticker for f in finalists))


def build_news_dossier(items: Sequence[tuple[int, NewsItem]]) -> Dossier:
    """A batch of headlines for the news classifier: ``(id, news item)`` pairs."""
    batch = [
        {
            "id": identifier,
            "tickers": list(item.tickers),
            "titular": _short(item.headline, HEADLINE_LIMIT),
            **({"resumen": _short(item.summary, SUMMARY_LIMIT)} if item.summary else {}),
        }
        for identifier, item in items
    ]
    text = f"# Titulares por clasificar ({len(batch)})\n\n{_json(batch)}\n"
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Dossier(text, fingerprint, ())
