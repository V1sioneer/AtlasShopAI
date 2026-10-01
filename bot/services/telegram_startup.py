"""Wait for Telegram's read-only identity check before starting polling."""
from __future__ import annotations

import asyncio

import structlog
from aiogram.exceptions import TelegramNetworkError

logger = structlog.get_logger()


async def wait_for_telegram(bot, *, sleep=asyncio.sleep):
    attempt = 0
    while True:
        try:
            # Bot.me() caches the identity, so Dispatcher does not perform a
            # second unprotected getMe request when entering its poll loop.
            identity = await asyncio.wait_for(bot.me(), timeout=15)
            logger.info("telegram_ready", username=identity.username)
            return
        except (TelegramNetworkError, TimeoutError):
            attempt += 1
            delay = min(2 * attempt, 10)
            logger.warning("telegram_startup_retry", attempt=attempt, retry_seconds=delay)
            await sleep(delay)
