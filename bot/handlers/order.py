from __future__ import annotations

import re

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import OrderType, User
from bot.keyboards.kb import confirm_external_kb, steam_games_menu_kb
from bot.services.orders import (
    DuplicateOrder,
    InsufficientUserBalance,
    OrderOutcomeUnknown,
    OrderService,
)
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.pricing import calculate_user_price
from bot.utils.money import money
from bot.utils.formatting import format_price

logger = structlog.get_logger()

router = Router(name="order")

STEAM_LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{2,64}$")


class SteamBuyState(StatesGroup):
    waiting_login = State()
    waiting_amount = State()
    confirming = State()


class GameBuyState(StatesGroup):
    waiting_variation_id = State()


# ── Entry point ──────────────────────────────────────────────────────


@router.message(F.text == "🎮 Steam / Игры")
async def show_steam_menu(message: Message) -> None:
    await message.answer(
        "🎮 <b>Steam и Игры</b>\n\n"
        "Выберите раздел:",
        parse_mode="HTML",
        reply_markup=steam_games_menu_kb(),
    )


# ── Steam topup ──────────────────────────────────────────────────────


@router.callback_query(F.data == "steam_topup")
async def cb_steam_topup(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SteamBuyState.waiting_login)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "💳 <b>Пополнение Steam</b>\n\n"
        "Введите логин Steam:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SteamBuyState.waiting_login)
async def process_steam_login(
    message: Message,
    state: FSMContext,
) -> None:
    login = (message.text or "").strip()
    if not STEAM_LOGIN_RE.match(login):
        await message.answer("❌ Неверный формат логина Steam. Попробуйте ещё:")
        return

    await state.update_data(login=login)
    await state.set_state(SteamBuyState.waiting_amount)
    await message.answer(
        f"Логин: <b>{login}</b>\n\n"
        f"Введите сумму пополнения в рублях (от 100 до 15000):",
        parse_mode="HTML",
    )


@router.message(SteamBuyState.waiting_amount)
async def process_steam_amount(
    message: Message,
    state: FSMContext,
    steam_min_amount: int,
    steam_max_amount: int,
    markup_percent: float,
) -> None:
    text = (message.text or "").strip()
    try:
        amount_decimal = money(text)
        amount = int(amount_decimal)
        if amount_decimal != amount:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число.")
        return

    if amount < steam_min_amount or amount > steam_max_amount:
        await message.answer(
            f"❌ Сумма должна быть от {steam_min_amount} до {steam_max_amount} ₽."
        )
        return

    data = await state.get_data()
    login = data["login"]
    await state.update_data(amount=amount)
    await state.set_state(SteamBuyState.confirming)
    user_price = calculate_user_price(amount, markup_percent)

    await message.answer(
        f"💳 <b>Подтверждение пополнения Steam</b>\n\n"
        f"Логин: {login}\n"
        f"На аккаунт Steam: {amount} ₽\n"
        f"К оплате с баланса: <b>{format_price(user_price)}</b>\n\n"
        f"Подтвердить?",
        parse_mode="HTML",
        reply_markup=confirm_external_kb("steam"),
    )


@router.callback_query(F.data == "confirm_ext:steam", SteamBuyState.confirming)
async def cb_confirm_steam(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    api: PartnerAPIClient,
    markup_percent: float,
    admin_ids: list[int],
) -> None:
    data = await state.get_data()
    login = data.get("login")
    amount = data.get("amount")
    if not isinstance(login, str) or not isinstance(amount, int):
        await state.clear()
        await callback.answer("Сессия оформления истекла. Начните заново.", show_alert=True)
        return
    user_price = calculate_user_price(amount, markup_percent)
    request_key = (
        f"steam:{db_user.id}:{callback.message.chat.id}:"
        f"{callback.message.message_id}:{login}:{amount}"
    )
    service = OrderService(session, api, markup_percent)
    try:
        result, local_order_id = await service.buy_external(
            user_id=db_user.id,
            order_type=OrderType.STEAM,
            user_price=user_price,
            partner_price=float(amount),
            api_call=lambda: api.buy_steam(login, float(amount)),
            product_name=f"Steam {login}: {amount} ₽",
            request_key=request_key,
            payload={"login": login, "amount_rub": amount},
        )
    except InsufficientUserBalance as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except DuplicateOrder:
        await state.clear()
        await callback.answer("Этот заказ уже отправлен на обработку.", show_alert=True)
        return
    except OrderOutcomeUnknown:
        await state.clear()
        await callback.message.edit_text(
            "⚠️ Статус пополнения уточняется. Средства закреплены за заказом; "
            "повторно оформлять его не нужно."
        )  # type: ignore[union-attr]
        for admin_id in admin_ids:
            try:
                await callback.bot.send_message(  # type: ignore[union-attr]
                    admin_id,
                    f"⚠️ Неопределённое пополнение Steam: пользователь {db_user.id}, "
                    f"логин {login}, сумма {amount} ₽",
                )
            except Exception:
                pass
        await callback.answer()
        return
    except PartnerAPIError as exc:
        await state.clear()
        await callback.message.edit_text(f"❌ Пополнение не выполнено: {exc.message}")  # type: ignore[union-attr]
        await callback.answer()
        return

    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✅ <b>Пополнение Steam принято</b>\n\n"
        f"Логин: <code>{login}</code>\n"
        f"Сумма: {amount} ₽\n"
        f"Списано: {format_price(user_price)}\n"
        f"Заказ: #{local_order_id} / поставщик #{result.order_id}",
        parse_mode="HTML",
    )
    await callback.answer()


# ── Games ────────────────────────────────────────────────────────────


@router.callback_query(F.data == "games_list")
async def cb_games_list(callback: CallbackQuery) -> None:
    await callback.message.edit_text(  # type: ignore[union-attr]
        "🎮 <b>Покупка игр</b>\n\n"
        "Раздел временно закрыт на доработку. Готовые товары доступны в каталоге.",
        parse_mode="HTML",
    )
    await callback.answer()
