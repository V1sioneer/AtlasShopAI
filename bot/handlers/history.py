from __future__ import annotations

import math
from html import escape

import structlog
from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import OrderStatus, User
from bot.db.repo import OrderRepo
from bot.keyboards.kb import history_page_kb
from bot.utils.formatting import format_datetime, format_price, format_status

logger = structlog.get_logger()

router = Router(name="history")

ORDERS_PER_PAGE = 5


def order_history_kb(orders, page, total_pages):
    keyboard = history_page_kb(page, total_pages)
    for order in orders:
        if order.status == OrderStatus.SUCCESS and order.delivered_data:
            keyboard.inline_keyboard.append([InlineKeyboardButton(
                text=f"Данные заказа #{order.id}", callback_data=f"order_data:{order.id}")])
    return keyboard


@router.callback_query(F.data.startswith("order_data:"))
async def cb_order_data(callback: CallbackQuery, session: AsyncSession, db_user: User):
    try:
        order_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    order = await OrderRepo(session).get(order_id)
    if order is None or order.user_id != db_user.id or order.status != OrderStatus.SUCCESS or not order.delivered_data:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer_document(BufferedInputFile(
        order.delivered_data.encode("utf-8"), filename=f"atlas-order-{order.id}.txt"))


@router.message(F.text == "📜 История")
async def show_history(
    message: Message,
    session: AsyncSession,
    db_user: User,
) -> None:
    repo = OrderRepo(session)
    total = await repo.count_user_orders(db_user.id)
    if total == 0:
        await message.answer("📭 У вас пока нет заказов.")
        return

    total_pages = max(1, math.ceil(total / ORDERS_PER_PAGE))
    orders = await repo.get_user_orders(db_user.id, limit=ORDERS_PER_PAGE, offset=0)

    text = _build_history_text(orders, 0, total_pages)
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=order_history_kb(orders, 0, total_pages),
    )


@router.callback_query(F.data.startswith("history_page:"))
async def cb_history_page(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
) -> None:
    page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    repo = OrderRepo(session)
    total = await repo.count_user_orders(db_user.id)
    total_pages = max(1, math.ceil(total / ORDERS_PER_PAGE))
    page = min(page, total_pages - 1)

    orders = await repo.get_user_orders(
        db_user.id, limit=ORDERS_PER_PAGE, offset=page * ORDERS_PER_PAGE
    )

    text = _build_history_text(orders, page, total_pages)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        reply_markup=order_history_kb(orders, page, total_pages),
    )
    await callback.answer()


def _build_history_text(orders, page: int, total_pages: int) -> str:
    lines = [f"📜 <b>История заказов</b> (стр. {page + 1}/{total_pages})\n"]
    for o in orders:
        status_str = format_status(o.status)
        type_labels = {
            "catalog": "📦 Подписка / Товар",
            "steam": "🎮 Steam",
            "game": "🎮 Игра",
        }
        type_label = type_labels.get(o.type.value if hasattr(o.type, 'value') else o.type, o.type)

        line = (
            f"\n{'─' * 20}\n"
            f"#{o.id} | {type_label}\n"
            f"Сумма: {format_price(o.user_price)}\n"
            f"Статус: {status_str}\n"
            f"Дата: {format_datetime(o.created_at)}"
        )
        if o.delivered_data and o.status.value == "success":
            line += f"\n🔑 <code>{escape(o.delivered_data[:50])}</code>"
        lines.append(line)

    return "\n".join(lines)
