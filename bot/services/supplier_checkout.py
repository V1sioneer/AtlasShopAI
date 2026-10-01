from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Deposit, Order, OrderStatus, OrderType, PartnerDeposit, SupplierCheckout
from bot.db.repo import OrderRepo
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError
from bot.services.promotions import PromotionService
from bot.services.payments import CryptoBotPayment
from bot.services.supplier_funding import require_supplier_funds, supplier_spend_lock
from bot.utils.money import money


class CheckoutError(ValueError):
    pass


RETRYABLE_ERRORS = {
    "OUT_OF_STOCK", "PRODUCT_NOT_FOUND", "PRICE_CHANGED", "INSUFFICIENT_BALANCE", "READ_FAILED",
}


class SupplierCheckoutService:
    def __init__(self, session: AsyncSession, api: PartnerAPIClient, markup: float,
                 cryptobot: CryptoBotPayment | None = None):
        self.session = session
        self.api = api
        self.markup = markup
        self.orders = OrderRepo(session)
        self.promotions = PromotionService(session)
        self.cryptobot = cryptobot

    @staticmethod
    def margin_payload(checkout: SupplierCheckout, order: Order) -> str:
        return f"direct:{checkout.id}:{order.user_id}:{money(checkout.margin_amount_rub)}"

    @staticmethod
    def safe_payment_url(url: str | None) -> bool:
        try:
            parsed = urlsplit(url or "")
            return (parsed.scheme == "https" and parsed.hostname in ("t.me", "pay.crypt.bot")
                    and parsed.username is None and parsed.password is None and parsed.port in (None, 443))
        except ValueError:
            return False

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
        self, user_id: int, product_id: int, *, expected_price: float, request_key: str, qty: int = 1,
    ) -> tuple[SupplierCheckout, Order]:
        existing = await self._by_request(request_key, user_id)
        if existing:
            return existing
        if type(qty) is not int or not 1 <= qty <= 99:
            raise CheckoutError("Количество должно быть от 1 до 99.")
        product = await self.api.get_product(product_id)
        if not product.direct_payment_supported or product.supplier != "thegodshop" or product_id <= 0:
            raise CheckoutError("Этот поставщик не поддерживает оплату напрямую. Счёт не создан.")
        if not product.in_stock or product.stock < qty:
            raise CheckoutError("Товар закончился. Счёт не создан, купон сохранён.")
        quote = await self.promotions.quote(user_id, product, qty, self.markup)
        cost = money(product.price) * qty
        margin = money(quote.payable_price) - cost
        if cost <= 0 or margin < 0:
            raise CheckoutError("Некорректная цена заказа.")
        if margin > 0 and self.cryptobot is None:
            raise CheckoutError("Оплата сервисного сбора временно недоступна. Счёт не создан.")
        if money(expected_price) != money(quote.payable_price):
            raise CheckoutError("Цена изменилась. Откройте карточку и подтвердите актуальную сумму.")
        try:
            order = await self.orders.create(
                user_id=user_id, order_type=OrderType.CATALOG,
                partner_price=float(cost), user_price=quote.payable_price,
                product_id=product_id, qty=qty, request_key=request_key,
                status=OrderStatus.WAITING_PAYMENT,
                payload={"payment_method": "supplier_crypto", "product_name": product.name,
                         "promo_code": quote.promo_code, "discount": quote.discount,
                         "regular_price": quote.regular_price},
            )
            if quote.claim_id is not None and not await self.promotions.reserve_for_order(quote.claim_id, user_id, product_id, order.id):
                raise CheckoutError("Купон уже используется в другом заказе.")
            checkout = SupplierCheckout(order_id=order.id, amount_rub=float(cost), status="creating",
                                        margin_amount_rub=float(margin), margin_paid=margin == 0)
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
            if margin > 0:
                store_invoice = await self.cryptobot.create_invoice(
                    float(margin), description=f"Сервисный сбор Atlas Shop, заказ #{order.id}",
                    payload=self.margin_payload(checkout, order),
                )
                if (type(store_invoice.get("invoice_id")) is not int or store_invoice["invoice_id"] <= 0
                    or money(store_invoice.get("amount")) != margin or store_invoice.get("currency") != "RUB"
                    or store_invoice.get("status") != "active" or not self.safe_payment_url(store_invoice.get("pay_url"))):
                    raise CheckoutError("Некорректный счёт сервисного сбора. Оплата не запрашивалась.")
                if await self.session.scalar(select(Deposit.id).where(
                    Deposit.method == "cryptobot", Deposit.external_id == str(store_invoice["invoice_id"])
                )):
                    raise CheckoutError("Этот счёт уже используется для пополнения баланса.")
                checkout.margin_invoice_id = store_invoice["invoice_id"]
                checkout.margin_pay_url = store_invoice["pay_url"]
            invoice = await self.api.deposit_crypto(checkout.amount_rub)
            if (
                money(invoice.amount_rub) != money(checkout.amount_rub)
                or invoice.deposit_id <= 0
                or invoice.status not in (None, "pending", "active")
                or not self.safe_payment_url(invoice.pay_url)
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
            margin_status = "paid" if checkout.margin_paid else "active"
            if checkout.margin_amount_rub > 0 and not checkout.margin_paid:
                if self.cryptobot is None:
                    raise CheckoutError("Проверка сервисного сбора временно недоступна.")
                invoice = await self.cryptobot.get_invoice(checkout.margin_invoice_id)
                try:
                    correct_margin = money(invoice.get("amount")) == money(checkout.margin_amount_rub)
                except ValueError:
                    correct_margin = False
                if (invoice.get("invoice_id") != checkout.margin_invoice_id
                    or invoice.get("currency") != "RUB"
                    or not correct_margin
                    or invoice.get("payload") != self.margin_payload(checkout, order)
                    or invoice.get("status") not in ("active", "paid", "expired")):
                    await self._state(checkout, order, "attention", OrderStatus.ATTENTION, "MARGIN_PAYMENT_MISMATCH")
                    return checkout, order
                margin_status = invoice["status"]
                checkout.margin_paid = margin_status == "paid"
                await self.session.commit()
            payment = await self.api.get_deposit(checkout.deposit_id)
            try:
                correct_amount = money(payment.amount_rub) == money(checkout.amount_rub)
            except ValueError:
                correct_amount = False
            if payment.deposit_id != checkout.deposit_id or payment.method != "crypto" or not correct_amount:
                await self._state(checkout, order, "attention", OrderStatus.ATTENTION, "PAYMENT_MISMATCH")
                return checkout, order
            if payment.status == "paid":
                checkout.supplier_paid = True
                await self.session.commit()
            supplier_expired = payment.status in ("expired", "cancelled", "canceled")
            if supplier_expired or margin_status == "expired":
                if checkout.supplier_paid or (checkout.margin_amount_rub > 0 and checkout.margin_paid):
                    await self._state(checkout, order, "attention", OrderStatus.ATTENTION, "PARTIAL_PAYMENT")
                else:
                    await self.promotions.restore_for_order(order.id)
                    await self._state(checkout, order, "expired", OrderStatus.EXPIRED)
                return checkout, order
            if not checkout.supplier_paid or not checkout.margin_paid:
                return checkout, order
            if checkout.status == "expired":
                checkout.supplier_paid = True
                await self._state(checkout, order, "attention", OrderStatus.ATTENTION, "LATE_PAYMENT")
                return checkout, order
            await self._fulfill(checkout, order)
            return checkout, order

    async def _fulfill(self, checkout: SupplierCheckout, order: Order) -> None:
        try:
            product = await self.api.get_product(order.product_id)
            if product.id != order.product_id:
                raise PartnerAPIError("READ_FAILED", "Поставщик вернул другой товар")
            if not product.direct_payment_supported or product.supplier != "thegodshop":
                raise PartnerAPIError("READ_FAILED", "Поставщик товара не совпадает с оплатой")
            if not product.in_stock or product.stock < order.qty:
                raise PartnerAPIError("OUT_OF_STOCK", "Товар закончился", 409)
            if money(product.price) * order.qty != money(checkout.amount_rub):
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
                SupplierCheckout.margin_paid.is_(True),
            ).values(status="fulfilling", error_code=None).returning(SupplierCheckout.id)
        )
        if claimed is None:
            await self.session.rollback()
            return
        order.status = OrderStatus.PENDING
        await self.session.commit()
        try:
            result = await self.api.create_order(order.product_id, order.qty)
        except Exception as exc:
            uncertain = not isinstance(exc, PartnerAPIError) or exc.outcome_unknown
            await self._state(
                checkout, order, "attention", OrderStatus.UNCERTAIN if uncertain else OrderStatus.ATTENTION,
                getattr(exc, "code", "UNKNOWN_OUTCOME"),
            )
            return
        order.partner_order_id = result.order_id
        order.supplier_order_ref = str(result.order_id)
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
            if checkout.status != "attention" or not (
                checkout.supplier_paid or (checkout.margin_amount_rub > 0 and checkout.margin_paid)
            ):
                raise CheckoutError("Возврат можно отметить только у оплаченного заказа на проверке.")
            paid_amount = ((money(checkout.amount_rub) if checkout.supplier_paid else money(0))
                           + (money(checkout.margin_amount_rub) if checkout.margin_paid else money(0)))
            if money(amount) != paid_amount:
                raise CheckoutError("Сумма должна совпадать с оплаченной суммой заказа.")
            await self.promotions.restore_for_order(order.id)
            await self._state(checkout, order, "refunded", OrderStatus.FAILED, "REFUND_CONFIRMED_BY_ADMIN")
            return checkout, order
