from __future__ import annotations

import math

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.keyboards.kb import (
    catalog_page_kb,
    categories_menu_kb,
    confirm_purchase_kb,
    product_card_kb,
)
from bot.services.orders import (
    DuplicateOrder,
    InsufficientUserBalance,
    OrderOutcomeUnknown,
    OrderService,
)
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError, Product
from bot.services.pricing import calculate_user_price
from bot.utils.formatting import format_price

logger = structlog.get_logger()

router = Router(name="catalog")

ITEMS_PER_PAGE = 6


class BuyQtyState(StatesGroup):
    waiting_qty = State()


# ── Categories & Catalog list ────────────────────────────────────────


@router.message(F.text.in_(["🛒 Каталог", "🛒 Каталог подписок", "🛒 Каталог товаров"]))
async def show_catalog(
    message: Message,
    api: PartnerAPIClient,
) -> None:
    try:
        products = await api.get_products()
    except PartnerAPIError as exc:
        await message.answer(f"⚠️ Не удалось загрузить каталог: {exc.message}")
        return

    if not products:
        await message.answer("📭 Каталог пуст.")
        return

    categories = list({p.category for p in products if p.category})
    await message.answer(
        "🛒 <b>Каталог товаров</b>\n\n"
        "Выберите интересующую категорию подписки:",
        parse_mode="HTML",
        reply_markup=categories_menu_kb(categories),
    )


