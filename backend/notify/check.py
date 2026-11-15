"""``python -m notify.check``: send one test message to the configured Telegram chat.

The reachability check of Phase 1 (getMe) proves the bot token works; only a real
message proves that ``TELEGRAM_CHAT_ID`` is the right chat. Nothing secret is printed.
"""

from __future__ import annotations

import asyncio
import sys

from collectors.registry import build_runtime
from core import clock, settings
from core.logging import configure_logging
from notify.messages import OutMessage
from notify.telegram import TelegramClient


async def _send() -> int:
    config, secrets = settings.get_config(), settings.get_secrets()
    configure_logging("WARNING", secret_values=secrets.secret_values(), stream=sys.stderr)
    missing = secrets.missing_for("telegram")
    if missing:
        print(f"Telegram sin configurar: falta {', '.join(missing)} en .env")
        return 1
    runtime = build_runtime(config, secrets)
    try:
        message_id = await TelegramClient(runtime).send(
            OutMessage(
                f"{config.system.name}: mensaje de prueba ({clock.dual_label(clock.now())}).\n"
                "Si lo ves, el Plan del día y las alertas de venta te van a llegar a este "
                "chat. No tienes que hacer nada."
            )
        )
    finally:
        await runtime.aclose()
    if message_id is None:
        print("Telegram NO aceptó el mensaje: revisa TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID.")
        return 1
    print(f"Telegram aceptó el mensaje de prueba (id {message_id}).")
    return 0


def main() -> int:
    return asyncio.run(_send())


if __name__ == "__main__":
    sys.exit(main())
