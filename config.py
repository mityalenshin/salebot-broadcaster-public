"""
Управление токенами ботов и общими настройками.

Multi-bot модель:
- Один основной бот в TELEGRAM_BROADCAST_BOT_TOKEN / _USERNAME (обязательный)
- Дополнительные — переменные .env вида:
    EXTRA_BOT_TOKEN_another_bot=12345:abc...
    EXTRA_BOT_TOKEN_third_bot=67890:def...
  Суффикс после префикса = username бота (без @).
"""
import os

from dotenv import load_dotenv

load_dotenv()

EXTRA_PREFIX = "EXTRA_BOT_TOKEN_"

PRIMARY_BOT_USERNAME = os.environ["TELEGRAM_BROADCAST_BOT_USERNAME"]
PRIMARY_BOT_TOKEN = os.environ["TELEGRAM_BROADCAST_BOT_TOKEN"]
COMMAND_BOT_TOKEN = os.environ["TELEGRAM_COMMAND_BOT_TOKEN"]


def get_bot_tokens() -> dict[str, str]:
    """{bot_username: token} — все доступные боты для рассылок, включая основной."""
    out = {PRIMARY_BOT_USERNAME: PRIMARY_BOT_TOKEN}
    for k, v in os.environ.items():
        if k.startswith(EXTRA_PREFIX) and v:
            username = k[len(EXTRA_PREFIX):]
            out[username] = v
    return out


def get_bot_token(bot_username: str) -> str | None:
    """Токен конкретного бота. None если для этого бота токен не задан."""
    return get_bot_tokens().get(bot_username)


def is_known_bot(bot_username: str) -> bool:
    return bot_username in get_bot_tokens()
