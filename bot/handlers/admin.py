from __future__ import annotations

import structlog
from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.repo import PartnerDepositRepo, UserRepo, TransactionRepo
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.utils.formatting import format_price
from bot.utils.money import money

logger = structlog.get_logger()

router = Router(name="admin")


def _is_admin(user_id: int, admin_ids: list[int]) -> bool:
    return user_id in admin_ids



# ── /admin & /stats ──────────────────────────────────────────────────


@router.message(Command("admin"))
@router.message(Command("stats"))
async def cmd_admin_stats(
    message: Message,
    session: AsyncSession,
    api: PartnerAPIClient,
    admin_ids: list[int],
    dispatcher: Dispatcher,
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    from bot.db.repo import OrderRepo, DepositRepo

    user_repo = UserRepo(session)
    total_users = await user_repo.count()
    today_users = await user_repo.count_today()

    order_repo = OrderRepo(session)
    total_orders = await order_repo.count()

    dep_repo = DepositRepo(session)
    total_deposits, sum_deposits = await dep_repo.get_paid_stats()

    markup = dispatcher.get("markup_percent", 15.0)

    try:
        bal = await api.get_balance()
        partner_bal_str = f"<b>{format_price(bal.balance)}</b> (скидка {bal.discount_percent}%)"
    except Exception:
        partner_bal_str = "<i>недоступен</i>"

    text = (
        f"👑 <b>Панель администратора</b>\n\n"
        f"📊 <b>Статистика пользователей:</b>\n"
        f"• Всего пользователей в БД: <b>{total_users}</b>\n"
        f"• Новых за сегодня: <b>{today_users}</b>\n\n"
        f"📦 <b>Заказы:</b>\n"
        f"• Всего оформлено заказов: <b>{total_orders}</b>\n\n"
        f"💰 <b>Пополнения баланса:</b>\n"
        f"• Оплачено счетов: <b>{total_deposits}</b>\n"
        f"• На общую сумму: <b>{format_price(sum_deposits)}</b>\n\n"
        f"💼 <b>Партнёрский баланс:</b> {partner_bal_str}\n"
        f"🏷 <b>Текущая наценка:</b> <b>{markup}%</b>\n\n"
        f"🛠 <b>Доступные команды:</b>\n"
        f"• <code>/set_markup 20</code> — изменить % наценки\n"
        f"• <code>/topup_user ID СУММА</code> — выдать баланс пользователю\n"
        f"• <code>/promo_create КОД ID ЛИМИТ</code> — товар без наценки\n"
        f"• <code>/promo_stats КОД</code> — статистика акции\n"
        f"• <code>/promo_disable КОД</code> — остановить новые активации\n"
        f"• <code>/broadcast ТЕКСТ</code> — рассылка всем пользователям\n"
        f"• <code>/partner_balance</code> — проверить баланс поставщика\n"
        f"• <code>/deposit_crypto 1000</code> — пополнить баланс API"
    )
    await message.answer(text, parse_mode="HTML")


# ── /set_markup ──────────────────────────────────────────────────────


@router.message(Command("set_markup"))
async def cmd_set_markup(
    message: Message,
    admin_ids: list[int],
    dispatcher: Dispatcher,
    state: FSMContext | None = None,
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return
    if state:
        await state.clear()

    args = (message.text or "").split()
    current = dispatcher.get("markup_percent", 15.0)

    if len(args) < 2:
        await message.answer(
            f"📊 Текущая наценка: <b>{current}%</b>\n\n"
            f"Чтобы изменить, напишите: <code>/set_markup 25</code>",
            parse_mode="HTML",
        )
        return

    try:
        new_val = float(money(args[1].replace("%", "")))
        if new_val < 0 or new_val > 500:
            await message.answer("❌ Наценка должна быть от 0% до 500%.")
            return

        dispatcher["markup_percent"] = new_val
        await message.answer(
            f"✅ Наценка успешно обновлена: <b>{new_val}%</b>!\n"
            f"Цены в каталоге пересчитаны.",
            parse_mode="HTML",
        )
    except ValueError:
        await message.answer("❌ Введите число, например: <code>/set_markup 20</code>")


# ── /partner_balance ─────────────────────────────────────────────────


@router.message(Command("partner_balance"))
async def cmd_partner_balance(
    message: Message,
    api: PartnerAPIClient,
    admin_ids: list[int],
    state: FSMContext | None = None,
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return
    if state:
        await state.clear()

    try:
        bal = await api.get_balance()
    except PartnerAPIError as exc:
        await message.answer(f"⚠️ Ошибка: {exc.message}")
        return

    await message.answer(
        f"💼 <b>Партнёрский баланс</b>\n\n"
        f"Баланс: <b>{format_price(bal.balance)}</b>\n"
        f"Скидка: {bal.discount_percent}%",
        parse_mode="HTML",
    )


# ── /partner_history ─────────────────────────────────────────────────


@router.message(Command("partner_history"))
async def cmd_partner_history(
    message: Message,
    api: PartnerAPIClient,
    admin_ids: list[int],
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    args = (message.text or "").split()
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20

    try:
        history = await api.get_history(limit)
    except PartnerAPIError as exc:
        await message.answer(f"⚠️ Ошибка: {exc.message}")
        return

    if not history:
        await message.answer("📭 История пуста.")
        return

    lines = ["📋 <b>История партнёра</b>\n"]
    for h in history[:20]:
        lines.append(f"• {h}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ── /topup_user ──────────────────────────────────────────────────────


@router.message(Command("topup_user"))
async def cmd_topup_user(
    message: Message,
    session: AsyncSession,
    admin_ids: list[int],
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            "Использование: /topup_user <tg_id> <сумма>\n"
            "Пример: /topup_user 123456789 500"
        )
        return

    try:
        tg_id = int(args[1])
        amount = float(money(args[2]))
    except ValueError:
        await message.answer("❌ Неверные аргументы.")
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть положительной.")
        return

    user_repo = UserRepo(session)
    user = await user_repo.get(tg_id)
    if user is None:
        await message.answer(f"❌ Пользователь {tg_id} не найден в БД.")
        return

    new_balance = await user_repo.update_balance(tg_id, amount)

    tx_repo = TransactionRepo(session)
    await tx_repo.create(
        user_id=tg_id,
        delta=amount,
        reason=f"Пополнение от админа ({message.from_user.id})",  # type: ignore[union-attr]
    )
    await session.commit()

    await message.answer(
        f"✅ Баланс пользователя {tg_id} пополнен на {format_price(amount)}\n"
        f"Новый баланс: {format_price(new_balance)}"
    )

    # Notify user
    try:
        await message.bot.send_message(  # type: ignore[union-attr]
            tg_id,
            f"💰 Ваш баланс пополнен на {format_price(amount)}!\n"
            f"Текущий баланс: {format_price(new_balance)}",
        )
    except Exception:
        pass


# ── /deposit_crypto ──────────────────────────────────────────────────


@router.message(Command("deposit_crypto"))
async def cmd_deposit_crypto(
    message: Message,
    session: AsyncSession,
    api: PartnerAPIClient,
    admin_ids: list[int],
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /deposit_crypto <сумма_руб>")
        return

    try:
        amount_rub = float(money(args[1]))
        if amount_rub <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return

    try:
        result = await api.deposit_crypto(amount_rub)
    except PartnerAPIError as exc:
        await message.answer(f"⚠️ Ошибка: {exc.message}")
        return

    # Save to DB
    dep_repo = PartnerDepositRepo(session)
    await dep_repo.create(
        deposit_id_partner=result.deposit_id,
        method="crypto",
        amount_rub=result.amount_rub,
        amount_usdt=result.amount_usdt,
        pay_url=result.pay_url,
    )
    await session.commit()

    await message.answer(
        f"💳 <b>Крипто-депозит создан</b>\n\n"
        f"ID: {result.deposit_id}\n"
        f"Сумма: {format_price(result.amount_rub)} ({result.amount_usdt} USDT)\n"
        f"Ссылка для оплаты:\n{result.pay_url}",
        parse_mode="HTML",
    )


# ── /deposit_ton ─────────────────────────────────────────────────────


@router.message(Command("deposit_ton"))
async def cmd_deposit_ton(
    message: Message,
    session: AsyncSession,
    api: PartnerAPIClient,
    admin_ids: list[int],
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /deposit_ton <сумма_руб>")
        return

    try:
        amount_rub = float(money(args[1]))
        if amount_rub <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return

    try:
        result = await api.deposit_ton(amount_rub)
    except PartnerAPIError as exc:
        await message.answer(f"⚠️ Ошибка: {exc.message}")
        return

    dep_repo = PartnerDepositRepo(session)
    await dep_repo.create(
        deposit_id_partner=result.deposit_id,
        method="ton",
        amount_rub=result.amount_rub,
        amount_usdt=result.amount_usdt,
        wallet=result.wallet,
        memo=result.memo,
    )
    await session.commit()

    await message.answer(
        f"💳 <b>TON-депозит создан</b>\n\n"
        f"ID: {result.deposit_id}\n"
        f"Сумма: {format_price(result.amount_rub)} ({result.amount_usdt} USDT)\n"
        f"Кошелёк: <code>{result.wallet}</code>\n"
        f"Комментарий (memo): <code>{result.memo}</code>\n\n"
        f"⚠️ Memo обязателен!",
        parse_mode="HTML",
    )


# ── /check_deposit ───────────────────────────────────────────────────


@router.message(Command("check_deposit"))
async def cmd_check_deposit(
    message: Message,
    session: AsyncSession,
    api: PartnerAPIClient,
    admin_ids: list[int],
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /check_deposit <deposit_id>")
        return

    try:
        deposit_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный ID.")
        return

    try:
        result = await api.get_deposit(deposit_id)
    except PartnerAPIError as exc:
        await message.answer(f"⚠️ Ошибка: {exc.message}")
        return

    status_emoji = "✅" if result.status == "paid" else "⏳"
    await message.answer(
        f"💳 <b>Статус депозита #{deposit_id}</b>\n\n"
        f"Статус: {status_emoji} {result.status}\n"
        f"Сумма: {format_price(result.amount_rub)} ({result.amount_usdt} USDT)",
        parse_mode="HTML",
    )

    # Update local DB
    if result.status == "paid":
        dep_repo = PartnerDepositRepo(session)
        await dep_repo.update_status(deposit_id, "paid")
        await session.commit()


# ── /broadcast ───────────────────────────────────────────────────────


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message,
    session: AsyncSession,
    admin_ids: list[int],
) -> None:
    if not _is_admin(message.from_user.id, admin_ids):  # type: ignore[union-attr]
        return

    # Get broadcast text (everything after /broadcast)
    text = (message.text or "")[len("/broadcast"):].strip()
    if not text:
        await message.answer("Использование: /broadcast <текст сообщения>")
        return

    from sqlalchemy import select
    from bot.db.models import User

    result = await session.execute(
        select(User.id).where(User.is_blocked == False)  # noqa: E712
    )
    user_ids = result.scalars().all()

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await message.bot.send_message(uid, text, parse_mode="HTML")  # type: ignore[union-attr]
            sent += 1
        except Exception:
            failed += 1

    await message.answer(
        f"📢 Рассылка завершена\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )
