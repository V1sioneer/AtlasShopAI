from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Order, OrderStatus, OrderType, PartnerDeposit, SupplierCheckout
from bot.db.repo import OrderRepo
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.promotions import PromotionService
from bot.services.supplier_funding import require_supplier_funds, supplier_spend_lock
from bot.utils.money import money


class CheckoutError(ValueError):
    pass


RETRYABLE_ERRORS = {
    "OUT_OF_STOCK", "PRODUCT_NOT_FOUND", "PRICE_CHANGED", "INSUFFICIENT_BALANCE", "READ_FAILED",
}


class SupplierCheckoutService:
    def __init__(self, session: AsyncSession, api: PartnerAPIClient, markup: float):
        self.session = session
        self.api = api
        self.markup = markup
        self.orders = OrderRepo(session)
        self.promotions = PromotionService(session)

    async def get(self, checkout_id: int, user_id: int | None = None) -> tuple[SupplierCheckout, Order]:
        query = select(SupplierCheckout, Order).join(Order, Order.id == SupplierCheckout.order_id).where(
            SupplierCheckout.id == checkout_id
        ).execution_options(populate_existing=True)
        if user_id is not None:
            query = query.where(Order.user_id == user_id)
        row = (await self.session.execute(query)).first()
        if row is None:
            raise CheckoutError("Заказ не найден.")
        return row[0], row[1]

    async def _by_request(self, request_key: str, user_id: int):
        row = (await self.session.execute(
            select(SupplierCheckout, Order).join(Order, Order.id == SupplierCheckout.order_id)
            .where(Order.request_key == request_key, Order.user_id == user_id)
            .execution_options(populate_existing=True)
        )).first()
        return (row[0], row[1]) if row else None

    async def _state(self, checkout: SupplierCheckout, order: Order, status: str,
                     order_status: OrderStatus, error: str | None = None) -> None:
        checkout.status = status
        checkout.error_code = error
        checkout.updated_at = datetime.now(UTC).replace(tzinfo=None)
        order.status = order_status
        order.error_code = error
        await self.session.commit()

    async def create(
        self, user_id: int, product_id: int, *, expected_price: float, request_key: str,
    ) -> tuple[SupplierCheckout, Order]:
        existing = await self._by_request(request_key, user_id)
        if existing:
            return existing
        product = await self.api.get_product(product_id)
        if not product.in_stock or product.stock < 1:
            raise CheckoutError("Товар закончился. Счёт не создан, купон сохранён.")
        quote = await self.promotions.quote(user_id, product, 1, self.markup)
        if quote.claim_id is None or money(quote.payable_price) != money(product.price):
            raise CheckoutError("Прямая оплата доступна для одной покупки по промокоду без наценки.")
        if money(expected_price) != money(quote.payable_price):
            raise CheckoutError("Цена изменилась. Откройте карточку и подтвердите актуальную сумму.")
        try:
            order = await self.orders.create(
                user_id=user_id, order_type=OrderType.CATALOG,
                partner_price=product.price, user_price=quote.payable_price,
                product_id=product_id, qty=1, request_key=request_key,
                status=OrderStatus.WAITING_PAYMENT,
                payload={"payment_method": "supplier_crypto", "product_name": product.name,
                         "promo_code": quote.promo_code, "discount": quote.discount,
                         "regular_price": quote.regular_price},
            )
            if not await self.promotions.reserve_for_order(quote.claim_id, user_id, product_id, order.id):
                raise CheckoutError("Купон уже используется в другом заказе.")
            checkout = SupplierCheckout(order_id=order.id, amount_rub=quote.payable_price, status="creating")
            self.session.add(checkout)
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._by_request(request_key, user_id)
            if existing:
                return existing
            raise
        except Exception:
            await self.session.rollback()
            raise

        # Persist the intent before the API mutation. A crashed process cannot
        # generate a second invoice automatically for this same request.
        checkout_id, order_id = checkout.id, order.id
        try:
            invoice = await self.api.deposit_crypto(checkout.amount_rub)
            parsed = urlsplit(invoice.pay_url or "")
            if (
                money(invoice.amount_rub) != money(checkout.amount_rub)
                or invoice.deposit_id <= 0
                or invoice.status not in (None, "pending", "active")
                or parsed.scheme != "https"
                or parsed.hostname not in ("t.me", "pay.crypt.bot")
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
            ):
                raise CheckoutError("Поставщик вернул некорректный счёт. Оплата не запрашивалась.")
            if await self.session.scalar(select(PartnerDeposit.id).where(
                PartnerDeposit.deposit_id_partner == invoice.deposit_id
            )):
                raise CheckoutError("Этот счёт уже зарегистрирован в магазине.")
            checkout.deposit_id = invoice.deposit_id
            checkout.amount_usdt = invoice.amount_usdt
            checkout.pay_url = invoice.pay_url
            checkout.status = "awaiting_payment"
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            checkout, order = await self.get(checkout_id, user_id)
            # No payment link has been exposed. Returning the coupon does not
            # mint a balance or silently retry a possibly-created invoice.
            await self.promotions.restore_for_order(order_id)
            await self._state(checkout, order, "failed", OrderStatus.FAILED, "INVOICE_CREATION_FAILED")
            raise
        return checkout, order

    async def check(
        self, checkout_id: int, user_id: int | None = None, *, retry: bool = False,
    ) -> tuple[SupplierCheckout, Order]:
        # All supplier purchases in this process use this same lock. Database
        # transitions additionally protect against duplicated workers/clicks.
        async with supplier_spend_lock():
            checkout, order = await self.get(checkout_id, user_id)
            if checkout.status in ("delivered", "refunded", "failed", "fulfilling", "creating"):
                return checkout, order
            if checkout.status == "attention" and (
                not retry or checkout.error_code not in RETRYABLE_ERRORS or order.status == OrderStatus.UNCERTAIN
            ):
                return checkout, order
            if checkout.deposit_id is None:
                return checkout, order
            payment = await self.api.get_deposit(checkout.deposit_id)
            try:
                correct_amount = money(payment.amount_rub) == money(checkout.amount_rub)
            except ValueError:
                correct_amount = False
            if payment.deposit_id != checkout.deposit_id or payment.method != "crypto" or not correct_amount:
                await self._state(checkout, order, "attention", OrderStatus.ATTENTION, "PAYMENT_MISMATCH")
                return checkout, order
            if payment.status in ("expired", "cancelled", "canceled") and not checkout.supplier_paid:
                await self.promotions.restore_for_order(order.id)
                await self._state(checkout, order, "expired", OrderStatus.EXPIRED)
                return checkout, order
            if payment.status != "paid":
                return checkout, order
            if checkout.status == "expired":
                checkout.supplier_paid = True
                await self._state(checkout, order, "attention", OrderStatus.ATTENTION, "LATE_PAYMENT")
                return checkout, order
            checkout.supplier_paid = True
            await self.session.commit()
            await self._fulfill(checkout, order)
            return checkout, order

    async def _fulfill(self, checkout: SupplierCheckout, order: Order) -> None:
        try:
            product = await self.api.get_product(order.product_id)
            if product.id != order.product_id:
                raise PartnerAPIError("READ_FAILED", "Поставщик вернул другой товар")
            if not product.in_stock or product.stock < 1:
                raise PartnerAPIError("OUT_OF_STOCK", "Товар закончился", 409)
            if money(product.price) != money(checkout.amount_rub):
                raise PartnerAPIError("PRICE_CHANGED", "Закупочная цена изменилась", 409)
            await require_supplier_funds(self.session, self.api, checkout.amount_rub, exclude_checkout=checkout.id)
        except PartnerAPIError as exc:
            code = exc.code if exc.code in RETRYABLE_ERRORS else "READ_FAILED"
            await self._state(checkout, order, "attention", OrderStatus.ATTENTION, code)
            return

        claimed = await self.session.scalar(
            update(SupplierCheckout).where(
                SupplierCheckout.id == checkout.id,
                SupplierCheckout.status.in_(["awaiting_payment", "attention"]),
                SupplierCheckout.supplier_paid.is_(True),
            ).values(status="fulfilling", error_code=None).returning(SupplierCheckout.id)
        )
        if claimed is None:
            await self.session.rollback()
            return
        order.status = OrderStatus.PENDING
        await self.session.commit()
        try:
            result = await self.api.create_order(order.product_id, 1)
        except Exception as exc:
            uncertain = not isinstance(exc, PartnerAPIError) or exc.outcome_unknown
            await self._state(
                checkout, order, "attention", OrderStatus.UNCERTAIN if uncertain else OrderStatus.ATTENTION,
                getattr(exc, "code", "UNKNOWN_OUTCOME"),
            )
            return
        order.partner_order_id = result.order_id
        order.delivered_data = result.delivered_data
        if money(result.price) != money(checkout.amount_rub) or not result.delivered_data:
            await self._state(checkout, order, "attention", OrderStatus.UNCERTAIN, "PURCHASE_REQUIRES_REVIEW")
            return
        await self._state(checkout, order, "delivered", OrderStatus.SUCCESS)

    async def record_refund(self, checkout_id: int, amount: float) -> tuple[SupplierCheckout, Order]:
        """Admin records a refund already made outside the bot; never sends funds."""
        async with supplier_spend_lock():
            checkout, order = await self.get(checkout_id)
            if checkout.status == "refunded":
                return checkout, order
            if checkout.status != "attention" or not checkout.supplier_paid:
                raise CheckoutError("Возврат можно отметить только у оплаченного заказа на проверке.")
            if money(amount) != money(checkout.amount_rub):
                raise CheckoutError("Сумма должна совпадать с оплаченной суммой заказа.")
            await self.promotions.restore_for_order(order.id)
            await self._state(checkout, order, "refunded", OrderStatus.FAILED, "REFUND_CONFIRMED_BY_ADMIN")
            return checkout, order
