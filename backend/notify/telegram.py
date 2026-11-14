"""Telegram: sending the plan and the alerts with buttons, and reading the answers.

Two parts:

* ``TelegramClient`` talks to the Bot API (rate-limited, with retries). The bot token
  is part of the URL, so no URL is ever logged.
* ``TelegramBot`` decides what to send and what a pressed button or a typed answer
  means. It only obeys the chat configured in ``TELEGRAM_CHAT_ID``; anything from
  another chat is dropped.

A button never buys or sells anything (principle 1). "La compré" and "Vendí" ask
for the price and the quantity and record what the user says they did.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Protocol

from sqlalchemy import Engine, select

from collectors.base import CollectorRuntime
from collectors.http import SourceError
from core import clock
from core.logging import get_logger
from core.settings import AppConfig
from db import models
from db.session import session_scope
from engine import account, confirmations, plan_store, states
from engine import watcher as watching
from engine.confirmations import ConfirmationError, NotFound
from engine.states import InvalidTransition
from notify import messages as msg
from notify.messages import OutMessage

log = get_logger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
OFFSET_KEY = "telegram/offset"
AWAITING_KEY = "telegram/awaiting"
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

HELP = (
    "Comandos:\n"
    "/plan — el Plan del día\n"
    "/estado — efectivo, posiciones y alertas abiertas\n"
    "/pendientes — lo que espera tu respuesta\n"
    "Los botones de cada mensaje registran lo que hiciste; yo nunca compro ni vendo."
)


class Transport(Protocol):
    """What the bot needs from Telegram."""

    async def send(self, message: OutMessage) -> int | None: ...

    async def updates(self, offset: int | None) -> list[dict]: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...


class TelegramClient:
    """The Bot API over HTTPS."""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime
        secrets = runtime.secrets
        self._token = secrets.telegram_bot_token.get_secret_value()
        self.chat_id = secrets.telegram_chat_id.get_secret_value()

    async def _call(self, method: str, **params: Any) -> Any | None:
        url = API_URL.format(token=self._token, method=method)
        try:
            response, _ = await self._runtime.client("telegram").get(url, params=params)
            payload = response.json()
        except (SourceError, ValueError) as error:
            log.warning("telegram_failed", method=method, error=self._runtime.scrub(str(error)))
            return None
        if not payload.get("ok"):
            log.warning(
                "telegram_refused",
                method=method,
                error=self._runtime.scrub(str(payload.get("description", ""))),
            )
            return None
        return payload.get("result")

    async def send(self, message: OutMessage) -> int | None:
        """Send a message to the configured chat. Returns its id, or None if it failed."""
        params: dict[str, Any] = {"chat_id": self.chat_id, "text": message.text}
        if message.buttons:
            keyboard = [
                [{"text": button.text, "callback_data": button.data} for button in row]
                for row in message.buttons
            ]
            params["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
        result = await self._call("sendMessage", **params)
        return result.get("message_id") if isinstance(result, dict) else None

    async def updates(self, offset: int | None) -> list[dict]:
        params: dict[str, Any] = {"timeout": 0, "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", **params) or []

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def _numbers(text: str) -> list[float]:
    return [float(match.replace(",", ".")) for match in _NUMBER.findall(text.replace("$", " "))]


class TelegramBot:
    """Sends the plan and the alerts, and turns the user's answers into confirmations."""

    def __init__(
        self, engine: Engine, config: AppConfig, transport: Transport, chat_id: str
    ) -> None:
        self._engine = engine
        self._config = config
        self._transport = transport
        self._chat_id = str(chat_id)

    # --- small persistent state ----------------------------------------------------------

    def _get(self, key: str) -> str | None:
        with session_scope(self._engine) as session:
            row = session.get(models.SystemStatus, key)
            return row.message if row is not None else None

    def _set(self, key: str, value: str | None) -> None:
        with session_scope(self._engine) as session:
            row = session.get(models.SystemStatus, key)
            if row is None:
                row = models.SystemStatus(component=key, status="ok", updated_at=clock.now())
                session.add(row)
            row.message, row.updated_at = value, clock.now()

    def _awaiting(self) -> dict | None:
        raw = self._get(AWAITING_KEY)
        return json.loads(raw) if raw else None

    async def _say(self, text: str, buttons: tuple = ()) -> None:
        await self._transport.send(OutMessage(text, buttons))

    # --- sending -------------------------------------------------------------------------

    async def publish_plan(self, day: date | None = None) -> int:
        """Send the plan in force for ``day``. Returns how many messages were sent."""
        day = day or clock.market_date(clock.now())
        with session_scope(self._engine) as session:
            plan = plan_store.plan_as_dict(session, day)
            capital = account.load_portfolio(session, self._config).capital
        if plan is None:
            await self._say(f"Todavía no hay Plan del día para el {day.isoformat()}.")
            return 1
        sent = 0
        for message in msg.plan_messages(
            plan, system_name=self._config.system.name, capital=capital
        ):
            sent += await self._transport.send(message) is not None
        return sent

    async def send_alerts(self, watcher: watching.Watcher) -> int:
        """Send the alerts that are due (new ones, and repeats of the high-urgency
        ones). An alert only counts as sent when Telegram accepted it."""
        sent = 0
        for alert_id in watcher.due_notifications():
            with session_scope(self._engine) as session:
                alert = session.get(models.Alert, alert_id)
                message = msg.alert_message(
                    alert.id,
                    alert.urgency,
                    alert.data,
                    notified_count=alert.notified_count,
                    max_repeats=self._config.notify.repeat_high_alerts_max,
                )
            if await self._transport.send(message) is not None:
                watcher.mark_notified(alert_id)
                sent += 1
        return sent

    async def remind_pending(self, day: date | None = None) -> int:
        """Remind the user of the purchases of today's plan and the high-urgency
        alerts that still wait for an answer."""
        day = day or clock.market_date(clock.now())
        with session_scope(self._engine) as session:
            ideas = [
                plan_store.idea_as_dict(idea)
                for idea in plan_store.todays_ideas(session, day)
                if idea.verdict == "COMPRAR" and idea.state == "PROPOSED"
            ]
            alerts = [
                {"id": alert.id, "data": alert.data}
                for alert in watching.open_alerts(session)
                if alert.urgency == "ALTA"
            ]
        reminders = msg.pending_reminder(ideas, alerts)
        for message in reminders:
            await self._transport.send(message)
        return len(reminders)

    # --- receiving -----------------------------------------------------------------------

    async def poll_once(self) -> int:
        """Read the updates Telegram holds for the bot and act on each. Returns how
        many were handled."""
        raw = self._get(OFFSET_KEY)
        updates = await self._transport.updates(int(raw) if raw else None)
        for update in updates:
            # The offset moves first: an update that fails is not processed twice.
            self._set(OFFSET_KEY, str(update["update_id"] + 1))
            try:
                await self.handle(update)
            except Exception as error:  # one bad update must not stop the bot
                log.error("telegram_update_failed", error=type(error).__name__)
        return len(updates)

    async def handle(self, update: dict) -> None:
        callback = update.get("callback_query")
        message = (callback or {}).get("message") or update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != self._chat_id:
            log.warning("telegram_ignored_chat")  # somebody else found the bot
            return
        if callback is not None:
            await self._transport.answer_callback(callback["id"])
            await self._on_button(str(callback.get("data") or ""))
        elif message.get("text"):
            await self._on_text(str(message["text"]).strip())

    async def _on_button(self, data: str) -> None:
        action, _, rest = data.partition(":")
        parts = rest.split(":")
        try:
            target = int(parts[0])
        except ValueError:
            return
        choice = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        try:
            if action == msg.BUY:
                await self._ask_purchase(target)
            elif action == msg.DECLINE:
                await self._say(
                    "¿Por qué no la compraste?",
                    msg.reason_buttons(msg.DECLINE_REASON, target, msg.DECLINE_REASONS),
                )
            elif action == msg.DECLINE_REASON and choice is not None:
                await self._decline(target, msg.DECLINE_REASONS[choice])
            elif action == msg.SOLD:
                await self._ask_sale(target)
            elif action == msg.STOP_RAISED:
                await self._stop_raised(target)
            elif action == msg.IGNORE:
                await self._say(
                    "¿Por qué ignoras esta alerta?",
                    msg.reason_buttons(msg.IGNORE_REASON, target, msg.IGNORE_REASONS),
                )
            elif action == msg.IGNORE_REASON and choice is not None:
                await self._ignore(target, msg.IGNORE_REASONS[choice])
        except (ConfirmationError, NotFound) as problem:
            await self._say(f"No se pudo: {problem}.")
        except InvalidTransition as problem:
            await self._say(f"PRECAUCIÓN: {problem}. No se cambió nada; revísalo en el dashboard.")

    async def _on_text(self, text: str) -> None:
        command = text.split()[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/ayuda", "/help"):
            await self._say(HELP)
        elif command == "/plan":
            await self.publish_plan()
        elif command == "/estado":
            await self._status()
        elif command == "/pendientes":
            if await self.remind_pending() == 0:
                await self._say("No hay nada esperando tu respuesta.")
        elif command == "/cancelar":
            self._set(AWAITING_KEY, None)
            await self._say("Cancelado: no registré nada.")
        elif (awaiting := self._awaiting()) is not None:
            await self._on_answer(awaiting, text)
        else:
            await self._say(HELP)

    # --- purchases -----------------------------------------------------------------------

    async def _ask_purchase(self, idea_id: int) -> None:
        with session_scope(self._engine) as session:
            idea = session.get(models.Idea, idea_id)
            if idea is None:
                raise NotFound(f"no existe la idea {idea_id}")
            ticker, shares = idea.ticker, idea.shares or 0
        self._set(AWAITING_KEY, json.dumps({"action": "bought", "idea": idea_id}))
        suggestion = f" El plan sugería {shares:g} acciones." if shares else ""
        await self._say(
            f"{ticker}: ¿a qué precio promedio y cuántas acciones compraste?{suggestion}\n"
            "Responde con dos números: precio y cantidad (por ejemplo: 50.10 9). "
            "Si quieres, agrega la comisión: 50.10 9 1.31. Para no registrar nada: /cancelar"
        )

    async def _decline(self, idea_id: int, reason: str) -> None:
        with states.guarded_session(self._engine) as session:
            idea = confirmations.confirm_declined(session, idea_id, reason=reason)
            ticker = idea.ticker
        await self._say(
            f"Anotado: no compraste {ticker} ({reason}). Al cierre la sigo como sombra si el "
            "precio tocó la zona de entrada."
        )

    # --- alerts --------------------------------------------------------------------------

    async def _ask_sale(self, alert_id: int) -> None:
        with session_scope(self._engine) as session:
            alert = session.get(models.Alert, alert_id)
            if alert is None:
                raise NotFound(f"no existe la alerta {alert_id}")
            if alert.resolved_at is not None:
                raise ConfirmationError(f"esa alerta ya fue resuelta ({alert.resolution})")
            position = session.get(models.Position, alert.position_id)
            position_id, ticker, left = position.id, position.ticker, position.qty_open
            suggested = (alert.data or {}).get("sell_qty") or left
        self._set(
            AWAITING_KEY,
            json.dumps({"action": "sold", "alert": alert_id, "position": position_id}),
        )
        await self._say(
            f"{ticker}: ¿a qué precio y cuántas acciones vendiste? Tienes {left:g}; la alerta "
            f"sugería vender {suggested:g}.\nResponde con dos números: precio y cantidad (por "
            "ejemplo: 48.25 10). Para no registrar nada: /cancelar"
        )

    async def _stop_raised(self, alert_id: int) -> None:
        with states.guarded_session(self._engine) as session:
            alert = session.get(models.Alert, alert_id)
            if alert is None:
                raise NotFound(f"no existe la alerta {alert_id}")
            position = confirmations.confirm_stop_raised(
                session, alert.position_id, alert_id=alert_id
            )
            ticker, stop = position.ticker, position.stop
        await self._say(f"Listo: el stop de {ticker} quedó en ${stop:,.2f}. Ya vigilo ese nivel.")

    async def _ignore(self, alert_id: int, reason: str) -> None:
        with states.guarded_session(self._engine) as session:
            alert = confirmations.ignore_alert(session, alert_id, reason=reason)
            data = alert.data or {}
        await self._say(
            f"Anotado: ignoraste la alerta de {data.get('ticker', '')} · "
            f"{data.get('rule_name', '')} ({reason}). La posición sigue abierta; hoy no insisto "
            "con esa regla."
        )

    # --- typed answers -------------------------------------------------------------------

    async def _on_answer(self, awaiting: dict, text: str) -> None:
        values = _numbers(text)
        if len(values) < 2:
            await self._say(
                "Necesito dos números: precio y cantidad (por ejemplo: 50.10 9). "
                "Para no registrar nada: /cancelar"
            )
            return
        price, qty = values[0], values[1]
        commission = values[2] if len(values) > 2 else 0.0
        try:
            if awaiting["action"] == "bought":
                reply = self._record_purchase(awaiting["idea"], price, qty, commission)
            else:
                reply = self._record_sale(
                    awaiting["position"], awaiting["alert"], price, qty, commission
                )
        except (ConfirmationError, NotFound) as problem:
            await self._say(f"No se pudo: {problem}. Corrige los números o usa /cancelar.")
            return
        except InvalidTransition as problem:
            self._set(AWAITING_KEY, None)
            await self._say(f"PRECAUCIÓN: {problem}. No se cambió nada; revísalo en el dashboard.")
            return
        self._set(AWAITING_KEY, None)
        await self._say(reply)

    def _record_purchase(self, idea_id: int, price: float, qty: float, commission: float) -> str:
        with states.guarded_session(self._engine) as session:
            position = confirmations.confirm_bought(
                session, idea_id, price=price, qty=qty, commission=commission
            )
            return (
                f"Registrado: compraste {qty:g} {position.ticker} a ${price:,.2f}. "
                f"Tienes {position.qty_open:g} a ${position.avg_price:,.2f} en promedio. "
                f"Stop en ${position.stop:,.2f}: arriesgas "
                f"${position.initial_r * position.qty_open:,.2f} "
                f"(R = ${position.initial_r:,.2f} por acción). Te aviso cuándo vender."
            )

    def _record_sale(
        self, position_id: int, alert_id: int, price: float, qty: float, commission: float
    ) -> str:
        with states.guarded_session(self._engine) as session:
            alert = session.get(models.Alert, alert_id)
            still_open = alert is not None and alert.resolved_at is None
            position = confirmations.confirm_sold(
                session,
                position_id,
                price=price,
                qty=qty,
                commission=commission,
                alert_id=alert_id if still_open else None,
            )
            session.flush()
            sale = session.scalars(
                select(models.Confirmation)
                .where(
                    models.Confirmation.kind == "sold",
                    models.Confirmation.position_id == position_id,
                )
                .order_by(models.Confirmation.id.desc())
            ).first()
            data = sale.data
            reply = f"Registrado: vendiste {qty:g} {position.ticker} a ${price:,.2f}. "
            if position.state == "CLOSED":
                net, r = data["position_net_usd"], data["position_r"]
                sign = "−" if net < 0 else "+"
                reply += (
                    f"Posición cerrada. Resultado neto de comisiones: {sign}${abs(net):,.2f} "
                    f"({msg.signed(r, 'R')})."
                )
            else:
                reply += (
                    f"Te quedan {position.qty_open:g} acciones con stop en ${position.stop:,.2f}."
                )
            return reply

    async def _status(self) -> None:
        with session_scope(self._engine) as session:
            portfolio = account.load_portfolio(session, self._config)
            alerts = watching.open_alerts(session)
            incidents = states.open_incidents(session)
        lines = [
            f"Efectivo: ${portfolio.cash:,.2f} · Capital: ${portfolio.capital:,.2f}",
            f"Posiciones abiertas: {len(portfolio.holdings)}",
        ]
        for holding in portfolio.holdings:
            r_now = msg.signed(holding.r_now, "R") if holding.r_now is not None else "sin R"
            lines.append(
                f"· {holding.ticker}: {holding.shares:g} a ${holding.entry_price:,.2f}, "
                f"último ${holding.last_price:,.2f} ({r_now}), stop ${holding.stop:,.2f}"
            )
        lines.append(f"Alertas sin resolver: {len(alerts)}")
        if incidents:
            lines.append(f"PRECAUCIÓN: {len(incidents)} incidente(s) de estado sin resolver.")
        await self._say("\n".join(lines))
