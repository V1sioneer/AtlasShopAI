from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.db.repo import DepositRepo
from bot.services.payments import CryptoBotPayment, YooKassaPayment
from bot.services.settlement import InvalidPayment, expire_payment, settle_payment
from bot.utils.formatting import format_price
from bot.utils.money import topup_amount

logger = structlog.get_logger()

router = Router(name="payment")

TOPUP_AMOUNTS = [100, 250, 500, 1000, 2500, 5000]


class TopupState(StatesGroup):
    waiting_amount = State()


# ── Entry point (from balance handler) ───────────────────────────────


@router.callback_query(F.data == "topup_balance")
async def cb_topup_balance(callback: CallbackQuery) -> None:
    buttons = []
    row = []
    for i, amt in enumerate(TOPUP_AMOUNTS):
        row.append(
            InlineKeyboardButton(
                text=f"{amt} ₽", callback_data=f"topup_amount:{amt}"
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(
            text="✏️ Своя сумма", callback_data="topup_custom"
        )
    ])
    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")
    ])

    await callback.message.edit_text(  # type: ignore[union-attr]
        "💳 <b>Пополнение баланса</b>\n\n"
        "Выберите сумму:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


# ── Custom amount ────────────────────────────────────────────────────


@router.callback_query(F.data == "topup_custom")
async def cb_topup_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TopupState.waiting_amount)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "✏️ Введите сумму пополнения в рублях (от 50 до 50000):"
    )
    await callback.answer()


@router.message(TopupState.waiting_amount)
async def process_topup_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        amount = topup_amount(text)
    except ValueError as exc:
        await message.answer(f"❌ {exc}.")
        return
    await state.clear()
    await _show_payment_methods(message, amount)


# ── Amount selected ──────────────────────────────────────────────────


@router.callback_query(F.data.startswith("topup_amount:"))
async def cb_topup_amount(callback: CallbackQuery) -> None:
    try:
        amount = topup_amount(callback.data.split(":")[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    await _show_payment_methods_edit(callback, amount)
    await callback.answer()


async def _show_payment_methods(message: Message, amount: int) -> None:
    kb = _payment_methods_kb(amount)
    await message.answer(
        f"💳 <b>Пополнение на {format_price(amount)}</b>\n\n"
        f"Выберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _show_payment_methods_edit(callback: CallbackQuery, amount: int) -> None:
    kb = _payment_methods_kb(amount)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"💳 <b>Пополнение на {format_price(amount)}</b>\n\n"
        f"Выберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=kb,
    )


def _payment_methods_kb(amount: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="💳 Банковская карта / СБП",
                callback_data=f"pay_yookassa:{amount}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤖 CryptoBot (криптовалюта)",
                callback_data=f"pay_crypto:{amount}",
            ),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="topup_balance"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════════════════════
# CryptoBot
# ═══════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("pay_crypto:"))
async def cb_pay_crypto(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    cryptobot: CryptoBotPayment | None,
    admin_ids: list[int],
) -> None:
    if not cryptobot:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⚠️ CryptoBot не настроен. Обратитесь к администратору."
        )
        await callback.answer()
        return

    try:
        amount = topup_amount(callback.data.split(":")[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    try:
        invoice = await cryptobot.create_invoice(
            amount=amount,
            description=f"Пополнение баланса на {amount} ₽",
            payload=f"{db_user.id}:{amount}",
        )
    except Exception as exc:
        logger.error("cryptobot_create_error", error=str(exc))
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⚠️ Ошибка создания платежа. Попробуйте позже."
        )
        await callback.answer()
        return

    # Save deposit
    dep_repo = DepositRepo(session)
    await dep_repo.create(
        user_id=db_user.id,
        amount_rub=amount,
        method="cryptobot",
        external_id=str(invoice["invoice_id"]),
        pay_url=invoice["pay_url"],
    )
    await session.commit()

    # Notify admins
    if callback.bot and db_user.id not in admin_ids:
        uname = f"@{db_user.username}" if db_user.username else "нет юзернейма"
        name = db_user.first_name or "Пользователь"
        for aid in admin_ids:
            try:
                await callback.bot.send_message(
                    aid,
                    f"💳 <b>Запрос на пополнение баланса</b>\n\n"
                    f"👤 Пользователь: <b>{name}</b> ({uname})\n"
                    f"🆔 ID: <code>{db_user.id}</code>\n"
                    f"💵 Сумма: <b>{format_price(amount)}</b>\n"
                    f"🤖 Способ: CryptoBot",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"🤖 <b>Оплата через CryptoBot</b>\n\n"
        f"Сумма: <b>{format_price(amount)}</b>\n\n"
        f"Нажмите кнопку ниже для оплаты.\n"
        f"После оплаты баланс пополнится автоматически (до 1 мин).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💰 Оплатить", url=invoice["pay_url"])],
                [InlineKeyboardButton(
                    text="🔄 Проверить оплату",
                    callback_data=f"check_crypto:{invoice['invoice_id']}",
                )],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("check_crypto:"))
