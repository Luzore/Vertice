"""Node 5: the 22 controls (ROADMAP section 8).

Deterministic checks that every candidate must pass before it can be a COMPRAR.
They veto the AI, never the other way round (principle 5).

Each control answers ``pass``, ``warn`` or ``fail`` with a reason in Spanish.

* ``fail`` always blocks the candidate.
* Type **R** controls answer ``warn`` when triggered: the position size is halved
  and the AI is told. Each one halves again, down to a quarter.
* A ``warn`` on a type **B** control is only a note.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from core import clock, market_calendar
from core.settings import AppConfig
from engine.data_engine import Quality
from engine.portfolio import Portfolio
from engine.risk_profile import RiskProfile
from engine.schemas import EconEvent, NewsItem, Quote
from engine.screener import Candidate
from engine.sentiment import SEVERE_CATEGORIES, SPECIAL_CATEGORIES
from engine.trade_plan import TradePlan

Result = Literal["pass", "warn", "fail"]
Kind = Literal["B", "R"]
Effect = Literal["none", "reduce", "block"]

MIN_SIZE_MULTIPLIER = 0.25


@dataclass(frozen=True)
class ControlContext:
    """Everything the controls look at for one candidate."""

    as_of: datetime
    config: AppConfig
    profile: RiskProfile
    regime: str
    candidate: Candidate
    plan: TradePlan | None
    portfolio: Portfolio
    quality: Quality | None
    quote: Quote | None = None
    sector_etf: str | None = None
    market_cap_usd: float | None = None
    sic_listed: bool = False
    econ_events: Sequence[EconEvent] = ()
    # News of this ticker only.
    news: Sequence[NewsItem] = ()
    # 60-day correlation with each open position, by ticker.
    correlations: Mapping[str, float] = field(default_factory=dict)
    # Today's opening price, once the market has opened (the 09:45 recheck).
    opening_price: float | None = None

    def metric(self, name: str) -> float:
        value = self.candidate.metrics.get(name)
        return float("nan") if value is None else float(value)


@dataclass(frozen=True)
class ControlResult:
    id: str
    name: str
    kind: Kind
    result: Result
    reason: str

    @property
    def effect(self) -> Effect:
        if self.result == "fail":
            return "block"
        if self.result == "warn" and self.kind == "R":
            return "reduce"
        return "none"


Check = Callable[[ControlContext], tuple[Result, str]]


def _money(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:,.1f} mil millones"
    return f"${value / 1e6:,.0f} millones"


def estimated_spread_pct(price: float, dollar_volume: float) -> float:
    """Rough bid-ask spread when there is no live quote: one cent of tick plus a
    term that shrinks with liquidity. About 0.25% at $20M traded per day and
    0.04% at $1,000M."""
    if not price > 0 or not dollar_volume > 0:
        return float("nan")
    return 0.01 / price * 100 + 1.1 / math.sqrt(dollar_volume / 1e6)


def c01_fresh_data(ctx: ControlContext) -> tuple[Result, str]:
    if ctx.quality is None or not ctx.quality.can_buy:
        score = ctx.quality.score if ctx.quality else 0.0
        issues = (
            "; ".join(ctx.quality.issues) if ctx.quality and ctx.quality.issues else "sin datos"
        )
        return "fail", f"calidad de datos {score:.2f}, mínimo 0.80: {issues}"
    if market_calendar.market_status(ctx.as_of) == "open":
        limit = ctx.config.data.stale.quote_max_age_s
        if ctx.quote is None:
            return "warn", "mercado abierto y sin precio en vivo de este ticker"
        age = (ctx.as_of - ctx.quote.ts).total_seconds()
        if age > limit:
            return "fail", f"el precio tiene {age:.0f} s; el máximo en sesión es {limit} s"
    return "pass", f"calidad de datos {ctx.quality.score:.2f}"


def c02_trading_day(ctx: ControlContext) -> tuple[Result, str]:
    day = clock.market_date(ctx.as_of)
    if not market_calendar.is_trading_day(day):
        return "fail", f"NYSE no abre el {day:%Y-%m-%d}"
    return "pass", "NYSE abre hoy"


def c03_min_price(ctx: ControlContext) -> tuple[Result, str]:
    price, minimum = ctx.metric("close"), ctx.config.universe.min_price
    if not price >= minimum:
        return "fail", f"precio ${price:,.2f}, menor al mínimo de ${minimum:,.2f}"
    return "pass", f"precio ${price:,.2f}"


def c04_liquidity(ctx: ControlContext) -> tuple[Result, str]:
    volume, minimum = ctx.metric("dollar_volume_20d"), ctx.config.universe.min_dollar_volume_20d
    if not volume >= minimum:
        shown = _money(volume) if not math.isnan(volume) else "desconocido"
        return "fail", f"negocia {shown} al día; el mínimo es {_money(minimum)}"
    return "pass", f"negocia {_money(volume)} al día"


def c05_market_cap(ctx: ControlContext) -> tuple[Result, str]:
    minimum = ctx.config.universe.min_market_cap
    if ctx.market_cap_usd is None or math.isnan(ctx.market_cap_usd):
        return "fail", "capitalización desconocida"
    if ctx.market_cap_usd < minimum:
        return (
            "fail",
            f"capitalización {_money(ctx.market_cap_usd)}; el mínimo es {_money(minimum)}",
        )
    return "pass", f"capitalización {_money(ctx.market_cap_usd)}"


def c06_spread(ctx: ControlContext) -> tuple[Result, str]:
    limit = ctx.config.controls.max_spread_pct
    live = ctx.quote.spread_pct if ctx.quote is not None else None
    if live is not None:
        spread, label = live, "spread"
    else:
        spread = estimated_spread_pct(ctx.metric("close"), ctx.metric("dollar_volume_20d"))
        label = "spread estimado"
    if not spread <= limit:
        shown = f"{spread:.2f}%" if not math.isnan(spread) else "desconocido"
        return "fail", f"{label} {shown}; el máximo es {limit:.2f}%"
    return "pass", f"{label} {spread:.2f}%"


def c07_available_in_market(ctx: ControlContext) -> tuple[Result, str]:
    if not ctx.config.universe.sic_only:
        return "pass", "filtro SIC apagado"
    if not ctx.sic_listed:
        return "fail", "no está en la lista de emisoras del SIC"
    return "pass", "listada en el SIC"


def c08_earnings(ctx: ControlContext) -> tuple[Result, str]:
    days, block = ctx.metric("days_to_earnings"), ctx.config.controls.earnings_block_days
    if days <= block:
        when = "hoy" if days == 0 else f"en {days:.0f} días hábiles"
        return "fail", f"reporta resultados {when}; no se compra a {block} días o menos"
    if math.isnan(days):
        return "pass", "sin resultados programados en la ventana conocida"
    return "pass", f"reporta resultados en {days:.0f} días hábiles"


def c09_macro_event(ctx: ControlContext) -> tuple[Result, str]:
    window = timedelta(hours=ctx.config.controls.macro_event_block_hours)
    upcoming = [
        event
        for event in ctx.econ_events
        if event.impact == "high" and ctx.as_of <= event.scheduled_at <= ctx.as_of + window
    ]
    if upcoming:
        first = min(upcoming, key=lambda event: event.scheduled_at)
        return (
            "warn",
            f"{first.name} a las {clock.dual_label(first.scheduled_at)}: tamaño a la mitad",
        )
    return (
        "pass",
        f"sin dato macro de alto impacto en las próximas {window.total_seconds() / 3600:g} horas",
    )


def c10_regime(ctx: ControlContext) -> tuple[Result, str]:
    if ctx.regime != "RIESGO_ALTO":
        return "pass", f"régimen {ctx.regime.replace('_', ' ')}"
    minimum = ctx.config.controls.high_risk_min_score
    if ctx.candidate.score < minimum:
        return "fail", f"RIESGO ALTO: puntaje {ctx.candidate.score:.0f}, se exige {minimum:.0f}"
    return "warn", f"RIESGO ALTO: puntaje {ctx.candidate.score:.0f}, tamaño a la mitad"


def c11_gap(ctx: ControlContext) -> tuple[Result, str]:
    if ctx.opening_price is None or ctx.plan is None:
        return "pass", "se revisa con el precio de apertura"
    gap_atr = (ctx.opening_price - ctx.plan.reference) / ctx.plan.atr
    limit = ctx.config.controls.max_gap_atr
    if gap_atr > limit:
        return (
            "fail",
            f"abrió {gap_atr:.1f} ATR arriba del cierre; el máximo es {limit:g}: no perseguir",
        )
    return "pass", f"abrió a {gap_atr:+.1f} ATR del cierre"


def c12_extension(ctx: ControlContext) -> tuple[Result, str]:
    extension, limit = ctx.metric("extension_atr"), ctx.config.controls.max_extension_atr
    if not extension <= limit:
        shown = f"{extension:.1f}" if not math.isnan(extension) else "desconocido"
        return "fail", f"precio a {shown} ATR sobre su promedio de 20 días; el máximo es {limit:g}"
    return "pass", f"precio a {extension:+.1f} ATR de su promedio de 20 días"


def c13_reward_risk(ctx: ControlContext) -> tuple[Result, str]:
    minimum = ctx.config.controls.min_reward_r
    if ctx.plan is None:
        return "fail", "sin niveles para calcular objetivo y stop"
    if ctx.plan.reward_r < minimum:
        return "fail", (
            f"el primer objetivo (${ctx.plan.targets[0]:,.2f}) está a {ctx.plan.reward_r:.1f}R; "
            f"el mínimo es {minimum:g}R"
        )
    return "pass", f"primer objetivo a {ctx.plan.reward_r:.1f}R"


def c14_stop(ctx: ControlContext) -> tuple[Result, str]:
    low, high = ctx.config.controls.stop_min_atr, ctx.config.controls.stop_max_atr
    if ctx.plan is None:
        return "fail", "sin niveles para colocar el stop"
    if ctx.plan.stop_level is None:
        return "fail", f"no hay un nivel técnico entre {low:g} y {high:g} ATR debajo del precio"
    # A hair of tolerance: 48.25 - 47.45 is 0.7999999999999972 in floating point.
    if not low - 1e-9 <= ctx.plan.stop_atr <= high + 1e-9:
        return "fail", f"stop a {ctx.plan.stop_atr:.1f} ATR; debe quedar entre {low:g} y {high:g}"
    return "pass", f"stop a {ctx.plan.stop_atr:.1f} ATR, debajo del {ctx.plan.stop_level_name}"


def c15_max_positions(ctx: ControlContext) -> tuple[Result, str]:
    count, limit = len(ctx.portfolio.holdings), ctx.profile.max_positions
    if count >= limit:
        return "fail", f"ya hay {count} posiciones abiertas; el límite es {limit}"
    return "pass", f"{count} de {limit} posiciones abiertas"


def c16_sector(ctx: ControlContext) -> tuple[Result, str]:
    if ctx.sector_etf is None:
        return "pass", "sector desconocido: no se puede medir concentración"
    controls = ctx.config.controls
    count = ctx.portfolio.sector_count(ctx.sector_etf)
    if count >= controls.max_sector_positions:
        return "fail", (
            f"ya hay {count} posiciones del sector {ctx.sector_etf}; "
            f"el máximo es {controls.max_sector_positions}"
        )
    capital = ctx.portfolio.capital
    share = 100 * ctx.portfolio.sector_value(ctx.sector_etf) / capital if capital > 0 else 0.0
    if share >= controls.max_sector_pct:
        return "fail", (
            f"el sector {ctx.sector_etf} ya es {share:.0f}% del capital; "
            f"el máximo es {controls.max_sector_pct:g}%"
        )
    return "pass", f"{count} posiciones y {share:.0f}% del capital en {ctx.sector_etf}"


def c17_correlation(ctx: ControlContext) -> tuple[Result, str]:
    limit = ctx.config.controls.max_correlation
    known = {t: c for t, c in ctx.correlations.items() if not math.isnan(c)}
    if not known:
        return "pass", "sin posiciones abiertas con las que comparar"
    ticker, highest = max(known.items(), key=lambda item: item[1])
    if highest > limit:
        return (
            "warn",
            f"correlación {highest:.2f} con {ticker} (máximo {limit:.2f}): tamaño a la mitad",
        )
    return "pass", f"correlación máxima {highest:.2f}, con {ticker}"


def c18_cash(ctx: ControlContext) -> tuple[Result, str]:
    price = ctx.plan.entry_max if ctx.plan is not None else ctx.metric("close")
    minimum = price * (0.01 if ctx.config.capital.fractional_shares else 1)
    if not ctx.portfolio.cash >= minimum:
        unit = "0.01 acciones" if ctx.config.capital.fractional_shares else "1 acción"
        return "fail", f"efectivo ${ctx.portfolio.cash:,.2f}; {unit} cuesta ${minimum:,.2f}"
    return "pass", f"efectivo disponible ${ctx.portfolio.cash:,.2f}"


def c19_cooldown(ctx: ControlContext) -> tuple[Result, str]:
    stopped = ctx.portfolio.stopped_out.get(ctx.candidate.ticker)
    days = ctx.config.controls.cooldown_days_after_stop
    if stopped is None:
        return "pass", "sin salida reciente por stop"
    today = clock.market_date(ctx.as_of)
    elapsed = len(market_calendar.trading_days_between(stopped, today)) - 1
    if elapsed < days:
        return "fail", (
            f"salió por stop el {stopped:%Y-%m-%d}, hace {elapsed} días hábiles; se esperan {days}"
        )
    return "pass", f"salió por stop hace {elapsed} días hábiles"


def c20_loss_brake(ctx: ControlContext) -> tuple[Result, str]:
    controls = ctx.config.controls
    if ctx.portfolio.day_pnl_pct <= -controls.daily_loss_stop_pct:
        return "fail", (
            f"pérdida del día {ctx.portfolio.day_pnl_pct:.1f}%; "
            f"el freno es {controls.daily_loss_stop_pct:g}%"
        )
    if ctx.portfolio.week_pnl_pct <= -controls.weekly_loss_stop_pct:
        return "fail", (
            f"pérdida de la semana {ctx.portfolio.week_pnl_pct:.1f}%; "
            f"el freno es {controls.weekly_loss_stop_pct:g}%"
        )
    return "pass", (
        f"día {ctx.portfolio.day_pnl_pct:+.1f}%, semana {ctx.portfolio.week_pnl_pct:+.1f}%"
    )


def _recent_news(ctx: ControlContext, categories: frozenset[str]) -> list[NewsItem]:
    since = ctx.as_of - timedelta(hours=ctx.config.controls.severe_news_hours)
    return [
        item
        for item in ctx.news
        if item.category in categories and since < item.published_at <= ctx.as_of
    ]


_CATEGORY_LABELS = {
    "investigation": "investigación regulatoria",
    "bankruptcy": "quiebra",
    "offering": "oferta de acciones",
    "downgrade": "rebaja de recomendación",
    "halt": "suspensión de cotización",
    "merger": "fusión o compra con precio fijado",
}


def c21_severe_news(ctx: ControlContext) -> tuple[Result, str]:
    severe = _recent_news(ctx, SEVERE_CATEGORIES)
    hours = ctx.config.controls.severe_news_hours
    if severe:
        latest = max(severe, key=lambda item: item.published_at)
        return "fail", f"{_CATEGORY_LABELS[latest.category]}: «{latest.headline}»"
    return "pass", f"sin noticias graves en {hours} horas"


def c22_special_situation(ctx: ControlContext) -> tuple[Result, str]:
    special = _recent_news(ctx, SPECIAL_CATEGORIES)
    if special:
        latest = max(special, key=lambda item: item.published_at)
        return "fail", f"{_CATEGORY_LABELS[latest.category]}: «{latest.headline}»"
    return "pass", "sin suspensión ni fusión con precio fijado"


@dataclass(frozen=True)
class ControlSpec:
    id: str
    name: str
    kind: Kind
    check: Check


CONTROLS: tuple[ControlSpec, ...] = (
    ControlSpec("C01", "Datos frescos", "B", c01_fresh_data),
    ControlSpec("C02", "Día hábil", "B", c02_trading_day),
    ControlSpec("C03", "Precio mínimo", "B", c03_min_price),
    ControlSpec("C04", "Liquidez", "B", c04_liquidity),
    ControlSpec("C05", "Tamaño de empresa", "B", c05_market_cap),
    ControlSpec("C06", "Spread", "B", c06_spread),
    ControlSpec("C07", "Disponible en tu mercado", "B", c07_available_in_market),
    ControlSpec("C08", "Resultados", "B", c08_earnings),
    ControlSpec("C09", "Evento macro", "R", c09_macro_event),
    ControlSpec("C10", "Régimen", "R", c10_regime),
    ControlSpec("C11", "Gap", "B", c11_gap),
    ControlSpec("C12", "Extensión", "B", c12_extension),
    ControlSpec("C13", "Recompensa vs riesgo", "B", c13_reward_risk),
    ControlSpec("C14", "Stop razonable", "B", c14_stop),
    ControlSpec("C15", "Máximo de posiciones", "B", c15_max_positions),
    ControlSpec("C16", "Concentración sectorial", "B", c16_sector),
    ControlSpec("C17", "Correlación", "R", c17_correlation),
    ControlSpec("C18", "Efectivo", "B", c18_cash),
    ControlSpec("C19", "Enfriamiento", "B", c19_cooldown),
    ControlSpec("C20", "Freno de pérdidas", "B", c20_loss_brake),
    ControlSpec("C21", "Noticia grave", "B", c21_severe_news),
    ControlSpec("C22", "Situación especial", "B", c22_special_situation),
)


def run_controls(ctx: ControlContext) -> list[ControlResult]:
    """All 22 controls for one candidate, in order."""
    results = []
    for spec in CONTROLS:
        result, reason = spec.check(ctx)
        results.append(ControlResult(spec.id, spec.name, spec.kind, result, reason))
    return results


def is_blocked(results: Sequence[ControlResult]) -> bool:
    return any(result.effect == "block" for result in results)


def blocking_reasons(results: Sequence[ControlResult]) -> list[str]:
    return [f"{r.id} {r.name}: {r.reason}" for r in results if r.effect == "block"]


def size_multiplier(results: Sequence[ControlResult]) -> float:
    """Half the size per triggered type-R control, never below a quarter."""
    reductions = sum(1 for result in results if result.effect == "reduce")
    return max(MIN_SIZE_MULTIPLIER, 0.5**reductions)
