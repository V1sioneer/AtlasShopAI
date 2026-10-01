import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from bot.services.telegram_startup import wait_for_telegram


@pytest.mark.asyncio
async def test_transient_identity_failures_retry_until_identity_is_cached():
    identity = SimpleNamespace(username="AtlasShopAI_bot")
    bot = SimpleNamespace(me=AsyncMock(side_effect=[
        TelegramNetworkError(method=GetMe(), message="timeout"), TimeoutError(), identity]))
    sleep = AsyncMock()
    await wait_for_telegram(bot, sleep=sleep)
    assert bot.me.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]


@pytest.mark.asyncio
async def test_invalid_bot_token_is_not_retried():
    bot = SimpleNamespace(me=AsyncMock(side_effect=TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")))
    sleep = AsyncMock()
    with pytest.raises(TelegramUnauthorizedError):
        await wait_for_telegram(bot, sleep=sleep)
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_startup_stops_immediately():
    bot = SimpleNamespace(me=AsyncMock(side_effect=asyncio.CancelledError()))
    sleep = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await wait_for_telegram(bot, sleep=sleep)
    sleep.assert_not_called()