async def cb_check_crypto(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    cryptobot: CryptoBotPayment | None,
    admin_ids: list[int],
) -> None:
    if not cryptobot:
        await callback.answer("CryptoBot не настроен", show_alert=True)
        return

    try:
        invoice_id = int(callback.data.split(":", 2)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.answer("Некорректный идентификатор платежа", show_alert=True)
        return

    try:
        invoice = await cryptobot.get_invoice(invoice_id)
    except Exception:
        await callback.answer("Ошибка проверки", show_alert=True)
        return

    if invoice["status"] == "paid":
        try:
            settlement = await settle_payment(
                session, "cryptobot", str(invoice_id), invoice, user_id=db_user.id,
            )
        except InvalidPayment as exc:
            logger.warning("cryptobot_settlement_rejected", invoice_id=invoice_id, error=str(exc))
            await callback.answer("Платёж не прошёл проверку. Обратитесь в поддержку.", show_alert=True)
            return
        if settlement.applied:

            # Notify admins
            if callback.bot:
                uname = f"@{db_user.username}" if db_user.username else "нет юзернейма"
                name = db_user.first_name or "Пользователь"
                for aid in admin_ids:
                    try:
                        await callback.bot.send_message(
                            aid,
                            f"💰 <b>ПОПОЛНЕНИЕ БАЛАНСА!</b>\n\n"
                            f"👤 Пользователь: <b>{name}</b> ({uname})\n"
                            f"🆔 ID: <code>{db_user.id}</code>\n"
                            f"💵 Зачислено: <b>+{format_price(settlement.amount)}</b>\n"
                            f"📈 Новый баланс: <b>{format_price(settlement.balance)}</b>\n"
                            f"🤖 Способ: CryptoBot",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

            await callback.message.edit_text(  # type: ignore[union-attr]
                f"✅ <b>Оплата получена!</b>\n\n"
                f"Зачислено: {format_price(settlement.amount)}\n"
                f"Баланс: {format_price(settlement.balance)}",
                parse_mode="HTML",
            )
        else:
            await callback.answer("✅ Уже зачислено!", show_alert=True)
            return
    elif invoice["status"] == "expired":
        await expire_payment(session, "cryptobot", str(invoice_id))
        await callback.message.edit_text("❌ Счёт истёк. Создайте новый.")  # type: ignore[union-attr]
    else:
        await callback.answer("⏳ Оплата ещё не получена. Подождите.", show_alert=True)
        return

    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════
# YooKassa (карта / СБП)
# ═══════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("pay_yookassa:"))
async def cb_pay_yookassa(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    yookassa: YooKassaPayment | None,
) -> None:
    if not yookassa:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⚠️ Оплата картой/СБП временно недоступна."
        )
        await callback.answer()
        return

    try:
        amount = topup_amount(callback.data.split(":")[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    try:
        payment = await yookassa.create_payment(
            amount=amount,
            description=f"Пополнение баланса на {amount} ₽",
            metadata={"user_id": str(db_user.id), "amount": str(amount)},
        )
    except Exception as exc:
        logger.error("yookassa_create_error", error=str(exc))
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⚠️ Ошибка создания платежа. Попробуйте позже."
        )
        await callback.answer()
        return

    # Save deposit
    dep_repo = DepositRepo(session)
    await dep_repo.create(
        user_id=db_user.id,
        amount_rub=amount,
        method="yookassa",
        external_id=payment["payment_id"],
        pay_url=payment["confirmation_url"],
    )
    await session.commit()

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"💳 <b>Оплата картой / СБП</b>\n\n"
        f"Сумма: <b>{format_price(amount)}</b>\n\n"
        f"Нажмите кнопку для оплаты.\n"
        f"После оплаты нажмите «Проверить».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить", url=payment["confirmation_url"])],
                [InlineKeyboardButton(
                    text="🔄 Проверить оплату",
                    callback_data=f"check_yookassa:{payment['payment_id']}",
                )],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("check_yookassa:"))
async def cb_check_yookassa(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    yookassa: YooKassaPayment | None,
) -> None:
    if not yookassa:
        await callback.answer("ЮKassa не настроена", show_alert=True)
        return

    try:
        payment_id = callback.data.split(":", 2)[1]  # type: ignore[union-attr]
        if not payment_id:
            raise ValueError
    except (ValueError, IndexError):
        await callback.answer("Некорректный идентификатор платежа", show_alert=True)
        return

    try:
        payment = await yookassa.get_payment(payment_id)
    except Exception:
        await callback.answer("Ошибка проверки", show_alert=True)
        return

    if payment["status"] == "succeeded":
        try:
            settlement = await settle_payment(
                session, "yookassa", payment_id, payment, user_id=db_user.id,
            )
        except InvalidPayment as exc:
            logger.warning("yookassa_settlement_rejected", payment_id=payment_id, error=str(exc))
            await callback.answer("Платёж не прошёл проверку. Обратитесь в поддержку.", show_alert=True)
            return
        if settlement.applied:

            await callback.message.edit_text(  # type: ignore[union-attr]
                f"✅ <b>Оплата получена!</b>\n\n"
                f"Зачислено: {format_price(settlement.amount)}\n"
                f"Баланс: {format_price(settlement.balance)}",
                parse_mode="HTML",
            )
        else:
            await callback.answer("✅ Уже зачислено!", show_alert=True)
            return
    elif payment["status"] == "canceled":
        await expire_payment(session, "yookassa", payment_id)
        await callback.message.edit_text("❌ Платёж отменён.")  # type: ignore[union-attr]
    else:
        await callback.answer("⏳ Оплата ещё не получена. Подождите.", show_alert=True)
        return

    await callback.answer()
