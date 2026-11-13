"""The replay report: ideas, shadow trades and the result against SPY, in Spanish."""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from engine.exits import ShadowPosition
from replay.engine import ReplayResult


@dataclass(frozen=True)
class TradeStats:
    count: int
    wins: int
    win_rate: float
    average_r: float
    total_r: float
    profit_factor: float
    best_r: float
    worst_r: float
    net_pnl: float


def trade_stats(trades: list[ShadowPosition]) -> TradeStats:
    if not trades:
        return TradeStats(0, 0, math.nan, math.nan, 0.0, math.nan, math.nan, math.nan, 0.0)
    results = [t.net_r for t in trades]
    gains = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    losses = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    return TradeStats(
        count=len(trades),
        wins=wins,
        win_rate=100 * wins / len(trades),
        average_r=sum(results) / len(results),
        total_r=sum(results),
        profit_factor=gains / losses if losses > 0 else math.inf,
        best_r=max(results),
        worst_r=min(results),
        net_pnl=sum(t.net_pnl for t in trades),
    )


def max_drawdown_pct(values: list[float]) -> float:
    """Largest fall from a previous peak, in percent (0 when it never fell)."""
    peak, worst = -math.inf, 0.0
    for value in values:
        if math.isnan(value):
            continue
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak * 100)
    return worst


def _pct(first: float, last: float) -> float:
    return (last / first - 1) * 100


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _stats_row(label: str, stats: TradeStats) -> list[str]:
    if stats.count == 0:
        return [label, "0", "—", "—", "—", "—"]
    factor = "∞" if math.isinf(stats.profit_factor) else f"{stats.profit_factor:.2f}"
    return [
        label,
        str(stats.count),
        f"{stats.win_rate:.0f}%",
        f"{stats.average_r:+.2f}R",
        factor,
        f"${stats.net_pnl:+,.0f}",
    ]


def _verdict_source(result: ReplayResult) -> str:
    if result.ai == "simulated":
        return (
            f"Veredictos con la IA simulada ({result.ai_calls} consultas en {result.ai_days} días)."
        )
    return "Veredictos por reglas, sin IA."


def _verdict_caveat(result: ReplayResult) -> str:
    if result.ai == "simulated":
        return (
            "- **IA simulada**: responde con reglas fijas a partir del dossier. Prueba que el "
            "dossier, la validación y el presupuesto funcionan; no dice nada del criterio de la "
            "IA real, que solo se puede medir hacia adelante."
        )
    return (
        "- **Veredictos por reglas**: falta el criterio de la IA, que solo se puede medir "
        "hacia adelante."
    )


