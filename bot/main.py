from __future__ import annotations

import asyncio
import sys

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import get_settings
from bot.db.engine import close_db, get_session_factory, init_db
from bot.handlers import admin, balance, catalog, history, order, promo, start, supplier_checkout
from bot.handlers import payment as payment_handler
from bot.logging_config import setup_logging
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.middlewares.user_context import UserContextMiddleware
from bot.services.background import (
    poll_external_orders,
    poll_partner_deposits,
    poll_user_deposits,
    poll_supplier_checkouts,
)
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.aethel_api import AethelAPIClient
from bot.services.suppliers import SupplierRouter
from bot.services.telegram_startup import wait_for_telegram
from bot.services.payments import CryptoBotPayment, YooKassaPayment

logger = structlog.get_logger()


async def main() -> None:
    # ── Load config ──────────────────────────────────────────────────
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info(
        "starting_bot",
        api_base=settings.partner_api_base,
        db=settings.database_url,
        markup=settings.markup_percent,
        admins=settings.admin_ids,
    )

    # ── Init DB ──────────────────────────────────────────────────────
    await init_db(settings.database_url)
    session_factory = get_session_factory()

    # ── Init Partner API client ──────────────────────────────────────
    api = PartnerAPIClient(
        base_url=settings.partner_api_base,
        api_key=settings.partner_api_key,
        rate_limit=settings.rate_limit_per_sec,
    )

    # Validate API key on startup
    try:
        bal = await api.get_balance()
        logger.info(
            "api_key_valid",
            balance=bal.balance,
            discount=bal.discount_percent,
        )
    except PartnerAPIError as exc:
        if exc.http_status in (401, 403):
            logger.critical(
                "invalid_api_key",
                code=exc.code,
                message=exc.message,
            )
            await api.close()
            await close_db()
            sys.exit(1)
        else:
            logger.warning("api_check_failed", error=str(exc))

    if settings.aethel_api_key and settings.aethel_usd_rub_rate > 0:
        aethel = AethelAPIClient(settings.aethel_api_base, settings.aethel_api_key,
                               settings.aethel_usd_rub_rate)
        try:
            await aethel.get_balance_usd()
        except PartnerAPIError as exc:
            logger.warning("aethel_check_failed", code=exc.code)
        api = SupplierRouter(api, aethel)
        logger.info("supplier_routing_enabled", gemini="aethel", chatgpt="aethel",
                    claude="thegodshop", usd_rub_rate=settings.aethel_usd_rub_rate)

    # ── Init Bot & Dispatcher ────────────────────────────────────────
    bot_kwargs: dict = {
        "token": settings.bot_token,
        "default": DefaultBotProperties(parse_mode=ParseMode.HTML),
    }
    if settings.proxy_url:
        from aiogram.client.session.aiohttp import AiohttpSession
        bot_kwargs["session"] = AiohttpSession(proxy=settings.proxy_url)
        logger.info("using_proxy", proxy=settings.proxy_url)

    bot = Bot(**bot_kwargs)
    dp = Dispatcher(storage=MemoryStorage())

    # ── Register middleware ──────────────────────────────────────────
    dp.message.middleware(ThrottlingMiddleware(cooldown=1.0))
    dp.callback_query.middleware(ThrottlingMiddleware(cooldown=0.5))

    user_ctx = UserContextMiddleware(session_factory)
    dp.message.middleware(user_ctx)
    dp.callback_query.middleware(user_ctx)

    # ── Inject shared dependencies into handler context ──────────────
    dp["api"] = api
    dp["markup_percent"] = settings.markup_percent
    dp["admin_ids"] = settings.admin_ids
    dp["session_factory"] = session_factory
    dp["steam_min_amount"] = settings.steam_min_amount
    dp["steam_max_amount"] = settings.steam_max_amount
    dp["direct_supplier_checkout_enabled"] = settings.direct_supplier_checkout_enabled

    # ── Init payment providers ───────────────────────────────────────
    cryptobot: CryptoBotPayment | None = None
    yookassa: YooKassaPayment | None = None

    if settings.cryptobot_token:
        cryptobot = CryptoBotPayment(settings.cryptobot_token)
        logger.info("payment_provider_enabled", provider="CryptoBot")

    if settings.yookassa_shop_id and settings.yookassa_secret_key:
        yookassa = YooKassaPayment(settings.yookassa_shop_id, settings.yookassa_secret_key)
        logger.info("payment_provider_enabled", provider="YooKassa")

    dp["cryptobot"] = cryptobot
    dp["yookassa"] = yookassa

    # ── Register routers ─────────────────────────────────────────────
    dp.include_router(admin.router)
    dp.include_router(payment_handler.router)
    dp.include_router(start.router)
    dp.include_router(promo.router)
    dp.include_router(supplier_checkout.router)
    dp.include_router(catalog.router)
    dp.include_router(order.router)
    dp.include_router(balance.router)
    dp.include_router(history.router)

    # ── Start background tasks ───────────────────────────────────────
    bg_tasks: list[asyncio.Task] = []

    bg_tasks.append(asyncio.create_task(poll_supplier_checkouts(
        bot=bot, session_factory=session_factory, api=api,
        admin_ids=settings.admin_ids, markup_percent=settings.markup_percent,
    )))

    bg_tasks.append(
        asyncio.create_task(
            poll_external_orders(
                bot=bot,
                session_factory=session_factory,
                api=api,
                admin_ids=settings.admin_ids,
                interval=15.0,
            )
        )
    )

    bg_tasks.append(
        asyncio.create_task(
            poll_partner_deposits(
                bot=bot,
                session_factory=session_factory,
                api=api,
                admin_ids=settings.admin_ids,
                interval=20.0,
            )
        )
    )

    if cryptobot or yookassa:
        bg_tasks.append(
            asyncio.create_task(
                poll_user_deposits(
                    bot=bot,
                    session_factory=session_factory,
                    cryptobot=cryptobot,
                    yookassa=yookassa,
                    admin_ids=settings.admin_ids,
                    interval=25.0,
                )
            )
        )


    # ── Start polling ────────────────────────────────────────────────
    logger.info("bot_started")
    try:
        await wait_for_telegram(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("bot_stopping")
        for task in bg_tasks:
            task.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        await api.close()
        if cryptobot:
            await cryptobot.close()
        if yookassa:
            await yookassa.close()
        await close_db()
        await bot.session.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    asyncio.run(main())
