from __future__ import annotations

from typing import Optional
import structlog
from aiohttp import web
from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from bot.services.payments import FreeKassaPayment
from bot.services.settlement import InvalidPayment, settle_payment
from bot.utils.formatting import format_price

logger = structlog.get_logger()


class FreeKassaWebhookServer:
    """HTTP webhook server to handle incoming FreeKassa Result URL notifications."""

    def __init__(
        self,
        freekassa: FreeKassaPayment,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        admin_ids: list[int],
    ) -> None:
        self.freekassa = freekassa
        self.session_factory = session_factory
        self.bot = bot
        self.admin_ids = admin_ids
        self.app = web.Application()
        self.app.router.add_post("/freekassa/notify", self.handle_notify)
        self.app.router.add_get("/freekassa/notify", self.handle_get)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def handle_get(self, request: web.Request) -> web.Response:
        """Health check for FreeKassa URL verification."""
        return web.Response(text="YES", content_type="text/plain")

    async def handle_notify(self, request: web.Request) -> web.Response:
        """Handle FreeKassa Result URL POST notification."""
        try:
            data = dict(await request.post())
        except Exception:
            try:
                data = await request.json()
            except Exception:
                data = {}

        logger.info(
            "freekassa_notify_received",
            merchant_id=data.get("MERCHANT_ID") or data.get("merchant_id"),
            order_id=data.get("MERCHANT_ORDER_ID") or data.get("merchant_order_id"),
            amount=data.get("AMOUNT") or data.get("amount"),
        )

        valid, reason = self.freekassa.verify_notification(data)
        if not valid:
            logger.warning("freekassa_invalid_notification", reason=reason, data=data)
            return web.Response(text=f"ERROR: {reason}", status=400, content_type="text/plain")

        order_id = str(data.get("MERCHANT_ORDER_ID") or data.get("merchant_order_id") or "").strip()
        amount_val = data.get("AMOUNT") or data.get("amount") or 0.0
        try:
            amount = float(amount_val)
        except (ValueError, TypeError):
            logger.warning("freekassa_invalid_amount_format", amount=amount_val)
            return web.Response(text="ERROR: Invalid amount", status=400, content_type="text/plain")

        async with self.session_factory() as session:
            try:
                settlement = await settle_payment(
                    session=session,
                    method="freekassa",
                    external_id=order_id,
                    payment={
                        "order_id": order_id,
                        "amount": amount,
                        "currency": "RUB",
                        "status": "completed",
                    },
                )
            except InvalidPayment as exc:
                logger.warning("freekassa_settlement_rejected", order_id=order_id, error=str(exc))
                return web.Response(text=f"ERROR: {exc}", status=400, content_type="text/plain")
            except Exception as exc:
                logger.error("freekassa_settlement_failed", order_id=order_id, error=str(exc))
                return web.Response(text="ERROR: Internal processing failure", status=500, content_type="text/plain")

        if settlement.applied:
            logger.info(
                "freekassa_payment_settled",
                user_id=settlement.user_id,
                amount=settlement.amount,
                balance=settlement.balance,
                order_id=order_id,
            )
            # Notify user
            try:
                await self.bot.send_message(
                    settlement.user_id,
                    f"✅ <b>Оплата получена!</b>\n\n"
                    f"Зачислено: <b>+{format_price(settlement.amount)}</b>\n"
                    f"Текущий баланс: <b>{format_price(settlement.balance)}</b>",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.warning("freekassa_user_alert_failed", user_id=settlement.user_id, error=str(exc))

            # Notify admins
            if self.admin_ids:
                for aid in self.admin_ids:
                    try:
                        await self.bot.send_message(
                            aid,
                            f"💰 <b>ПОПОЛНЕНИЕ БАЛАНСА!</b>\n\n"
                            f"👤 Пользователь ID: <code>{settlement.user_id}</code>\n"
                            f"💵 Зачислено: <b>+{format_price(settlement.amount)}</b>\n"
                            f"📈 Новый баланс: <b>{format_price(settlement.balance)}</b>\n"
                            f"💳 Способ: FreeKassa (Карта / СБП)",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
        else:
            logger.info("freekassa_payment_already_applied", order_id=order_id)

        # FreeKassa expects 'YES' on success
        return web.Response(text="YES", content_type="text/plain")

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start listening for incoming FreeKassa webhook requests."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        logger.info("freekassa_webhook_started", host=host, port=port)

    async def stop(self) -> None:
        """Gracefully stop webhook server."""
        if self.runner is not None:
            await self.runner.cleanup()
            logger.info("freekassa_webhook_stopped")
