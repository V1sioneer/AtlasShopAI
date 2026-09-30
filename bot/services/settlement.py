from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Deposit
from bot.db.repo import DepositRepo, TransactionRepo, UserRepo
from bot.utils.money import money


class InvalidPayment(ValueError):
    pass


@dataclass(frozen=True)
class Settlement:
    applied: bool
    user_id: int
    amount: float
    balance: float


async def settle_payment(
    session: AsyncSession, method: str, external_id: str, payment: dict,
    *, user_id: int | None = None,
) -> Settlement:
    """Validate provider data, then claim and credit a deposit exactly once."""
    repo = DepositRepo(session)
    dep = await repo.get_by_external_id(external_id, method)
    if dep is None or (user_id is not None and dep.user_id != user_id):
        raise InvalidPayment("Платёж не найден или принадлежит другому пользователю")
    identifier = payment.get("invoice_id") if method == "cryptobot" else payment.get("payment_id")
    expected_status = "paid" if method == "cryptobot" else "succeeded"
    try:
        valid_amount = money(payment.get("amount")) == money(dep.amount_rub) and money(dep.amount_rub) > 0
    except ValueError:
        valid_amount = False
    if (str(identifier) != external_id or payment.get("status") != expected_status
            or payment.get("currency") != "RUB" or not valid_amount):
        raise InvalidPayment("Реквизиты оплаты не совпадают с сохранённым счётом")
    metadata = payment.get("metadata", {})
    if metadata.get("user_id") is not None and str(metadata["user_id"]) != str(dep.user_id):
        raise InvalidPayment("Владелец оплаты не совпадает с владельцем счёта")
    payload = payment.get("payload")
    if payload and str(payload) != f"{dep.user_id}:{int(dep.amount_rub)}":
        raise InvalidPayment("Данные оплаты не совпадают с сохранённым счётом")
    deposit_id, owner, amount = dep.id, dep.user_id, dep.amount_rub
    # Release the read transaction before competing for the write claim.
    await session.commit()
    try:
        applied = await repo.mark_paid(deposit_id)
        if applied:
            balance = await UserRepo(session).update_balance(owner, amount)
            await TransactionRepo(session).create(
                user_id=owner, delta=amount, reason=f"Пополнение {method} #{external_id}",
            )
        else:
            await session.rollback()
            user = await UserRepo(session).get(owner)
            balance = user.balance_rub if user else 0.0
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return Settlement(applied, owner, amount, balance)


async def expire_payment(session: AsyncSession, method: str, external_id: str) -> None:
    await session.execute(
        update(Deposit).where(Deposit.method == method, Deposit.external_id == external_id,
                              Deposit.status == "pending").values(status="expired")
    )
    await session.commit()