def render(result: ReplayResult) -> str:
    """The report as Markdown."""
    days = result.days
    equity = [d.equity for d in days]
    spy = [d.spy_equity for d in days]
    system_return = _pct(result.capital, equity[-1])
    spy_return = _pct(result.capital, spy[-1])
    stats = trade_stats(result.trades)
    verdicts = Counter(idea.verdict for idea in result.ideas)
    outcomes = Counter(
        "entró" if i.outcome == "entró" else i.outcome.split(":")[0]
        for i in result.ideas
        if i.verdict == "COMPRAR"
    )
    regimes = Counter(d.regime for d in days)

    lines = [
        f"# Replay {result.start} a {result.end}",
        "",
        _verdict_source(result)
        + " Cada idea COMPRAR se siguió como sombra con las reglas de salida, sobre barras "
        "diarias y con comisiones, IVA y deslizamiento.",
        "",
        "## Resultado contra SPY",
        "",
        *_table(
            ["", "Sistema (sombras)", "SPY"],
            [
                ["Capital inicial", f"${result.capital:,.0f}", f"${result.capital:,.0f}"],
                ["Capital final", f"${equity[-1]:,.0f}", f"${spy[-1]:,.0f}"],
                ["Rendimiento", f"{system_return:+.2f}%", f"{spy_return:+.2f}%"],
                [
                    "Caída máxima",
                    f"{max_drawdown_pct(equity):.2f}%",
                    f"{max_drawdown_pct(spy):.2f}%",
                ],
            ],
        ),
        "",
        f"Diferencia contra SPY: {system_return - spy_return:+.2f} puntos porcentuales.",
        "",
        "## Ideas",
        "",
        f"- Días hábiles: {len(days)}. Ideas listadas: {len(result.ideas)} "
        f"(mínimo por día: {min(d.listed for d in days)}).",
        f"- COMPRAR: {verdicts['COMPRAR']} · VIGILAR: {verdicts['VIGILAR']} · "
        f"NO HOY: {verdicts['NO_HOY']}.",
        f"- Días sin ninguna idea en COMPRAR: {sum(1 for d in days if d.buy == 0)}.",
        "- De las ideas COMPRAR: "
        + (", ".join(f"{count} {label}" for label, count in outcomes.most_common()) or "ninguna")
        + ".",
        "- Régimen: "
        + ", ".join(
            f"{name.replace('_', ' ')} {count} días" for name, count in regimes.most_common()
        )
        + ".",
        "",
        "## Sombras (operaciones virtuales)",
        "",
    ]
    if stats.count == 0:
        lines.append("No se abrió ninguna sombra en el periodo.")
    else:
        lines += _table(
            ["Grupo", "Operaciones", "Ganadoras", "Promedio", "Profit factor", "Resultado neto"],
            [_stats_row("Todas", stats)],
        )
        lines += [
            "",
            f"Mejor: {stats.best_r:+.2f}R · Peor: {stats.worst_r:+.2f}R · "
            f"Suma: {stats.total_r:+.2f}R.",
            "",
        ]
        by_setup: dict[str, list[ShadowPosition]] = defaultdict(list)
        by_regime: dict[str, list[ShadowPosition]] = defaultdict(list)
        for trade in result.trades:
            by_setup[trade.setup or "sin setup"].append(trade)
            by_regime[trade.regime_at_entry.replace("_", " ")].append(trade)
        lines += ["### Por setup", ""]
        lines += _table(
            ["Setup", "Operaciones", "Ganadoras", "Promedio", "Profit factor", "Resultado neto"],
            [_stats_row(name, trade_stats(group)) for name, group in sorted(by_setup.items())],
        )
        lines += ["", "### Por régimen al entrar", ""]
        lines += _table(
            ["Régimen", "Operaciones", "Ganadoras", "Promedio", "Profit factor", "Resultado neto"],
            [_stats_row(name, trade_stats(group)) for name, group in sorted(by_regime.items())],
        )
        reasons = Counter(trade.exit_reason for trade in result.trades)
        lines += ["", "### Motivo de salida", ""]
        lines += _table(
            ["Motivo", "Operaciones"],
            [[reason, str(count)] for reason, count in reasons.most_common()],
        )
    lines += ["", "## Controles que más bloquearon", ""]
    if result.blockers:
        lines += _table(
            ["Control", "Ideas bloqueadas"],
            [[name, str(count)] for name, count in result.blockers.most_common(10)],
        )
    else:
        lines.append("Ningún control bloqueó ideas.")
    lines += [
        "",
        "## Tiempos",
        "",
        f"- Cargar los datos: {result.load_seconds:.1f} s.",
        f"- Por día: {result.seconds_per_day:.1f} s en promedio; el más lento, "
        f"{result.slowest_day_seconds:.1f} s.",
        "",
        "## Cómo leer este reporte",
        "",
        "- **No demuestra una ventaja.** Sirve para probar que todas las piezas funcionan juntas.",
        "- **El universo es el de hoy.** Solo incluye empresas que hoy cotizan y son líquidas, "
        "lo que favorece los resultados pasados.",
        "- **Capitalización y sector son los actuales**, no los de cada fecha.",
        "- **Sin noticias ni sorpresas de EPS históricas**: el componente de catalizador casi no "
        "puntúa y los controles de noticias no bloquean.",
        "- **Barras diarias**: no se conoce el orden dentro del día. Si el stop y un objetivo se "
        "tocaron el mismo día, se asume que primero saltó el stop.",
        "- **Motor de riesgo**: el tamaño usa la convicción (sin IA, la mitad del riesgo base), "
        "la racha de la cuenta y el filtro de expectativa de +0.15R tras costos. La pausa por "
        "caída de 10% no se simula, porque solo el usuario puede levantarla.",
        _verdict_caveat(result),
    ]
    return "\n".join(lines) + "\n"


_TRADE_COLUMNS = [
    "ticker",
    "setup",
    "regime",
    "score",
    "entry_day",
    "entry_price",
    "shares",
    "initial_stop",
    "target_1",
    "target_2",
    "exit_day",
    "exit_reason",
    "days_held",
    "net_pnl",
    "net_r",
]


def write(result: ReplayResult, directory: Path) -> Path:
    """Write report.md, trades.csv, ideas.csv and equity.csv. Returns the report path."""
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "report.md"
    report.write_text(render(result), encoding="utf-8")
    with (directory / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_TRADE_COLUMNS)
        for t in result.trades:
            writer.writerow(
                [
                    t.ticker,
                    t.setup,
                    t.regime_at_entry,
                    t.score,
                    t.entry_day,
                    round(t.entry_price, 4),
                    t.shares,
                    t.initial_stop,
                    t.targets[0],
                    t.targets[1],
                    t.exit_day,
                    t.exit_reason,
                    t.days_held,
                    round(t.net_pnl, 2),
                    round(t.net_r, 3),
                ]
            )
    with (directory / "ideas.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "ticker", "verdict", "setup", "score", "reason", "outcome"])
        for i in result.ideas:
            writer.writerow([i.day, i.ticker, i.verdict, i.setup, i.score, i.reason, i.outcome])
    with (directory / "equity.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "regime", "listed", "buy", "watch", "rejected", "entered",
                         "open_positions", "cash", "equity", "spy_equity"])  # fmt: skip
        for d in result.days:
            writer.writerow([d.day, d.regime, d.listed, d.buy, d.watch, d.rejected, d.entered,
                             d.open_positions, round(d.cash, 2), round(d.equity, 2),
                             round(d.spy_equity, 2)])  # fmt: skip
    return report
