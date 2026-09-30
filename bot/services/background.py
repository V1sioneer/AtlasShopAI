from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from bot.db.models import OrderStatus
from bot.db.repo import DepositRepo, OrderRepo, PartnerDepositRepo, TransactionRepo, UserRepo
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.payments import CryptoBotPayment, YooKassaPayment
from bot.services.settlement import InvalidPayment, expire_payment, settle_payment
from bot.utils.formatting import format_price

logger = structlog.get_logger()


async def poll_external_orders(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    api: PartnerAPIClient,
    admin_ids: list[int],
    interval: float = 15.0,
    max_age_minutes: float = 30.0,
) -> None:
    """Background task: poll external orders (Steam/Games) for status updates."""
    logger.info("background_task_started", task="poll_external_orders")

    while True:
        try:
            await asyncio.sleep(interval)

            async with session_factory() as session:
                order_repo = OrderRepo(session)
                orders = await order_repo.get_processing_external()

                if not orders:
                    continue

                logger.debug("polling_external_orders", count=len(orders))

                for order in orders:
                    if order.partner_order_id is None:
                        continue

                    try:
                        status_resp = await api.get_external_status(
                            order.partner_order_id
                        )
                    except PartnerAPIError as exc:
                        logger.warning(
                            "poll_status_error",
                            order_id=order.id,
                            partner_order_id=order.partner_order_id,
                            error=str(exc),
                        )
                        continue

                    new_status = status_resp.status.lower()

                    if new_status in ("success", "completed"):
                        await order_repo.update_status(
                            order.id,
                            OrderStatus.SUCCESS,
                            delivered_data="Заказ выполнен",
                        )
                        await session.commit()

                        # Notify user
                        try:
                            await bot.send_message(
                                order.user_id,
                                f"✅ <b>Заказ #{order.id} выполнен!</b>\n\n"
                                f"Ваш заказ успешно обработан.",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                        logger.info(
                            "order_completed",
                            order_id=order.id,
                            partner_order_id=order.partner_order_id,
                        )

                    elif new_status == "failed":
                        refunded = await order_repo.transition(
                            order.id,
                            [OrderStatus.PROCESSING],
                            OrderStatus.FAILED,
                        )
                        if not refunded:
                            await session.rollback()
                            continue
                        await UserRepo(session).update_balance(order.user_id, order.user_price)
                        await TransactionRepo(session).create(
                            user_id=order.user_id,
                            delta=order.user_price,
                            reason="Возврат: заказ не выполнен",
                            order_id=order.id,
                        )
                        await session.commit()

                        # Notify user
                        try:
                            await bot.send_message(
                                order.user_id,
                                f"❌ <b>Заказ #{order.id} не выполнен</b>\n\n"
                                f"Средства возвращены на ваш баланс: "
                                f"{order.user_price:.0f} ₽",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                        logger.info(
                            "order_failed_refunded",
                            order_id=order.id,
                            user_id=order.user_id,
                            refund=order.user_price,
                        )

                    elif new_status == "uncertain":
                        await order_repo.update_status(
                            order.id, OrderStatus.UNCERTAIN
                        )
                        await session.commit()

                        # Notify user
                        try:
                            await bot.send_message(
                                order.user_id,
                                f"⚠️ <b>Заказ #{order.id}</b>\n\n"
                                f"Статус заказа уточняется. "
                                f"Средства заморожены, ожидайте решения.",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                        # Alert admins
                        for aid in admin_ids:
                            try:
                                await bot.send_message(
                                    aid,
                                    f"⚠️ <b>UNCERTAIN ORDER #{order.id}</b>\n"
                                    f"User: {order.user_id}\n"
                                    f"Partner order: {order.partner_order_id}\n"
                                    f"Amount: {order.user_price:.0f} ₽\n"
                                    f"Требует ручного разбора!",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass

                        logger.warning(
                            "order_uncertain",
                            order_id=order.id,
                            partner_order_id=order.partner_order_id,
                        )

                    # else: still processing, do nothing

        except asyncio.CancelledError:
            logger.info("poll_external_orders_cancelled")
            break
        except Exception as exc:
            logger.error("poll_external_orders_error", error=str(exc))
            await asyncio.sleep(30)


async def poll_partner_deposits(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    api: PartnerAPIClient,
    admin_ids: list[int],
    interval: float = 20.0,
) -> None:
    """Background task: poll pending partner deposits for payment confirmation."""
    logger.info("background_task_started", task="poll_partner_deposits")

    while True:
        try:
            await asyncio.sleep(interval)

            async with session_factory() as session:
                dep_repo = PartnerDepositRepo(session)
                pending = await dep_repo.get_pending()

                for dep in pending:
                    try:
                        result = await api.get_deposit(dep.deposit_id_partner)
                    except PartnerAPIError:
                        continue

                    if result.status == "paid":
                        await dep_repo.update_status(
                            dep.deposit_id_partner, "paid"
                        )
                        await session.commit()

                        # Notify admins
                        for aid in admin_ids:
                            try:
                                await bot.send_message(
                                    aid,
                                    f"✅ <b>Депозит #{dep.deposit_id_partner} оплачен!</b>\n"
                                    f"Сумма: {dep.amount_rub:.0f} ₽\n"
                                    f"Баланс пополнен.",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass

                        logger.info(
                            "deposit_paid",
                            deposit_id=dep.deposit_id_partner,
                            amount_rub=dep.amount_rub,
                        )

        except asyncio.CancelledError:
            logger.info("poll_partner_deposits_cancelled")
            break
        except Exception as exc:
            logger.error("poll_partner_deposits_error", error=str(exc))
            await asyncio.sleep(30)


async def poll_user_deposits(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    cryptobot: CryptoBotPayment | None,
    yookassa: YooKassaPayment | None,
    admin_ids: list[int] | None = None,
    interval: float = 25.0,
) -> None:
    """Background task: auto-check pending user deposits for CryptoBot and YooKassa."""
    if not cryptobot and not yookassa:
        return

    logger.info("background_task_started", task="poll_user_deposits")

    while True:
        try:
            await asyncio.sleep(interval)

            async with session_factory() as session:
                dep_repo = DepositRepo(session)
                # 1. CryptoBot
                if cryptobot:
                    crypto_deps = await dep_repo.get_pending_by_method("cryptobot")
                    for dep in crypto_deps:
                        if not dep.external_id:
                            continue
                        try:
                            invoice = await cryptobot.get_invoice(int(dep.external_id))
                            if invoice["status"] == "paid":
                                settlement = await settle_payment(
                                    session, "cryptobot", dep.external_id, invoice,
                                )
                                if not settlement.applied:
                                    continue

                                try:
                                    await bot.send_message(
                                        settlement.user_id,
                                        f"✅ <b>Оплата получена!</b>\n\n"
                                        f"Зачислено: {format_price(settlement.amount)}\n"
                                        f"Текущий баланс: {format_price(settlement.balance)}",
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass

                                if admin_ids:
                                    for aid in admin_ids:
                                        try:
                                            await bot.send_message(
                                                aid,
                                                f"💰 <b>АВТО-ПОПОЛНЕНИЕ БАЛАНСА!</b>\n\n"
                                                f"👤 Пользователь ID: <code>{settlement.user_id}</code>\n"
                                                f"💵 Зачислено: <b>+{format_price(settlement.amount)}</b>\n"
                                                f"📈 Новый баланс: <b>{format_price(settlement.balance)}</b>\n"
                                                f"🤖 Способ: CryptoBot",
                                                parse_mode="HTML",
                                            )
                                        except Exception:
                                            pass
                            elif invoice["status"] == "expired":
                                await expire_payment(session, "cryptobot", dep.external_id)
                        except InvalidPayment as e:
                            logger.warning("poll_cryptobot_rejected", error=str(e), dep_id=dep.id)
                        except Exception as e:
                            logger.debug("poll_cryptobot_err", error=str(e), dep_id=dep.id)

                # 2. YooKassa
                if yookassa:
                    yk_deps = await dep_repo.get_pending_by_method("yookassa")
                    for dep in yk_deps:
                        if not dep.external_id:
                            continue
                        try:
                            payment = await yookassa.get_payment(dep.external_id)
                            if payment["status"] == "succeeded":
                                settlement = await settle_payment(
                                    session, "yookassa", dep.external_id, payment,
                                )
                                if not settlement.applied:
                                    continue

                                try:
                                    await bot.send_message(
                                        settlement.user_id,
                                        f"✅ <b>Оплата получена!</b>\n\n"
                                        f"Зачислено: {format_price(settlement.amount)}\n"
                                        f"Текущий баланс: {format_price(settlement.balance)}",
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass

                                if admin_ids:
                                    for aid in admin_ids:
                                        try:
                                            await bot.send_message(
                                                aid,
                                                f"💰 <b>АВТО-ПОПОЛНЕНИЕ БАЛАНСА!</b>\n\n"
                                                f"👤 Пользователь ID: <code>{settlement.user_id}</code>\n"
                                                f"💵 Зачислено: <b>+{format_price(settlement.amount)}</b>\n"
                                                f"📈 Новый баланс: <b>{format_price(settlement.balance)}</b>\n"
                                                f"💳 Способ: ЮKassa",
                                                parse_mode="HTML",
                                            )
                                        except Exception:
                                            pass
                            elif payment["status"] == "canceled":
                                await expire_payment(session, "yookassa", dep.external_id)
                        except InvalidPayment as e:
                            logger.warning("poll_yookassa_rejected", error=str(e), dep_id=dep.id)
                        except Exception as e:
                            logger.debug("poll_yookassa_err", error=str(e), dep_id=dep.id)

        except asyncio.CancelledError:
            logger.info("poll_user_deposits_cancelled")
            break
        except Exception as exc:
            logger.error("poll_user_deposits_error", error=str(exc))
            await asyncio.sleep(30)