@router.callback_query(F.data == "catalog_cats")
async def cb_catalog_cats(
    callback: CallbackQuery,
    api: PartnerAPIClient,
) -> None:
    try:
        products = await api.get_products()
    except PartnerAPIError as exc:
        await callback.answer(f"Ошибка: {exc.message}", show_alert=True)
        return

    categories = list({p.category for p in products if p.category})
    await callback.message.edit_text(  # type: ignore[union-attr]
        "🛒 <b>Каталог товаров</b>\n\n"
        "Выберите интересующую категорию подписки:",
        parse_mode="HTML",
        reply_markup=categories_menu_kb(categories),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_category_page(
    callback: CallbackQuery,
    api: PartnerAPIClient,
    markup_percent: float,
) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    category = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    try:
        products = await api.get_products()
    except PartnerAPIError as exc:
        await callback.answer(f"Ошибка: {exc.message}", show_alert=True)
        return

    if category != "all":
        filtered = [p for p in products if p.category == category]
    else:
        filtered = products

    if not filtered:
        await callback.answer("В этой категории пока нет товаров.", show_alert=True)
        return

    total_pages = max(1, math.ceil(len(filtered) / ITEMS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * ITEMS_PER_PAGE
    page_products = filtered[start : start + ITEMS_PER_PAGE]

    title = f"📁 <b>{category}</b>" if category != "all" else "📦 <b>Все товары</b>"
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"{title}\n\nВыберите товар для покупки:",
        parse_mode="HTML",
        reply_markup=catalog_page_kb(page_products, category, page, total_pages, markup_percent),
    )
    await callback.answer()


# ── Product card ─────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("product:"))
async def cb_product_card(
    callback: CallbackQuery,
    api: PartnerAPIClient,
    markup_percent: float,
) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    product_id = int(parts[1])
    category = parts[2] if len(parts) > 2 else "all"

    try:
        product = await api.get_product(product_id)
    except PartnerAPIError as exc:
        await callback.answer(f"Ошибка: {exc.message}", show_alert=True)
        return

    user_price = calculate_user_price(product.price, markup_percent)
    stock_text = f"✅ В наличии ({product.stock} шт.)" if product.in_stock else "❌ Нет в наличии"

    cat_display = product.category or category
    text = (
        f"📦 <b>{product.name}</b>\n\n"
        f"📁 Категория: <b>{cat_display}</b>\n"
        f"💰 Цена: <b>{format_price(user_price)}</b>\n"
        f"📊 {stock_text}\n"
    )

    await callback.message.edit_text(  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        reply_markup=product_card_kb(product_id, category),
    )
    await callback.answer()


# ── Buy 1 piece ──────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("buy_catalog:"))
async def cb_buy_catalog(
    callback: CallbackQuery,
    api: PartnerAPIClient,
    markup_percent: float,
) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    product_id = int(parts[1])
    qty = int(parts[2])

    try:
        product = await api.get_product(product_id)
    except PartnerAPIError:
        await callback.answer("Товар не найден", show_alert=True)
        return

    user_price = calculate_user_price(product.price, markup_percent) * qty

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"🛒 <b>Подтверждение покупки</b>\n\n"
        f"Товар: {product.name}\n"
        f"Количество: {qty}\n"
        f"Сумма: <b>{format_price(user_price)}</b>\n\n"
        f"Подтвердить покупку?",
        parse_mode="HTML",
        reply_markup=confirm_purchase_kb(product_id, qty),
    )
    await callback.answer()


# ── Buy N pieces (ask quantity) ──────────────────────────────────────


@router.callback_query(F.data.startswith("buy_catalog_qty:"))
async def cb_buy_catalog_qty(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    product_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    await state.set_state(BuyQtyState.waiting_qty)
    await state.update_data(product_id=product_id)

    await callback.message.edit_text(  # type: ignore[union-attr]
        "📦 Введите количество (от 1 до 99):",
    )
    await callback.answer()


@router.message(BuyQtyState.waiting_qty)
async def process_qty(
    message: Message,
    state: FSMContext,
    api: PartnerAPIClient,
    markup_percent: float,
) -> None:
    text = message.text or ""
    if not text.isdigit() or int(text) < 1 or int(text) > 99:
        await message.answer("❌ Введите число от 1 до 99.")
        return

    qty = int(text)
    data = await state.get_data()
    product_id = data["product_id"]
    await state.clear()

    try:
        product = await api.get_product(product_id)
    except PartnerAPIError:
        await message.answer("⚠️ Товар не найден.")
        return

    user_price = calculate_user_price(product.price, markup_percent) * qty

    await message.answer(
        f"🛒 <b>Подтверждение покупки</b>\n\n"
        f"Товар: {product.name}\n"
        f"Количество: {qty}\n"
        f"Сумма: <b>{format_price(user_price)}</b>\n\n"
        f"Подтвердить покупку?",
        parse_mode="HTML",
        reply_markup=confirm_purchase_kb(product_id, qty),
    )


# ── Confirm catalog purchase ────────────────────────────────────────


@router.callback_query(F.data.startswith("confirm_catalog:"))
async def cb_confirm_catalog(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    api: PartnerAPIClient,
    markup_percent: float,
    admin_ids: list[int],
) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    product_id = int(parts[1])
    qty = int(parts[2])

    svc = OrderService(session, api, markup_percent)
    request_key = (
        f"catalog:{db_user.id}:{callback.message.chat.id}:"
        f"{callback.message.message_id}:{product_id}:{qty}"
    )

    try:
        result, user_price = await svc.buy_catalog_product(
            user_id=db_user.id,
            product_id=product_id,
            qty=qty,
            request_key=request_key,
        )
    except InsufficientUserBalance as exc:
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"❌ Недостаточно средств.\n{exc}",
        )
        await callback.answer()
        return
    except DuplicateOrder:
        await callback.answer("Этот заказ уже отправлен на обработку.", show_alert=True)
        return
    except OrderOutcomeUnknown:
        await callback.message.edit_text(
            "⚠️ Поставщик получил запрос, но итоговый статус пока неизвестен. "
            "Средства сохранены за заказом; поддержка проверит его вручную."
        )  # type: ignore[union-attr]
        for aid in admin_ids:
            try:
                await callback.bot.send_message(  # type: ignore[union-attr]
                    aid,
                    f"⚠️ Неопределённый заказ пользователя {db_user.id}: "
                    f"товар {product_id}, количество {qty}",
                )
            except Exception:
                pass
        await callback.answer()
        return
    except PartnerAPIError as exc:
        error_text = f"⚠️ Ошибка: {exc.message}"
        if exc.code == "INSUFFICIENT_BALANCE":
            error_text = "⚠️ Временно недоступно, попробуйте позже."
            # Alert admin
            bot = callback.bot
            for aid in admin_ids:
                try:
                    await bot.send_message(  # type: ignore[union-attr]
                        aid,
                        f"🚨 <b>INSUFFICIENT_BALANCE</b>\n"
                        f"Партнёрский баланс недостаточен!\n"
                        f"Пользователь: {db_user.id} (@{db_user.username})\n"
                        f"Товар ID: {product_id}, кол-во: {qty}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        elif exc.code == "OUT_OF_STOCK":
            error_text = "❌ Товар закончился."
        elif exc.code == "PRODUCT_NOT_FOUND":
            error_text = "❌ Товар не найден."

        await callback.message.edit_text(error_text)  # type: ignore[union-attr]
        await callback.answer()
        return

    delivered = result.delivered_data or "—"
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"💰 Списано: {format_price(user_price)}\n"
        f"📦 Заказ #{result.order_id}\n\n"
        f"🔑 Ваши данные:\n"
        f"<code>{delivered}</code>",
        parse_mode="HTML",
    )
    await callback.answer()

    if callback.bot:
        uname = f"@{db_user.username}" if db_user.username else "нет юзернейма"
        name = db_user.first_name or "Пользователь"
        for aid in admin_ids:
            try:
                await callback.bot.send_message(
                    aid,
                    f"🛒 <b>НОВАЯ ПОКУПКА В КАТАЛОГЕ!</b>\n\n"
                    f"👤 Покупатель: <b>{name}</b> ({uname})\n"
                    f"🆔 ID: <code>{db_user.id}</code>\n"
                    f"📦 Заказ: #{result.order_id}\n"
                    f"💰 Сумма: <b>{format_price(user_price)}</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
