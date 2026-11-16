"""Results of what the system proposed: closed trades, their statistics and the
capital curve against SPY (principle 10).

Real trades are the ones the user confirmed; shadows are the ideas they did not
buy, followed virtually. Both are measured the same way: dollars net of costs and
R, the result over what was risked at entry.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import models
from engine.account import fills_of, is_voided, net_result

_SALE_REASON_LABELS = {
    "stop": "saltó el stop",
    "objetivo": "alcanzó objetivo",
    "tiempo": "stop de tiempo",
    "resultados": "resultados: vendida antes del reporte",
    "tesis invalidada": "decisión de la IA",
    "noticia grave": "noticia grave",
}


@dataclass(frozen=True)
class ClosedTrade:
    position_id: int
    idea_id: int
    ticker: str
    is_shadow: bool
    setup: str | None
    regime: str | None
    opened_at: datetime
    closed_at: datetime
    shares: float
    entry: float
    exit_avg: float
    net_usd: float
    r_multiple: float
    # Commissions and modelled costs as a share of the risk taken.
    costs_r: float
    reason: str


@dataclass(frozen=True)
class TradeStats:
    count: int
    wins: int
    win_rate_pct: float | None
    expectancy_r: float | None
    profit_factor: float | None
    total_r: float
    net_usd: float
    best_r: float | None
    worst_r: float | None
    avg_cost_r: float | None

    def as_dict(self) -> dict:
        def clean(value: float | None) -> float | None:
            if value is None or math.isnan(value) or math.isinf(value):
                return None
            return round(value, 3)

        return {
            "count": self.count,
            "wins": self.wins,
            "win_rate_pct": clean(self.win_rate_pct),
            "expectancy_r": clean(self.expectancy_r),
            "profit_factor": clean(self.profit_factor),
            "total_r": round(self.total_r, 3),
            "net_usd": round(self.net_usd, 2),
            "best_r": clean(self.best_r),
            "worst_r": clean(self.worst_r),
            "avg_cost_r": clean(self.avg_cost_r),
        }


def closed_trades(session: Session) -> list[ClosedTrade]:
    """Every closed position, real and shadow, oldest first. Cancelled ones are left out."""
    positions = [
        p
        for p in session.scalars(
            select(models.Position)
            .where(models.Position.state == "CLOSED")
            .order_by(models.Position.closed_at)
        )
        if not is_voided(p)
    ]
    fills = fills_of(session, (p.id for p in positions))
    ideas = {
        idea.id: idea
        for idea in session.scalars(
            select(models.Idea).where(models.Idea.id.in_({p.idea_id for p in positions}))
        )
    }
    runs = {
        run.id: run
        for run in session.scalars(
            select(models.Run).where(models.Run.id.in_({i.run_id for i in ideas.values()}))
        )
    }
    reasons: dict[int, str] = {}
    for confirmation in session.scalars(
        select(models.Confirmation)
        .where(models.Confirmation.kind == "sold")
        .order_by(models.Confirmation.id)
    ):
        reasons[confirmation.position_id] = (confirmation.data or {}).get("reason") or ""

    trades = []
    for position in positions:
        own = fills.get(position.id, [])
        bought = sum(f.qty for f in own if f.side == "buy")
        sold = sum(f.qty for f in own if f.side == "sell")
        if bought <= 0 or sold <= 0:
            continue
        risk = position.initial_r * bought
        net = net_result(own)
        idea = ideas.get(position.idea_id)
        run = runs.get(idea.run_id) if idea is not None else None
        if position.is_shadow:
            reason = (position.tracking or {}).get("exit_reason") or "regla de salida"
        else:
            raw = reasons.get(position.id, "")
            reason = _SALE_REASON_LABELS.get(raw, raw or "decisión tuya")
        trades.append(
            ClosedTrade(
                position_id=position.id,
                idea_id=position.idea_id,
                ticker=position.ticker,
                is_shadow=position.is_shadow,
                setup=idea.setup if idea is not None else None,
                regime=run.regime if run is not None else None,
                opened_at=position.opened_at,
                closed_at=position.closed_at,
                shares=bought,
                entry=position.avg_price,
                exit_avg=sum(f.price * f.qty for f in own if f.side == "sell") / sold,
                net_usd=net,
                r_multiple=net / risk,
                costs_r=sum(f.commission for f in own) / risk,
                reason=reason,
            )
        )
    return trades


def trade_stats(trades: Iterable[ClosedTrade]) -> TradeStats:
    trades = list(trades)
    if not trades:
        return TradeStats(0, 0, None, None, None, 0.0, 0.0, None, None, None)
    results = [t.r_multiple for t in trades]
    gains = sum(t.net_usd for t in trades if t.net_usd > 0)
    losses = -sum(t.net_usd for t in trades if t.net_usd < 0)
    wins = sum(1 for t in trades if t.net_usd > 0)
    return TradeStats(
        count=len(trades),
        wins=wins,
        win_rate_pct=100 * wins / len(trades),
        expectancy_r=sum(results) / len(results),
        profit_factor=gains / losses if losses > 0 else (math.inf if gains > 0 else None),
        total_r=sum(results),
        net_usd=sum(t.net_usd for t in trades),
        best_r=max(results),
        worst_r=min(results),
        avg_cost_r=sum(t.costs_r for t in trades) / len(trades),
    )


def grouped(
    trades: Iterable[ClosedTrade], key: Callable[[ClosedTrade], str | None]
) -> dict[str, TradeStats]:
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        groups[key(trade) or "sin dato"].append(trade)
    return {name: trade_stats(group) for name, group in sorted(groups.items())}


def max_drawdown_pct(values: Iterable[float]) -> float:
    """Largest fall from a previous peak, in percent."""
    peak, worst = -math.inf, 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def equity_series(session: Session) -> list[dict]:
    """The capital at each close, with the three lines the chart shows rebased to
    100 on the first day each one has a value: the real account, the system (real
    plus shadows) and SPY."""
    rows = list(session.scalars(select(models.MetricsDaily).order_by(models.MetricsDaily.day)))
    bases: dict[str, float] = {}
    series = []
    for row in rows:
        point: dict = {"day": row.day.isoformat()}
        for name, value in (
            ("real", row.equity_real),
            ("system", row.equity_system),
            ("spy", row.spy_close),
        ):
            if value is None or value <= 0:
                point[name] = None
                continue
            base = bases.setdefault(name, value)
            point[name] = round(100 * value / base, 3)
        point["equity_real_usd"] = row.equity_real
        series.append(point)
    return series
