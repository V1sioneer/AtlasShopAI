from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Order, OrderStatus, OrderType
from bot.db.repo import OrderRepo, TransactionRepo, UserRepo
from bot.services.partner_api import ExternalOrder, OrderResult, PartnerAPIClient, PartnerAPIError
from bot.services.pricing import calculate_user_price


class InsufficientUserBalance(Exception):
    pass


class DuplicateOrder(Exception):
    pass


class OrderOutcomeUnknown(Exception):
    pass


class OrderService:
    def __init__(self, session: AsyncSession, api: PartnerAPIClient, markup_percent: float) -> None:
        self.session = session
        self.api = api
        self.markup = markup_percent
        self.users = UserRepo(session)
        self.orders = OrderRepo(session)
        self.txns = TransactionRepo(session)

    async def _reserve(
        self,
        *,
        user_id: int,
        order_type: OrderType,
        partner_price: float,
        user_price: float,
        product_name: str,
        request_key: str,
        product_id: int | None = None,
        variation_id: int | None = None,
        qty: int = 1,
        payload: dict | None = None,
    ) -> Order:
        if await self.orders.get_by_request_key(request_key):
            raise DuplicateOrder("Этот заказ уже был отправлен")
        try:
            order = await self.orders.create(
                user_id=user_id,
                order_type=order_type,
                partner_price=partner_price,
                user_price=user_price,
                product_id=product_id,
                variation_id=variation_id,
                qty=qty,
                payload=payload,
                status=OrderStatus.PENDING,
                request_key=request_key,
            )
            balance = await self.users.debit(user_id, user_price)
            if balance is None:
                await self.session.rollback()
                user = await self.users.get(user_id)
                current = user.balance_rub if user else 0.0
                raise InsufficientUserBalance(
                    f"Нужно {user_price:.0f} ₽, на балансе {current:.0f} ₽"
                )
            await self.txns.create(
                user_id=user_id,
                delta=-user_price,
                reason=f"Покупка: {product_name}",
                order_id=order.id,
            )
            await self.session.commit()
            return order
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateOrder("Этот заказ уже был отправлен") from exc
        except Exception:
            if self.session.in_transaction():
                await self.session.rollback()
            raise

    async def _provider_failed(self, order: Order, exc: Exception) -> None:
        unknown = not isinstance(exc, PartnerAPIError) or exc.outcome_unknown
        if unknown:
            await self.orders.transition(
                order.id,
                [OrderStatus.PENDING],
                OrderStatus.UNCERTAIN,
                error_code=getattr(exc, "code", "UNKNOWN_OUTCOME"),
            )
            await self.session.commit()
            raise OrderOutcomeUnknown("Результат заказа уточняется") from exc

        refunded = await self.orders.transition(
            order.id,
            [OrderStatus.PENDING],
            OrderStatus.FAILED,
            error_code=exc.code,
        )
        if refunded:
            await self.users.update_balance(order.user_id, order.user_price)
            await self.txns.create(
                user_id=order.user_id,
                delta=order.user_price,
                reason=f"Возврат: ошибка API ({exc.code})",
                order_id=order.id,
            )
        await self.session.commit()
        raise exc

    async def buy_catalog_product(
        self,
        user_id: int,
        product_id: int,
        qty: int = 1,
        *,
        request_key: str,
    ) -> tuple[OrderResult, float]:
        product = await self.api.get_product(product_id)
        partner_price = product.price * qty
        user_price = calculate_user_price(product.price, self.markup) * qty
        order = await self._reserve(
            user_id=user_id,
            order_type=OrderType.CATALOG,
            partner_price=partner_price,
            user_price=user_price,
            product_name=f"{product.name} x{qty}",
            request_key=request_key,
            product_id=product_id,
            qty=qty,
            payload={"product_id": product_id, "qty": qty},
        )
        try:
            result = await self.api.create_order(product_id, qty)
        except Exception as exc:
            await self._provider_failed(order, exc)
            raise AssertionError("unreachable")
        await self.orders.update_status(
            order.id,
            OrderStatus.SUCCESS,
            partner_order_id=result.order_id,
            delivered_data=result.delivered_data,
        )
        await self.session.commit()
        return result, user_price

    async def buy_external(
        self,
        user_id: int,
        order_type: OrderType,
        user_price: float,
        partner_price: float,
        api_call: Callable[[], Awaitable[ExternalOrder]],
        product_name: str,
        *,
        request_key: str,
        product_id: int | None = None,
        variation_id: int | None = None,
        payload: dict | None = None,
    ) -> tuple[ExternalOrder, int]:
        order = await self._reserve(
            user_id=user_id,
            order_type=order_type,
            partner_price=partner_price,
            user_price=user_price,
            product_name=product_name,
            request_key=request_key,
            product_id=product_id,
            variation_id=variation_id,
            payload=payload,
        )
        try:
            result = await api_call()
        except Exception as exc:
            await self._provider_failed(order, exc)
            raise AssertionError("unreachable")
        await self.orders.update_status(
            order.id,
            OrderStatus.PROCESSING,
            partner_order_id=result.order_id,
        )
        await self.session.commit()
        return result, order.id

    async def refund_order(self, order_id: int) -> bool:
        order = await self.orders.get(order_id)
        if order is None:
            return False
        refunded = await self.orders.transition(
            order_id, [OrderStatus.PROCESSING], OrderStatus.FAILED
        )
        if not refunded:
            await self.session.rollback()
            return False
        await self.users.update_balance(order.user_id, order.user_price)
        await self.txns.create(
            user_id=order.user_id,
            delta=order.user_price,
            reason="Возврат: заказ не выполнен",
            order_id=order_id,
        )
        await self.session.commit()
        return True
