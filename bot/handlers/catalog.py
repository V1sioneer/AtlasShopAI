from __future__ import annotations

import math
from html import escape

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.catalog_browser import (
    SUPPLIERS, SUPPLIER_LABELS, category_token, favorite_ids, filter_products,
    preferences, set_favorite, supplier_code, supplier_products,
)
from bot.keyboards.catalog import (
    categories_keyboard, product_keyboard, products_keyboard, supplier_menu,
)
from bot.keyboards.kb import (
    confirm_purchase_kb,
    product_card_kb,
)
from bot.services.orders import (
    DuplicateOrder,
    InsufficientUserBalance,
    OrderOutcomeUnknown,
    OrderService,
    PriceChanged,
)
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError, Product
from bot.services.promotions import CatalogQuote, PromotionService
from bot.services.payments import CryptoBotPayment
from bot.utils.formatting import format_price
from bot.utils.money import money

logger = structlog.get_logger()

router = Router(name="catalog")

ITEMS_PER_PAGE = 6


class CatalogSearchState(StatesGroup):
    waiting_query = State()


class BuyQtyState(StatesGroup):
    waiting_qty = State()


def quote_text(quote: CatalogQuote) -> str:
    if quote.claim_id is None:
        return f"Сумма: <b>{format_price(quote.payable_price)}</b>\n"
    return (
        f"Обычная сумма: <s>{format_price(quote.regular_price)}</s>\n"
        f"Промокод {escape(quote.promo_code)}: −{format_price(quote.discount)}\n"
        "Одна штука по закупочной цене, без наценки магазина.\n"
        f"К оплате: <b>{format_price(quote.payable_price)}</b>\n"
    )


def direct_available(quote: CatalogQuote, product: Product, qty: int, enabled: bool,
                     store_payments_enabled: bool = True) -> bool:
    cost = money(product.price) * qty
    return (enabled and product.direct_payment_supported and product.supplier == "thegodshop"
            and 1 <= qty <= 99 and cost > 0 and money(quote.payable_price) >= cost
            and (money(quote.payable_price) == cost or store_payments_enabled))


def payment_note(quote, product, qty, direct):
    if not direct:
        return ""
    cost = float(money(product.price) * qty)
    margin = float(money(quote.payable_price) - money(cost))
    if margin == 0:
        return "Оплата одним счётом в CryptoBot, без пополнения баланса бота.\n\n"
    return ("Оплата в CryptoBot двумя счетами:\n"
            f"1. Сервисный сбор магазина — {format_price(margin)}.\n"
            f"2. Оплата поставщику — {format_price(cost)}.\n"
            "Товар выдаётся после оплаты обоих счетов.\n\n")


async def edit_catalog_message(callback, text, **kwargs):
    try:
        await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


# Supplier choice, categories, search, stock filter and favorites.


@router.message(F.text.in_(["🛒 Каталог", "🛒 Каталог подписок", "🛒 Каталог товаров"]))
async def show_catalog(message: Message, api: PartnerAPIClient, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛒 <b>Каталог товаров</b>\n\nВыберите поставщика:",
                         parse_mode="HTML", reply_markup=supplier_menu())


