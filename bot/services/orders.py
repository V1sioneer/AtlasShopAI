from __future__ import annotations

from collections.abc import Awaitable, Callable
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Order, OrderStatus, OrderType
from bot.db.repo import OrderRepo, TransactionRepo, UserRepo
from bot.services.partner_api import ExternalOrder, OrderResult, PartnerAPIClient, PartnerAPIError
from bot.services.promotions import PromotionService
from bot.services.supplier_funding import require_supplier_funds, with_supplier_spend_lock
from bot.utils.money import money


class InsufficientUserBalance(Exception):
    pass


class DuplicateOrder(Exception):
    pass


class OrderOutcomeUnknown(Exception):
    pass


class PriceChanged(Exception):
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
        promotion_claim_id: int | None = None,
        supplier: str = "thegodshop",
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
            order.supplier = supplier
            if promotion_claim_id is not None:
                reserved = await PromotionService(self.session).reserve_for_order(
                    promotion_claim_id, user_id, product_id, order.id
                )
                if not reserved:
                    raise PriceChanged("Купон уже используется в другом заказе. Проверьте цену снова.")
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
            if hasattr(exc, "supplier_receipt"):
                payload = json.loads(order.payload_json or "{}")
                payload["supplier_receipt"] = exc.supplier_receipt
                order.payload_json = json.dumps(payload, ensure_ascii=False)
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
            await PromotionService(self.session).restore_for_order(order.id)
            await self.txns.create(
                user_id=order.user_id,
                delta=order.user_price,
                reason=f"Возврат: ошибка API ({exc.code})",
                order_id=order.id,
            )
        await self.session.commit()
        raise exc

    @with_supplier_spend_lock
    async def buy_catalog_product(
        self,
        user_id: int,
        product_id: int,
        qty: int = 1,
        *,
        request_key: str,
        expected_price: float | None = None,
    ) -> tuple[OrderResult, float]:
        if await self.orders.get_by_request_key(request_key):
            raise DuplicateOrder("Этот заказ уже был отправлен")
        product = await self.api.get_product(product_id)
        if not product.in_stock or product.stock < qty:
            raise PartnerAPIError("OUT_OF_STOCK", "Товар закончился", 409)
        partner_price = float(money(product.price) * qty)
        quote = await PromotionService(self.session).quote(user_id, product, qty, self.markup)
        user_price = quote.payable_price
        if expected_price is not None and money(expected_price) != money(user_price):
            raise PriceChanged("Цена или доступность купона изменились. Подтвердите новую цену.")
        prepare = getattr(self.api, "prepare_catalog_purchase", None)
        if prepare is not None:
            await prepare(self.session, product, qty, partner_price)
        else:
            await require_supplier_funds(self.session, self.api, partner_price)
        order = await self._reserve(
            user_id=user_id,
            order_type=OrderType.CATALOG,
            partner_price=partner_price,
            user_price=user_price,
            product_name=f"{product.name} x{qty}",
            request_key=request_key,
            product_id=product_id,
            qty=qty,
            payload={
                "product_id": product_id, "qty": qty,
                "regular_price": quote.regular_price,
                "promo_code": quote.promo_code,
                "discount": quote.discount,
                "supplier": product.supplier,
                "supplier_product_id": abs(product.id),
                "price_usd": product.price_usd,
                "usd_rub_rate": product.usd_rub_rate,
                "product_name": product.name,
            },
            promotion_claim_id=quote.claim_id,
            supplier=product.supplier,
        )
        try:
            purchase = getattr(self.api, "purchase_catalog", None)
            result = (await purchase(product, qty, request_key) if purchase is not None
                      else await self.api.create_order(product_id, qty))
        except Exception as exc:
            await self._provider_failed(order, exc)
            raise AssertionError("unreachable")
        await self.orders.update_status(
            order.id,
            OrderStatus.SUCCESS,
            partner_order_id=result.order_id if isinstance(result.order_id, int) else None,
            delivered_data=result.delivered_data,
        )
        order.supplier_order_ref = str(result.order_id)
        await self.session.commit()
        return result, user_price

    @with_supplier_spend_lock
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
        await require_supplier_funds(self.session, self.api, partner_price)
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
        await PromotionService(self.session).restore_for_order(order.id)
        await self.txns.create(
            user_id=order.user_id,
            delta=order.user_price,
            reason="Возврат: заказ не выполнен",
            order_id=order_id,
        )
        await self.session.commit()
        return True
