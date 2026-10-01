from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.promotions import PromotionError, PromotionService
from bot.utils.formatting import format_price

router = Router(name="promo")


class PromoState(StatesGroup):
    waiting_code = State()


async def activate_promotion_message(
    message: Message, code: str, session: AsyncSession, db_user: User,
    api: PartnerAPIClient, markup_percent: float,
) -> None:
    service = PromotionService(session)
    try:
        campaign = await service.get_campaign(code)
        if campaign is None:
            raise PromotionError("Промокод не найден. Проверьте написание.")
        product = await api.get_product(campaign.product_id)
        if product.price <= 0:
            raise PromotionError("Этот товар пока недоступен для акции.")
        campaign = await service.activate(code, db_user.id)
        quote = await service.quote(db_user.id, product, 1, markup_percent)
    except PromotionError as exc:
        await message.answer(escape(str(exc)))
        return
    except PartnerAPIError:
        await message.answer("Не удалось проверить товар. Купон не потрачен; попробуйте позже.")
        return
    stock_notice = "" if product.in_stock else "\nСейчас товара нет в наличии. Купон сохранён до покупки.\n"
    await message.answer(
        f"🎟 <b>Промокод {escape(campaign.code)} активирован</b>\n\n"
        f"Товар: <b>{escape(product.name)}</b>\n"
        "Одна штука по закупочной цене, без наценки магазина.\n"
        f"Сейчас к оплате: <b>{format_price(quote.payable_price)}</b>\n\n"
        "Скидка применится автоматически при покупке. "
        "Это купон на товар, а не пополнение баланса. "
        "Цена зависит от актуальной цены поставщика и показывается перед подтверждением.\n"
        f"{stock_notice}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть товар", callback_data=f"product:{product.id}:all")
        ]]),
    )


@router.message(Command("promo"))
async def cmd_promo(
    message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
    db_user: User, api: PartnerAPIClient, markup_percent: float,
) -> None:
    await state.clear()
    if command.args:
        await activate_promotion_message(message, command.args, session, db_user, api, markup_percent)
    else:
        await ask_promo(message, state)


@router.message(F.text == "🎟 Промокод")
async def ask_promo(message: Message, state: FSMContext) -> None:
    await state.set_state(PromoState.waiting_code)
    await message.answer("Введите промокод. Отмена — /cancel.")


@router.message(
    PromoState.waiting_code, F.text, ~F.text.startswith("/"),
    ~F.text.in_(["🛒 Каталог", "🎮 Steam / Игры", "💰 Мой баланс", "📜 История"]),
)
async def receive_promo(
    message: Message, state: FSMContext, session: AsyncSession, db_user: User,
    api: PartnerAPIClient, markup_percent: float,
) -> None:
    await state.clear()
    await activate_promotion_message(message, message.text, session, db_user, api, markup_percent)


@router.message(Command("promo_create"))
async def cmd_promo_create(
    message: Message, command: CommandObject, session: AsyncSession,
    api: PartnerAPIClient, admin_ids: list[int],
) -> None:
    if not message.from_user or message.from_user.id not in admin_ids:
        return
    parts = (command.args or "").split()
    if len(parts) != 3:
        await message.answer("Формат: /promo_create КОД ID_ТОВАРА ЛИМИТ\nОдна штука по закупочной цене на пользователя.")
        return
    try:
        product_id, limit = int(parts[1]), int(parts[2])
        product = await api.get_product(product_id)
        if product.price <= 0:
            raise PromotionError("Закупочная цена должна быть положительной.")
        campaign = await PromotionService(session).create(parts[0], product_id, limit)
    except (ValueError, PartnerAPIError) as exc:
        await message.answer(f"Не удалось создать акцию: {escape(str(exc))}")
        return
    await message.answer(
        f"Создан промокод <b>{escape(campaign.code)}</b>.\n"
        f"Товар: {escape(product.name)}\nЛимит: {campaign.max_claims} пользователей.\n"
        "Каждому — одна штука без наценки. Комиссии платёжек остаются расходом магазина."
    )


@router.message(Command("promo_stats"))
async def cmd_promo_stats(
    message: Message, command: CommandObject, session: AsyncSession, admin_ids: list[int],
) -> None:
    if not message.from_user or message.from_user.id not in admin_ids:
        return
    try:
        campaign, completed, reserved = await PromotionService(session).stats(command.args or "")
    except PromotionError as exc:
        await message.answer(f"{escape(str(exc))}\nФормат: /promo_stats КОД")
        return
    await message.answer(
        f"Промокод <b>{escape(campaign.code)}</b>\n"
        f"Выдано: {campaign.claimed_count}/{campaign.max_claims}\n"
        f"Успешных покупок: {completed}\nЗаказов в обработке или на проверке: {reserved}\n"
        f"Не использовано: {campaign.claimed_count - completed - reserved}\n"
        f"Новые активации: {'включены' if campaign.is_active else 'выключены'}"
    )


@router.message(Command("promo_disable"))
async def cmd_promo_disable(
    message: Message, command: CommandObject, session: AsyncSession, admin_ids: list[int],
) -> None:
    if not message.from_user or message.from_user.id not in admin_ids:
        return
    service = PromotionService(session)
    try:
        campaign = await service.get_campaign(command.args or "")
        if campaign is None:
            raise PromotionError("Промокод не найден.")
    except PromotionError as exc:
        await message.answer(f"{escape(str(exc))}\nФормат: /promo_disable КОД")
        return
    campaign.is_active = False
    await session.commit()
    await message.answer("Новые активации отключены. Уже выданные купоны продолжают действовать.")
