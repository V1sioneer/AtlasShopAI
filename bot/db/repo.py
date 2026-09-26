from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Deposit,
    GameProduct,
    Order,
    OrderStatus,
    OrderType,
    PartnerDeposit,
    Transaction,
    User,
)


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> User:
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        is_new = False
        if user is None:
            user = User(
                id=user_id,
                username=username,
                first_name=first_name,
                balance_rub=0.0,
            )
            self.session.add(user)
            await self.session.flush()
            is_new = True
        else:
            if username is not None:
                user.username = username
            if first_name is not None:
                user.first_name = first_name
            await self.session.flush()
        user._is_new = is_new
        return user

    async def count(self) -> int:
        stmt = select(func.count(User.id))
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def count_today(self) -> int:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = select(func.count(User.id)).where(User.created_at >= today_start)
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get(self, user_id: int) -> Optional[User]:
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_balance(self, user_id: int, delta: float) -> float:
        """Atomically update user balance. Returns new balance."""
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(balance_rub=User.balance_rub + delta)
            .returning(User.balance_rub)
        )
        result = await self.session.execute(stmt)
        new_balance = result.scalar_one()
        await self.session.flush()
        return new_balance


class OrderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count(self) -> int:
        stmt = select(func.count(Order.id))
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def create(
        self,
        user_id: int,
        order_type: OrderType,
        partner_price: float,
        user_price: float,
        product_id: Optional[int] = None,
        variation_id: Optional[int] = None,
        qty: int = 1,
        payload: Optional[dict] = None,
        status: OrderStatus = OrderStatus.PENDING,
    ) -> Order:
        order = Order(
            user_id=user_id,
            type=order_type,
            product_id=product_id,
            variation_id=variation_id,
            qty=qty,
            payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
            partner_price=partner_price,
            user_price=user_price,
            status=status,
        )
        self.session.add(order)
        await self.session.flush()
        return order

    async def update_status(
        self,
        order_id: int,
        status: OrderStatus,
        partner_order_id: Optional[int] = None,
        delivered_data: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> Optional[Order]:
        stmt = select(Order).where(Order.id == order_id)
        result = await self.session.execute(stmt)
        order = result.scalar_one_or_none()
        if order is None:
            return None
        order.status = status
        order.updated_at = datetime.utcnow()
        if partner_order_id is not None:
            order.partner_order_id = partner_order_id
        if delivered_data is not None:
            order.delivered_data = delivered_data
        if error_code is not None:
            order.error_code = error_code
        await self.session.flush()
        return order

    async def get_processing_external(self) -> Sequence[Order]:
        """Get all orders with status=processing for polling."""
        stmt = (
            select(Order)
            .where(
                Order.status == OrderStatus.PROCESSING,
                Order.type.in_(
                    [OrderType.STEAM, OrderType.GAME]
                ),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_user_orders(
        self, user_id: int, limit: int = 20, offset: int = 0
    ) -> Sequence[Order]:
        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count_user_orders(self, user_id: int) -> int:
        stmt = select(func.count()).select_from(Order).where(Order.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def get(self, order_id: int) -> Optional[Order]:
        stmt = select(Order).where(Order.id == order_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class TransactionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        delta: float,
        reason: str,
        order_id: Optional[int] = None,
    ) -> Transaction:
        tx = Transaction(
            user_id=user_id,
            delta=delta,
            reason=reason,
            order_id=order_id,
        )
        self.session.add(tx)
        await self.session.flush()
        return tx


class PartnerDepositRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        deposit_id_partner: int,
        method: str,
        amount_rub: float,
        amount_usdt: Optional[float] = None,
        pay_url: Optional[str] = None,
        wallet: Optional[str] = None,
        memo: Optional[str] = None,
    ) -> PartnerDeposit:
        dep = PartnerDeposit(
            deposit_id_partner=deposit_id_partner,
            method=method,
            amount_rub=amount_rub,
            amount_usdt=amount_usdt,
            pay_url=pay_url,
            wallet=wallet,
            memo=memo,
            status="pending",
        )
        self.session.add(dep)
        await self.session.flush()
        return dep

    async def get_pending(self) -> Sequence[PartnerDeposit]:
        stmt = select(PartnerDeposit).where(PartnerDeposit.status == "pending")
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def update_status(self, deposit_id_partner: int, status: str) -> None:
        stmt = (
            update(PartnerDeposit)
            .where(PartnerDeposit.deposit_id_partner == deposit_id_partner)
            .values(status=status)
        )
        await self.session.execute(stmt)
        await self.session.flush()


class GameProductRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all_active(self) -> Sequence[GameProduct]:
        stmt = (
            select(GameProduct)
            .where(GameProduct.is_active == True)  # noqa: E712
            .order_by(GameProduct.name)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_variation_id(self, variation_id: int) -> Optional[GameProduct]:
        stmt = select(GameProduct).where(GameProduct.variation_id == variation_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        variation_id: int,
        name: str,
        price: float,
        description: Optional[str] = None,
        fields_schema: Optional[str] = None,
    ) -> GameProduct:
        gp = GameProduct(
            variation_id=variation_id,
            name=name,
            price=price,
            description=description,
            fields_schema=fields_schema,
        )
        self.session.add(gp)
        await self.session.flush()
        return gp


class DepositRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_paid_stats(self) -> tuple[int, float]:
        stmt = select(func.count(Deposit.id), func.coalesce(func.sum(Deposit.amount_rub), 0.0)).where(Deposit.status == "paid")
        result = await self.session.execute(stmt)
        row = result.one()
        return row[0], float(row[1])

    async def create(
        self,
        user_id: int,
        amount_rub: float,
        method: str,
        external_id: Optional[str] = None,
        pay_url: Optional[str] = None,
    ) -> Deposit:
        dep = Deposit(
            user_id=user_id,
            amount_rub=amount_rub,
            method=method,
            external_id=external_id,
            pay_url=pay_url,
            status="pending",
        )
        self.session.add(dep)
        await self.session.flush()
        return dep

    async def get_pending_by_method(self, method: str) -> Sequence[Deposit]:
        stmt = select(Deposit).where(
            Deposit.status == "pending",
            Deposit.method == method,
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_external_id(self, external_id: str) -> Optional[Deposit]:
        stmt = select(Deposit).where(Deposit.external_id == external_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_paid(self, deposit_id: int) -> None:
        stmt = (
            update(Deposit)
            .where(Deposit.id == deposit_id)
            .values(status="paid")
        )
        await self.session.execute(stmt)
        await self.session.flush()

