"""What the system says through Telegram: the plan of the day, the sell alerts and
the reminders, as plain text in Spanish with their buttons.

Pure functions: they turn data already saved (the plan as ``plan_store`` returns
it, the data of an alert) into messages. Nothing here talks to Telegram.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from core import clock

MAX_LENGTH = 3900  # Telegram cuts at 4096 characters
_WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_URGENCY_MARK = {"ALTA": "🔴", "MEDIA": "🟠", "BAJA": "🟢"}

# Prefixes of the data a button carries back (Telegram allows 64 bytes).
BUY, DECLINE, DECLINE_REASON = "buy", "no", "why"
SOLD, STOP_RAISED, IGNORE, IGNORE_REASON = "sold", "stop", "ign", "ignw"

DECLINE_REASONS = ("sin efectivo", "no me convenció", "llegué tarde", "otro")
IGNORE_REASONS = ("prefiero esperar", "no estoy de acuerdo", "ya lo resolví", "otro")


@dataclass(frozen=True)
class Button:
    text: str
    data: str


@dataclass(frozen=True)
class OutMessage:
    text: str
    # Rows of buttons under the message.
    buttons: tuple[tuple[Button, ...], ...] = ()


def _day_label(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]} {day.year}"


def _money(value: float) -> str:
    return f"−${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def signed(value: float, suffix: str = "", digits: int = 2) -> str:
    sign = "−" if value < 0 else "+"
    return f"{sign}{abs(value):.{digits}f}{suffix}"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LENGTH else text[: MAX_LENGTH - 1].rstrip() + "…"


def idea_buttons(idea_id: int) -> tuple[tuple[Button, ...], ...]:
    return (
        (
            Button("La compré", f"{BUY}:{idea_id}"),
            Button("No la compré", f"{DECLINE}:{idea_id}"),
        ),
    )


def reason_buttons(
    prefix: str, target_id: int, reasons: Sequence[str]
) -> tuple[tuple[Button, ...], ...]:
    buttons = [
        Button(reason.capitalize(), f"{prefix}:{target_id}:{index}")
        for index, reason in enumerate(reasons)
    ]
    return tuple(tuple(buttons[i : i + 2]) for i in range(0, len(buttons), 2))


def idea_message(idea: Mapping[str, Any], capital: float | None = None) -> OutMessage:
    """One purchase idea, as the roadmap's example shows it."""
    entry, targets, targets_r = idea["entry"], idea["targets"], idea.get("targets_r") or []
    tags = [idea["setup"] or "sin setup", f"puntaje {idea['score']:.0f}"]
    if idea.get("probability") is not None:
        tags.append(f"prob. {idea['probability']:.2f}")
    elif idea.get("no_ai"):
        tags.append("sin IA")
    lines = [f"COMPRAR  {idea['ticker']}  ({' · '.join(tags)})"]
    if idea.get("name"):
        lines.append(str(idea["name"]))
    lines.append(
        f"Entrada: límite {entry['min']:,.2f}–{entry['max']:,.2f} "
        f"(no perseguir sobre {entry['no_chase_above']:,.2f})"
    )
    reached = [
        f"{price:,.2f}" + (f" (+{r:.1f}R)" if index < len(targets_r) else "")
        for index, (price, r) in enumerate(zip(targets, [*targets_r, 0, 0], strict=False))
    ]
    lines.append(f"Stop: {idea['stop']:,.2f} (−1R)   Objetivos: {' / '.join(reached)}")
    share = (
        f" ({100 * idea['amount_usd'] / capital:.0f}% del capital)"
        if capital and capital > 0
        else ""
    )
    lines.append(
        f"Tamaño: {idea['shares']:g} acciones ≈ ${idea['amount_usd']:,.0f}{share} "
        f"· riesgo ${idea['risk_usd']:,.0f}"
    )
    if idea.get("ev_r") is not None:
        lines.append(f"Expectativa: {signed(idea['ev_r'], 'R')} después de costos")
    if idea.get("thesis"):
        lines.append(f"Tesis: {idea['thesis']}")
    elif idea.get("notes"):
        lines.append(f"Por qué: {'; '.join(idea['notes'])}")
    if idea.get("risks"):
        lines.append(f"Riesgo: {'; '.join(idea['risks'][:2])}")
    if idea.get("warnings"):
        lines.append(f"Avisos: {'; '.join(idea['warnings'])}")
    return OutMessage(_clip("\n".join(lines)), idea_buttons(idea["id"]))


