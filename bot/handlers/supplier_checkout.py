from __future__ import annotations

import json
import httpx
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Order, OrderStatus, SupplierCheckout, User
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.payments import CryptoBotPayment
from bot.services.supplier_checkout import CheckoutError, RETRYABLE_ERRORS, SupplierCheckoutService
from bot.services.supplier_funding import protected_supplier_amount
from bot.utils.formatting import format_price
from bot.utils.money import money

router = Router(name="supplier_checkout")


def checkout_view(checkout: SupplierCheckout, order: Order) -> tuple[str, InlineKeyboardMarkup]:
    payload = json.loads(order.payload_json or "{}")
    text = (
        f"🧾 <b>Заказ #{order.id}</b>\n\n"
        f"{escape(payload.get('product_name', 'Товар из каталога'))}\n"
        f"Количество: {order.qty}\n"
        f"Итого: <b>{format_price(order.user_price)}</b>\n\n"
    )
    rows = []
    if checkout.status == "awaiting_payment":
        if checkout.margin_amount_rub > 0:
            text += (f"Сервисный сбор: {format_price(checkout.margin_amount_rub)} — "
                     f"{'✅ оплачен' if checkout.margin_paid else 'ожидает оплаты'}.\n"
                     f"Поставщику: {format_price(checkout.amount_rub)} — "
                     f"{'✅ оплачено' if checkout.supplier_paid else 'ожидает оплаты'}.\n\n")
        if not checkout.margin_paid:
            text += "Сначала оплатите сервисный сбор и нажмите «Проверить оплату». Затем появится счёт поставщика."
            rows.append([InlineKeyboardButton(text="1. Оплатить сервисный сбор", url=checkout.margin_pay_url)])
        elif not checkout.supplier_paid:
            text += (
                "Оплатите счёт через CryptoBot. Деньги поступят напрямую поставщику; "
                "пополнять баланс бота для этого заказа не нужно.\n"
                f"Сумма счёта: {checkout.amount_usdt:g} USDT.\n\n"
                "После подтверждения оплаты товар выдаётся автоматически. "
                "Если выдача окажется недоступна, оплаченный заказ сохранится для решения поддержкой."
            )
            rows.append([InlineKeyboardButton(text="2. Оплатить поставщику" if checkout.margin_amount_rub > 0 else "Оплатить в CryptoBot", url=checkout.pay_url)])
        else:
            text += "Оплата подтверждена. Проверяем выдачу товара."
        rows.append([InlineKeyboardButton(text="Проверить оплату и выдачу", callback_data=f"direct_check:{checkout.id}")])
    elif checkout.status == "delivered":
        text += "✅ <b>Оплата получена, товар выдан.</b>\n\n"
        data = escape(order.delivered_data or "")
        if len(data) <= 2800:
            text += f"<code>{data}</code>"
        else:
            text += "Данные заказа доступны файлом по кнопке ниже."
            rows.append([InlineKeyboardButton(text="Скачать данные заказа", callback_data=f"direct_data:{checkout.id}")])
    elif checkout.status == "attention":
        if checkout.supplier_paid or (checkout.margin_amount_rub > 0 and checkout.margin_paid):
            text += (
                "🛠 Оплата получена, но выдача пока не завершена. "
                "Заказ сохранён. Поддержка поможет с выдачей или возвратом оплаты."
            )
        else:
            text += "🛠 Не удалось завершить проверку заказа. Обратитесь в поддержку с номером заказа."
        if checkout.supplier_paid and checkout.error_code in RETRYABLE_ERRORS and order.status != OrderStatus.UNCERTAIN:
            rows.append([InlineKeyboardButton(text="Повторить выдачу", callback_data=f"direct_retry:{checkout.id}")])
        rows.append([InlineKeyboardButton(text="Связаться с поддержкой", url="https://t.me/V1sionHere")])
    elif checkout.status == "expired":
        text += "⌛ Счёт истёк. Купон восстановлен; откройте товар для нового заказа."
    elif checkout.status == "refunded":
        text += "Возврат оплаты отмечен поддержкой. Купон восстановлен."
    elif checkout.status == "failed":
        text += "Не удалось создать счёт. Купон сохранён; откройте товар для нового заказа."
    else:
        text += "Заказ обрабатывается. Проверьте его через несколько секунд."
        rows.append([InlineKeyboardButton(text="Проверить заказ", callback_data=f"direct_check:{checkout.id}")])
    rows.append([InlineKeyboardButton(text="Мои счета", callback_data="direct_list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("supplier_checkout:"))
async def cb_create_checkout(
    callback: CallbackQuery, session: AsyncSession, db_user: User, api: PartnerAPIClient,
    markup_percent: float, direct_supplier_checkout_enabled: bool = True,
    cryptobot: CryptoBotPayment | None = None,
) -> None:
    if not direct_supplier_checkout_enabled:
        await callback.answer("Этот способ оплаты временно отключён.", show_alert=True)
        return
    try:
        _, product_id, qty, amount = callback.data.split(":")
        product_id = int(product_id)
        qty = int(qty)
        if not 1 <= qty <= 99:
            raise ValueError
        expected_price = float(money(amount))
    except (ValueError, TypeError):
        await callback.answer("Откройте карточку товара снова.", show_alert=True)
        return
    await callback.answer("Готовлю счёт…")
    key = f"supplier:{db_user.id}:{callback.message.chat.id}:{callback.message.message_id}:{product_id}:{qty}"
    try:
        checkout, order = await SupplierCheckoutService(session, api, markup_percent, cryptobot).create(
            db_user.id, product_id, expected_price=expected_price, request_key=key, qty=qty,
        )
    except CheckoutError as exc:
        await callback.message.answer(escape(str(exc)))
        return
    except (PartnerAPIError, RuntimeError, httpx.HTTPError):
        await callback.message.answer("Не удалось создать счёт у поставщика. Купон сохранён; попробуйте позже.")
        return
    text, keyboard = checkout_view(checkout, order)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("direct_check:"))
@router.callback_query(F.data.startswith("direct_retry:"))
async def cb_check_checkout(
    callback: CallbackQuery, session: AsyncSession, db_user: User, api: PartnerAPIClient, markup_percent: float,
    cryptobot: CryptoBotPayment | None = None,
) -> None:
    try:
        action, checkout_id = callback.data.split(":")
        checkout_id = int(checkout_id)
    except (ValueError, TypeError):
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    # Ownership is checked before acknowledging or calling the provider.
    service = SupplierCheckoutService(session, api, markup_percent, cryptobot)
    try:
        await service.get(checkout_id, db_user.id)
    except CheckoutError:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    await callback.answer("Проверяю…")
    try:
        checkout, order = await service.check(checkout_id, db_user.id, retry=action == "direct_retry")
    except (PartnerAPIError, CheckoutError, ValueError, RuntimeError, httpx.HTTPError):
        await callback.message.answer("Поставщик пока не ответил. Заказ сохранён; проверьте ещё раз позже.")
        return
    text, keyboard = checkout_view(checkout, order)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
        await callback.message.answer("Пока без изменений. Проверка оплаты также идёт автоматически.")


@router.callback_query(F.data.startswith("direct_data:"))
async def cb_download_checkout(
    callback: CallbackQuery, session: AsyncSession, db_user: User, api: PartnerAPIClient, markup_percent: float,
) -> None:
    try:
        checkout, order = await SupplierCheckoutService(session, api, markup_percent).get(
            int(callback.data.split(":")[1]), db_user.id
        )
        if checkout.status != "delivered" or order.status != OrderStatus.SUCCESS or not order.delivered_data:
            raise CheckoutError("Данные заказа ещё не готовы.")
    except (ValueError, CheckoutError):
        await callback.answer("Данные заказа недоступны.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(order.delivered_data.encode("utf-8"), filename=f"atlas-order-{order.id}.txt")
    )


async def list_checkouts(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = (await session.execute(
        select(SupplierCheckout, Order).join(Order, Order.id == SupplierCheckout.order_id)
        .where(Order.user_id == user_id).order_by(SupplierCheckout.id.desc()).limit(20)
    )).all()
    buttons = [[InlineKeyboardButton(
        text=f"Заказ #{order.id} · {format_price(order.user_price)}",
        callback_data=f"direct_check:{checkout.id}",
    )] for checkout, order in rows]
    text = "Выберите счёт или оплаченный заказ:" if rows else "У вас пока нет счетов прямой оплаты."
    buttons.append([InlineKeyboardButton(text="В главное меню", callback_data="back_main")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("direct_orders"))
async def cmd_direct_orders(message: Message, session: AsyncSession, db_user: User) -> None:
    text, keyboard = await list_checkouts(session, db_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "direct_list")
async def cb_direct_list(callback: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    text, keyboard = await list_checkouts(session, db_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.message(Command("direct_stats"))
async def cmd_direct_stats(message: Message, session: AsyncSession, admin_ids: list[int]) -> None:
    if not message.from_user or message.from_user.id not in admin_ids:
        return
    counts = (await session.execute(
        select(SupplierCheckout.status, func.count(SupplierCheckout.id)).group_by(SupplierCheckout.status)
    )).all()
    lines = ["<b>Прямая оплата поставщику</b>"]
    lines.extend(f"{escape(status)}: {count}" for status, count in counts)
    pending = (await session.execute(
        select(SupplierCheckout.id, SupplierCheckout.order_id, SupplierCheckout.status)
        .where(SupplierCheckout.status.in_(["awaiting_payment", "attention", "fulfilling"]))
        .order_by(SupplierCheckout.id.desc()).limit(20)
    )).all()
    lines.extend(f"ID счёта {row.id} · заказ #{row.order_id} · {escape(row.status)}" for row in pending)
    lines.append(f"Защищено под счета: {format_price(await protected_supplier_amount(session))}")
    lines.append("/direct_order ID_СЧЁТА — подробности\n/direct_retry ID_СЧЁТА — повторить подтверждённо неудачную выдачу")
    await message.answer("\n".join(lines))


@router.message(Command("direct_order", "direct_retry"))
async def cmd_admin_checkout(
    message: Message, command: CommandObject, session: AsyncSession,
    api: PartnerAPIClient, markup_percent: float, admin_ids: list[int],
    cryptobot: CryptoBotPayment | None = None,
) -> None:
    if not message.from_user or message.from_user.id not in admin_ids:
        return
    service = SupplierCheckoutService(session, api, markup_percent, cryptobot)
    try:
        checkout_id = int(command.args or "")
        if command.command == "direct_retry":
            checkout, order = await service.check(checkout_id, retry=True)
        else:
            checkout, order = await service.get(checkout_id)
    except (ValueError, CheckoutError, PartnerAPIError, RuntimeError, httpx.HTTPError):
        await message.answer("Не удалось получить заказ. Укажите ID счёта из /direct_stats или уведомления.")
        return
    text, _ = checkout_view(checkout, order)
    await message.answer(
        f"ID счёта: {checkout.id}\nПокупатель: <code>{order.user_id}</code>\n"
        f"Депозит поставщика: {checkout.deposit_id}\nПричина: {escape(checkout.error_code or '—')}\n\n{text}"
        f"\nСчёт сервисного сбора: {checkout.margin_invoice_id or '—'}\n"
        f"Оплачено поставщику: {'да' if checkout.supplier_paid else 'нет'}\n"
        f"Сервисный сбор оплачен: {'да' if checkout.margin_paid else 'нет'}"
    )


@router.message(Command("direct_refunded"))
async def cmd_admin_refunded(
    message: Message, command: CommandObject, session: AsyncSession,
    api: PartnerAPIClient, markup_percent: float, admin_ids: list[int],
) -> None:
    if not message.from_user or message.from_user.id not in admin_ids:
        return
    parts = (command.args or "").split()
    if len(parts) != 3 or parts[2] != "ВОЗВРАТ_ВЫПОЛНЕН":
        await message.answer(
            "Команда только отмечает возврат, уже выполненный вручную; денег она не переводит.\n"
            "Формат: /direct_refunded ID_СЧЁТА СУММА ВОЗВРАТ_ВЫПОЛНЕН"
        )
        return
    try:
        checkout, order = await SupplierCheckoutService(session, api, markup_percent).record_refund(
            int(parts[0]), float(money(parts[1]))
        )
    except (ValueError, CheckoutError) as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer(f"Выполненный возврат по заказу #{order.id} отмечен. Купон восстановлен.")
