"""Telegram reachability check. Sending alerts arrives with the watcher (Phase 6).

The bot token is part of the URL path, which is why errors and logs never include
URLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from collectors.base import Collector, RawSnapshot, _Requests
from collectors.http import SourceError

API_URL = "https://api.telegram.org/bot{token}/{method}"


class TelegramCollector(Collector):
    source = "telegram"

    async def get_me(self, *, as_of: datetime | None = None) -> RawSnapshot:
        """Confirms the bot token works. Sends nothing to the chat."""

        async def fetch(requests: _Requests) -> Any:
            payload = await requests.get_json(
                API_URL.format(token=self._secret("telegram_bot_token"), method="getMe")
            )
            if not payload.get("ok"):
                raise SourceError("Telegram rechazó el token del bot")
            return payload

        return await self._collect("get_me", "getMe", fetch, as_of=as_of)
