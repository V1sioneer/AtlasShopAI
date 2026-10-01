"""Keep buyer-funded deposits separate from the store's available funds."""
from __future__ import annotations

import asyncio
import weakref
from functools import wraps

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import SupplierCheckout
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.utils.money import money

_locks = weakref.WeakKeyDictionary()
PROTECTED_STATUSES = ("awaiting_payment", "fulfilling", "attention")


def supplier_spend_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


def with_supplier_spend_lock(function):
    @wraps(function)
    async def locked(*args, **kwargs):
        async with supplier_spend_lock():
            return await function(*args, **kwargs)
    return locked


async def protected_supplier_amount(
    session: AsyncSession, *, exclude_checkout: int | None = None, paid_only: bool = False,
) -> float:
    query = select(SupplierCheckout.amount_rub).where(
        SupplierCheckout.deposit_id.is_not(None),
        SupplierCheckout.status.in_(PROTECTED_STATUSES),
    )
    if exclude_checkout is not None:
        query = query.where(SupplierCheckout.id != exclude_checkout)
    if paid_only:
        query = query.where(SupplierCheckout.supplier_paid.is_(True))
    amounts = (await session.scalars(query)).all()
    return float(sum((money(amount) for amount in amounts), money(0)))


async def require_supplier_funds(
    session: AsyncSession, api: PartnerAPIClient, amount: float,
    *, exclude_checkout: int | None = None,
) -> None:
    # Wallet-funded purchases also reserve pending invoices conservatively:
    # the provider may have credited them before our next payment-status poll.
    protected = await protected_supplier_amount(
        session, exclude_checkout=exclude_checkout, paid_only=exclude_checkout is not None,
    )
    if protected == 0 and exclude_checkout is None:
        return
    balance = await api.get_balance()
    if money(balance.balance) < money(amount) + money(protected):
        raise PartnerAPIError(
            "INSUFFICIENT_BALANCE", "Средства поставщика зарезервированы за оплаченными заказами", 409
        )