def plan_messages(
    plan: Mapping[str, Any], *, system_name: str, capital: float | None = None
) -> list[OutMessage]:
    """The plan of the day: a header, one message per purchase (with its buttons) and
    a summary of what is only watched or not for today."""
    day = date.fromisoformat(plan["plan_date"])
    ideas = plan["ideas"]
    buys = [i for i in ideas if i["verdict"] == "COMPRAR"]
    watch = [i for i in ideas if i["verdict"] == "VIGILAR"]
    rejected = [i for i in ideas if i["verdict"] == "NO_HOY"]

    header = [
        f"{system_name} · Plan del día · {_day_label(day)} · Régimen "
        f"{str(plan['regime']).replace('_', ' ')}"
    ]
    if plan.get("market_reading"):
        header.append(plan["market_reading"])
    ai = plan.get("ai") or {}
    if ai.get("reading"):
        header.append(f"Lectura de la IA: {ai['reading']}")
    elif ai.get("note"):
        header.append(f"Veredictos por reglas ({ai.get('label')}): {ai['note']}.")
    events = plan.get("events_today") or []
    if events:
        listed = ", ".join(
            f"{e['name']} {clock.dual_label(datetime.fromisoformat(e['at']))}" for e in events
        )
        header.append(f"Eventos de hoy: {listed}.")
    brake = plan.get("brake") or {}
    if brake.get("state") and brake["state"] != "NORMAL":
        label = "PAUSA" if brake["state"] == "PAUSA" else "SOLO VIGILAR"
        header.append(f"{label}: {'; '.join(brake.get('reasons') or [])}.")
    if plan.get("streak_note"):
        header.append(f"Tamaño a la mitad por racha: {plan['streak_note']}.")
    header.append(
        f"{len(buys)} para COMPRAR · {len(watch)} para VIGILAR · {len(rejected)} NO HOY."
        if buys
        else "Hoy no hay ninguna idea en COMPRAR: ninguna supera todos los controles. "
        f"{len(watch)} para VIGILAR · {len(rejected)} NO HOY."
    )
    messages = [OutMessage(_clip("\n".join(header)))]
    messages += [idea_message(idea, capital) for idea in buys]

    rest: list[str] = []
    for idea in watch:
        setup = idea["setup"] or "sin setup"
        line = f"VIGILAR  {idea['ticker']} ({setup} · puntaje {idea['score']:.0f})"
        if idea.get("reason"):
            line += f": {idea['reason']}"
        if idea.get("watch_trigger"):
            line += f". Se activa si: {idea['watch_trigger']}"
        rest.append(line)
    for idea in rejected:
        rest.append(
            f"NO HOY  {idea['ticker']} ({idea['setup'] or 'sin setup'} · puntaje "
            f"{idea['score']:.0f}): {idea.get('reason') or 'sin motivo registrado'}"
        )
    for rotation in plan.get("rotations") or []:
        rest.append(f"Rotación propuesta: {rotation['text']}")
    if buys:
        rest.append(
            "Cómo entrar: orden límite dentro de la zona, nunca a mercado; no entres en los "
            "primeros 15 minutos; si puedes, deja también la orden stop en el precio indicado."
        )
    if rest:
        messages.append(OutMessage(_clip("\n\n".join(rest))))
    return messages


def alert_buttons(alert_id: int, kind: str) -> tuple[tuple[Button, ...], ...]:
    sold = Button("Vendí", f"{SOLD}:{alert_id}")
    raised = Button("Ya subí el stop", f"{STOP_RAISED}:{alert_id}")
    ignore = Button("Ignorar", f"{IGNORE}:{alert_id}")
    if kind == "stop":
        return ((raised, ignore),)
    if kind == "either":
        return ((raised, sold), (ignore,))
    return ((sold, ignore),)


def alert_message(
    alert_id: int,
    urgency: str,
    data: Mapping[str, Any],
    *,
    notified_count: int = 0,
    max_repeats: int = 3,
) -> OutMessage:
    """A sell alert: what fired, the price with its source and time, the result so
    far, what to do and what happens otherwise."""
    title = f"{_URGENCY_MARK.get(urgency, '')} {urgency} · {data['ticker']} · {data['rule_name']}"
    if notified_count:
        title = f"Recordatorio {notified_count} de {max_repeats} · {title.strip()}"
    at = datetime.fromisoformat(data["price_at"])
    lines = [
        title.strip(),
        data["action"],
        f"Motivo: {data['explanation']}.",
        f"Precio: ${data['price']:,.2f} ({data['price_source']}, {clock.dual_label(at)})",
        f"Resultado: {_money(data['pnl_usd'])} · {signed(data['pnl_pct'], '%', 1)} · "
        f"{signed(data['r_now'], 'R')}",
        f"Si no haces nada: {data['consequence']}.",
    ]
    if urgency == "ALTA":
        lines.append("La posición sigue ABIERTA hasta que me digas qué hiciste.")
    return OutMessage(_clip("\n".join(lines)), alert_buttons(alert_id, data.get("kind", "sell")))


def pending_reminder(
    ideas: Sequence[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]]
) -> list[OutMessage]:
    """Reminders of what is waiting for an answer: purchases of today's plan and
    high-urgency alerts."""
    messages = []
    for idea in ideas:
        messages.append(
            OutMessage(
                f"Pendiente: ¿compraste {idea['ticker']}? El plan de hoy la propuso "
                f"({idea['shares']:g} acciones, zona {idea['entry']['min']:,.2f}–"
                f"{idea['entry']['max']:,.2f}). Si no contestas, al cierre se sigue como sombra.",
                idea_buttons(idea["id"]),
            )
        )
    for alert in alerts:
        data = alert["data"]
        messages.append(
            OutMessage(
                f"Pendiente de tu confirmación: {data['ticker']} · {data['rule_name']}. "
                f"{data['action']}. La posición sigue abierta en el sistema.",
                alert_buttons(alert["id"], data.get("kind", "sell")),
            )
        )
    return messages