@router.callback_query(F.data == "catalog_cats")
async def cb_catalog_cats(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("🛒 <b>Каталог товаров</b>\n\nВыберите поставщика:",
                                    reply_markup=supplier_menu())
    await callback.answer()


async def show_supplier(callback, api, session, user_id, code):
    prefs = await preferences(session, user_id)
    products = await supplier_products(api, code)
    prefs.supplier = SUPPLIERS[code]
    await session.commit()
    filtered = filter_products(products, prefs)
    search = f"\nПоиск: <b>{escape(prefs.search_query)}</b>" if prefs.search_query else ""
    text = (f"<b>{SUPPLIER_LABELS[code]}</b>\n\n"
            f"Товаров: {len(products)}, по текущим фильтрам: {len(filtered)}.{search}\n"
            "Выберите категорию или откройте все товары.")
    await edit_catalog_message(callback, text, reply_markup=categories_keyboard(products, code, prefs))


@router.callback_query(F.data.startswith("supplier:"))
async def cb_supplier(callback: CallbackQuery, api: PartnerAPIClient,
                      session: AsyncSession, db_user: User, state: FSMContext):
    await state.clear()
    try:
        await show_supplier(callback, api, session, db_user.id, callback.data.split(":")[1])
    except (PartnerAPIError, KeyError) as exc:
        await callback.answer(getattr(exc, "message", "Поставщик не найден"), show_alert=True)
        return
    await callback.answer()


async def render_page(callback, api, session, user_id, markup, code, token, page, favorites=False):
    prefs = await preferences(session, user_id)
    if favorites:
        ids = await favorite_ids(session, user_id)
        products = [p for p in await api.get_products() if p.id in ids]
        title = "⭐ Избранное"
    else:
        products = filter_products(await supplier_products(api, code), prefs, token)
        title = SUPPLIER_LABELS[code]
    pages = max(1, math.ceil(len(products) / ITEMS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    visible = products[page * ITEMS_PER_PAGE:(page + 1) * ITEMS_PER_PAGE]
    prices = {p.id: (await PromotionService(session).quote(user_id, p, 1, markup)).payable_price
              for p in visible}
    text = f"<b>{title}</b>\n\n"
    if prefs.search_query and not favorites:
        text += f"Поиск: {escape(prefs.search_query)}\n"
    if prefs.in_stock_only and not favorites:
        text += "Показаны только товары в наличии.\n"
    text += "Выберите товар:" if products else "Товаров по текущим фильтрам нет."
    await edit_catalog_message(callback, text, reply_markup=products_keyboard(
        visible, prices, code, token, page, pages, prefs, favorites=favorites))


@router.callback_query(F.data.startswith("cat:"))
async def cb_category_page(callback: CallbackQuery, api: PartnerAPIClient, markup_percent: float,
                           session: AsyncSession, db_user: User) -> None:
    parts = callback.data.split(":")
    try:
        if len(parts) == 4 and parts[1] in SUPPLIERS:
            code, token, page = parts[1], parts[2], int(parts[3])
        else:
            # Existing messages keep working after the catalog upgrade.
            prefs = await preferences(session, db_user.id)
            code = "a" if prefs.supplier == "aethel" else "g"
            token = "all" if parts[1] == "all" else category_token(parts[1])
            page = int(parts[2]) if len(parts) > 2 else 0
        await render_page(callback, api, session, db_user.id, markup_percent, code, token, page)
    except (PartnerAPIError, ValueError, IndexError) as exc:
        await callback.answer(getattr(exc, "message", "Обновите каталог."), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("catalog_stock:"))
@router.callback_query(F.data.startswith("catalog_clear:"))
async def cb_catalog_filter(callback: CallbackQuery, api: PartnerAPIClient,
                            session: AsyncSession, db_user: User):
    action, code = callback.data.split(":")
    if code not in SUPPLIERS:
        await callback.answer("Поставщик не найден.", show_alert=True)
        return
    prefs = await preferences(session, db_user.id)
    if action == "catalog_stock":
        prefs.in_stock_only = not prefs.in_stock_only
    else:
        prefs.search_query = ""
    await session.commit()
    try:
        await show_supplier(callback, api, session, db_user.id, code)
    except PartnerAPIError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("catalog_search:"))
async def cb_catalog_search(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[1]
    if code not in SUPPLIERS:
        await callback.answer("Поставщик не найден.", show_alert=True)
        return
    await state.set_state(CatalogSearchState.waiting_query)
    await state.update_data(supplier=code)
    await callback.message.answer("Введите название товара для поиска. Отмена — /cancel.")
    await callback.answer()


@router.message(CatalogSearchState.waiting_query, F.text, ~F.text.startswith("/"),
                ~F.text.in_(["🛒 Каталог", "🎮 Steam / Игры", "💰 Мой баланс", "📜 История", "ℹ️ Информация", "🎟 Промокод"]))
async def receive_catalog_search(message: Message, state: FSMContext, api: PartnerAPIClient,
                                 session: AsyncSession, db_user: User, markup_percent: float):
    query = (message.text or "").strip()
    if not query or len(query) > 200:
        await message.answer("Введите запрос длиной от 1 до 200 символов.")
        return
    code = (await state.get_data()).get("supplier", "g")
    await state.clear()
    prefs = await preferences(session, db_user.id)
    prefs.search_query, prefs.supplier = query, SUPPLIERS[code]
    await session.commit()
    try:
        products = filter_products(await supplier_products(api, code), prefs)
    except PartnerAPIError as exc:
        await message.answer(escape(exc.message))
        return
    visible = products[:ITEMS_PER_PAGE]
    prices = {p.id: (await PromotionService(session).quote(db_user.id, p, 1, markup_percent)).payable_price
              for p in visible}
    await message.answer(f"🔎 <b>{escape(query)}</b> · найдено: {len(products)}",
                         reply_markup=products_keyboard(visible, prices, code, "all", 0,
                                                        max(1, math.ceil(len(products)/ITEMS_PER_PAGE)), prefs))


@router.callback_query(F.data.startswith("favorites:"))
async def cb_favorites(callback: CallbackQuery, api: PartnerAPIClient,
                       session: AsyncSession, db_user: User, markup_percent: float):
    try:
        await render_page(callback, api, session, db_user.id, markup_percent, "g", "all",
                          int(callback.data.split(":")[1]), favorites=True)
    except (PartnerAPIError, ValueError) as exc:
        await callback.answer(getattr(exc, "message", "Откройте избранное снова."), show_alert=True)
        return
    await callback.answer()


async def show_product(callback, product, session, user_id, markup, category):
    quote = await PromotionService(session).quote(user_id, product, 1, markup)
    stock = f"✅ В наличии ({product.stock} шт.)" if product.in_stock else "❌ Нет в наличии"
    text = (f"📦 <b>{escape(product.name)}</b>\n\n"
            f"{SUPPLIER_LABELS[supplier_code(product.id)]}\n"
            f"Категория: {escape(product.category or 'Другое')}\n"
            f"{quote_text(quote)}{stock}\n")
    full_terms = len(escape(product.description)) > 2300
    if product.description:
        excerpt = product.description[:600] + "…\nПолный текст доступен по кнопке ниже." if full_terms else product.description
        text += "\n<b>Условия товара</b>\n" + escape(excerpt)
    await edit_catalog_message(callback, text, reply_markup=product_keyboard(
        product, category, product.id in await favorite_ids(session, user_id), full_terms=full_terms))


@router.callback_query(F.data.startswith("product:"))
async def cb_product_card(callback: CallbackQuery, api: PartnerAPIClient, markup_percent: float,
                          session: AsyncSession, db_user: User) -> None:
    try:
        parts = callback.data.split(":")
        product = await api.get_product(int(parts[1]))
        category = parts[2] if len(parts) > 2 else "all"
        if category not in ("fav", "all") and len(category) != 12:
            category = category_token(category)
        await show_product(callback, product, session, db_user.id, markup_percent, category)
    except (PartnerAPIError, ValueError) as exc:
        await callback.answer(getattr(exc, "message", "Товар не найден."), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("fav_add:"))
@router.callback_query(F.data.startswith("fav_del:"))
async def cb_change_favorite(callback: CallbackQuery, api: PartnerAPIClient,
                             session: AsyncSession, db_user: User, markup_percent: float):
    try:
        action, identifier, category = callback.data.split(":")
        product = await api.get_product(int(identifier))
        await set_favorite(session, db_user.id, product.id, action == "fav_add")
        await show_product(callback, product, session, db_user.id, markup_percent, category)
    except (PartnerAPIError, ValueError) as exc:
        await callback.answer(getattr(exc, "message", "Товар не найден."), show_alert=True)
        return
    await callback.answer("Сохранено" if action == "fav_add" else "Удалено из избранного")


@router.callback_query(F.data.startswith("product_terms:"))
async def cb_product_terms(callback: CallbackQuery, api: PartnerAPIClient):
    try:
        product = await api.get_product(int(callback.data.split(":")[1]))
    except (PartnerAPIError, ValueError):
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer_document(BufferedInputFile(
        product.description.encode("utf-8"), filename="atlas-product-terms.txt"))


# ── Buy 1 piece ──────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("buy_catalog:"))
async def cb_buy_catalog(
    callback: CallbackQuery,
    api: PartnerAPIClient,
    markup_percent: float,
    session: AsyncSession,
    db_user: User,
    direct_supplier_checkout_enabled: bool = True,
    cryptobot: CryptoBotPayment | None = None,
) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    product_id = int(parts[1])
    qty = int(parts[2])
    if not 1 <= qty <= 99:
        await callback.answer("Количество должно быть от 1 до 99.", show_alert=True)
        return

    try:
        product = await api.get_product(product_id)
    except PartnerAPIError:
        await callback.answer("Товар не найден", show_alert=True)
        return

    quote = await PromotionService(session).quote(db_user.id, product, qty, markup_percent)
    direct = direct_available(quote, product, qty, direct_supplier_checkout_enabled, cryptobot is not None)
    note = payment_note(quote, product, qty, direct)

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"🛒 <b>Подтверждение покупки</b>\n\n"
        f"Товар: {escape(product.name)}\n"
        f"Количество: {qty}\n"
        f"{quote_text(quote)}\n"
        f"{note}"
        f"Подтвердить покупку?",
        parse_mode="HTML",
        reply_markup=confirm_purchase_kb(product_id, qty, quote.payable_price, direct,
                                        wallet_available=product.supplier != "thegodshop"),
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
    session: AsyncSession,
    db_user: User,
    direct_supplier_checkout_enabled: bool = True,
    cryptobot: CryptoBotPayment | None = None,
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

    quote = await PromotionService(session).quote(db_user.id, product, qty, markup_percent)
    direct = direct_available(quote, product, qty, direct_supplier_checkout_enabled, cryptobot is not None)
    note = payment_note(quote, product, qty, direct)

    await message.answer(
        f"🛒 <b>Подтверждение покупки</b>\n\n"
        f"Товар: {escape(product.name)}\n"
        f"Количество: {qty}\n"
        f"{quote_text(quote)}\n"
        f"{note}"
        f"Подтвердить покупку?",
        parse_mode="HTML",
        reply_markup=confirm_purchase_kb(product_id, qty, quote.payable_price, direct,
                                        wallet_available=product.supplier != "thegodshop"),
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

    if len(parts) != 4:
        await callback.answer("Обновите карточку товара и подтвердите актуальную цену.", show_alert=True)
        return
    if not 1 <= qty <= 99:
        await callback.answer("Откройте карточку товара снова.", show_alert=True)
        return
    if product_id > 0:
        await callback.message.edit_text(
            "Для этого товара теперь доступна оплата напрямую в CryptoBot. "
            "Откройте карточку и выберите покупку; пополнять баланс бота не требуется.",
            reply_markup=product_card_kb(product_id),
        )
        await callback.answer()
        return
    expected_price = float(parts[3])

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
            expected_price=expected_price,
        )
    except PriceChanged:
        await callback.message.edit_text(
            "Цена или доступность купона изменились. Откройте карточку и подтвердите актуальную сумму.",
            reply_markup=product_card_kb(product_id),
        )
        await callback.answer()
        return
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
    saved_order = await svc.orders.get_by_request_key(request_key)
    order_number = saved_order.id if saved_order else result.order_id
    inline_data = escape(delivered)
    long_delivery = len(inline_data) > 2800
    delivery_text = ("Данные заказа отправлены файлом ниже. Их также можно скачать в истории."
                     if long_delivery else f"<code>{inline_data}</code>")
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"💰 Списано: {format_price(user_price)}\n"
        f"📦 Заказ #{order_number}\n\n"
        f"🔑 Ваши данные:\n"
        f"{delivery_text}",
        parse_mode="HTML",
    )
    if long_delivery:
        await callback.message.answer_document(BufferedInputFile(
            delivered.encode("utf-8"), filename=f"atlas-order-{order_number}.txt"))
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
                    f"📦 Заказ: #{order_number}\n"
                    f"💰 Сумма: <b>{format_price(user_price)}</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
