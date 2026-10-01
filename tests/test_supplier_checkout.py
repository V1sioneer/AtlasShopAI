import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.engine import close_db, configure_sqlite, get_session_factory, init_db
from bot.db.models import Base, Order, OrderStatus, PromotionClaim, SupplierCheckout, Transaction, User
from bot.handlers.supplier_checkout import cb_check_checkout, checkout_view, cmd_admin_refunded
from bot.services.background import process_supplier_checkouts_once
from bot.services.orders import OrderService
from bot.services.partner_api import Balance, DepositResult, OrderResult, PartnerAPIError, PartnerDepositStatus, Product
from bot.services.promotions import PromotionService
from bot.services.supplier_checkout import CheckoutError, SupplierCheckoutService


class FakeSupplier:
    def __init__(self):
        self.price = 100
        self.stock = 10
        self.balance = 0
        self.invoices = {}
        self.invoice_calls = 0
        self.order_calls = 0
        self.status_calls = 0
        self.failure = None
        self.invoice_error = None
        self.wrong_invoice_amount = False
        self.payment_url = "https://t.me/CryptoBot?start=test"
        self.consume_before_failure = False

    async def get_product(self, product_id):
        return Product(id=product_id, name="Gemini Pro", price=self.price, stock=self.stock,
                       in_stock=self.stock > 0, category="Gemini")

    async def deposit_crypto(self, amount):
        self.invoice_calls += 1
        await asyncio.sleep(0)
        if self.invoice_error:
            raise self.invoice_error
        deposit_id = self.invoice_calls
        stored_amount = amount + 1 if self.wrong_invoice_amount else amount
        self.invoices[deposit_id] = {"deposit_id": deposit_id, "amount": stored_amount,
                                     "status": "pending", "method": "crypto"}
        return DepositResult(deposit_id=deposit_id, amount_rub=stored_amount, amount_usdt=1.2,
                             pay_url=self.payment_url, status="pending")

    def pay(self, deposit_id):
        if self.invoices[deposit_id]["status"] != "paid":
            self.balance = round(self.balance + self.invoices[deposit_id]["amount"], 2)
            self.invoices[deposit_id]["status"] = "paid"

    async def get_deposit(self, deposit_id):
        self.status_calls += 1
        return PartnerDepositStatus(**self.invoices[deposit_id])

    async def get_balance(self):
        return Balance(balance=self.balance, discount_percent=0)

    async def create_order(self, product_id, qty):
        self.order_calls += 1
        if self.failure:
            if self.consume_before_failure:
                self.balance -= self.price * qty
            raise self.failure
        if self.balance < self.price * qty:
            raise PartnerAPIError("INSUFFICIENT_BALANCE", "Нет средств", 409)
        self.balance = round(self.balance - self.price * qty, 2)
        return OrderResult(order_id=800 + self.order_calls, price=self.price * qty, delivered_data="KEY<>&")


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct.db'}", connect_args={"timeout": 30})
    event.listen(engine.sync_engine, "connect", configure_sqlite)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([User(id=i, balance_rub=500) for i in (1, 2, 3)])
        await session.commit()
        service = PromotionService(session)
        await service.create("GEMINI100", 38, 10)
        await service.activate("GEMINI100", 1)
        await service.activate("GEMINI100", 2)
    yield factory
    await engine.dispose()


async def create_checkout(sessions, api, user_id=1, key="direct-first"):
    async with sessions() as session:
        checkout, _ = await SupplierCheckoutService(session, api, 15).create(
            user_id, 38, expected_price=api.price, request_key=key
        )
        return checkout.id, checkout.deposit_id


@pytest.mark.asyncio
async def test_customer_funds_purchase_from_zero_supplier_balance(sessions):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    async with sessions() as session:
        service = SupplierCheckoutService(session, api, 15)
        checkout, order = await service.check(checkout_id, 1)
        assert checkout.status == "awaiting_payment"
        assert order.status == OrderStatus.WAITING_PAYMENT
        assert api.order_calls == 0 and api.balance == 0
        api.pay(deposit_id)
        checkout, order = await service.check(checkout_id, 1)
        assert checkout.status == "delivered" and order.status == OrderStatus.SUCCESS
        assert order.delivered_data == "KEY<>&"
        assert api.order_calls == 1 and api.balance == 0
        assert (await session.get(User, 1)).balance_rub == 500
        assert await session.scalar(select(func.count(Transaction.id))) == 0
        await service.check(checkout_id, 1, retry=True)
        assert api.order_calls == 1
        text, _ = checkout_view(checkout, order)
        assert "KEY&lt;&gt;&amp;" in text


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_clicks_create_only_one_invoice(sessions):
    api = FakeSupplier()
    results = await asyncio.gather(create_checkout(sessions, api), create_checkout(sessions, api))
    assert results[0][0] == results[1][0]
    assert api.invoice_calls == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count(Order.id))) == 1
        assert await session.scalar(select(func.count(SupplierCheckout.id))) == 1


@pytest.mark.asyncio
async def test_concurrent_checks_submit_paid_order_once(sessions):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    async def check():
        async with sessions() as session:
            return await SupplierCheckoutService(session, api, 15).check(checkout_id, 1)
    results = await asyncio.gather(check(), check(), check())
    assert all(checkout.status == "delivered" for checkout, _ in results)
    assert api.order_calls == 1 and api.balance == 0


@pytest.mark.asyncio
async def test_wallet_purchase_cannot_spend_buyer_deposit_before_poll(sessions):
    api = FakeSupplier()
    _, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    async with sessions() as session:
        with pytest.raises(PartnerAPIError):
            await OrderService(session, api, 15).buy_catalog_product(3, 99, request_key="normal", expected_price=115)
        assert api.order_calls == 0 and api.balance == 100
        assert (await session.get(User, 3)).balance_rub == 500


@pytest.mark.asyncio
async def test_two_buyers_fund_their_own_orders_without_store_money(sessions):
    api = FakeSupplier()
    first, first_deposit = await create_checkout(sessions, api)
    second, second_deposit = await create_checkout(sessions, api, 2, "second")
    api.pay(first_deposit)
    api.pay(second_deposit)
    async with sessions() as session:
        service = SupplierCheckoutService(session, api, 15)
        assert (await service.check(first, 1))[0].status == "delivered"
        assert api.balance == 100
        assert (await service.check(second, 2))[0].status == "delivered"
        assert api.balance == 0


@pytest.mark.asyncio
async def test_protected_funds_with_kopecks_do_not_accumulate_float_error(sessions):
    from bot.services.supplier_funding import protected_supplier_amount
    api = FakeSupplier()
    api.price = 100.01
    async with sessions() as session:
        await PromotionService(session).activate("GEMINI100", 3)
    for user_id in (1, 2, 3):
        await create_checkout(sessions, api, user_id, f"kopecks-{user_id}")
    async with sessions() as session:
        assert await protected_supplier_amount(session) == 300.03


@pytest.mark.asyncio
async def test_no_stock_creates_no_invoice_and_preserves_coupon(sessions):
    api = FakeSupplier()
    api.stock = 0
    with pytest.raises(CheckoutError):
        await create_checkout(sessions, api)
    assert api.invoice_calls == 0
    async with sessions() as session:
        claim = await session.scalar(select(PromotionClaim).where(PromotionClaim.user_id == 1))
        assert claim.order_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["amount", "unsafe_url", "transport"])
async def test_failed_invoice_restores_coupon_without_crediting_wallet(sessions, issue):
    api = FakeSupplier()
    api.wrong_invoice_amount = issue == "amount"
    if issue == "unsafe_url":
        api.payment_url = "http://attacker.invalid/pay"
    if issue == "transport":
        api.invoice_error = PartnerAPIError("TRANSPORT_ERROR", "Нет ответа", outcome_unknown=True)
    with pytest.raises((CheckoutError, PartnerAPIError)):
        await create_checkout(sessions, api)
    async with sessions() as session:
        claim = await session.scalar(select(PromotionClaim).where(PromotionClaim.user_id == 1))
        assert claim.order_id is None
        assert (await session.get(User, 1)).balance_rub == 500
        checkout = await session.scalar(select(SupplierCheckout))
        assert checkout.status == "failed"
        # Repeated click retrieves the failed intent and cannot mutate again.
        await SupplierCheckoutService(session, api, 15).create(1, 38, expected_price=100, request_key="direct-first")
        assert api.invoice_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("amount", 99), ("deposit_id", 900), ("method", "ton")])
async def test_payment_mismatch_never_buys_or_credits_user(sessions, field, value):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    api.invoices[deposit_id][field] = value
    async with sessions() as session:
        checkout, order = await SupplierCheckoutService(session, api, 15).check(checkout_id, 1)
        assert checkout.status == "attention" and checkout.error_code == "PAYMENT_MISMATCH"
        assert api.order_calls == 0
        assert (await session.get(User, 1)).balance_rub == 500


@pytest.mark.asyncio
async def test_foreign_user_cannot_check_or_reveal_invoice(sessions):
    api = FakeSupplier()
    checkout_id, _ = await create_checkout(sessions, api)
    async with sessions() as session:
        with pytest.raises(CheckoutError):
            await SupplierCheckoutService(session, api, 15).check(checkout_id, 2)
        callback = SimpleNamespace(data=f"direct_check:{checkout_id}", answer=AsyncMock())
        await cb_check_checkout(callback, session, await session.get(User, 2), api, 15)
        assert callback.answer.call_args.kwargs["show_alert"]
        assert api.status_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["stock", "price", "provider"])
async def test_paid_unfulfilled_order_stays_protected_and_can_retry(sessions, issue):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    if issue == "stock": api.stock = 0
    if issue == "price": api.price = 110
    if issue == "provider": api.failure = PartnerAPIError("OUT_OF_STOCK", "Нет товара", 409)
    async with sessions() as session:
        service = SupplierCheckoutService(session, api, 15)
        checkout, order = await service.check(checkout_id, 1)
        assert checkout.status == "attention" and order.status == OrderStatus.ATTENTION
        assert checkout.supplier_paid and api.balance == 100
        assert (await session.get(User, 1)).balance_rub == 500
        with pytest.raises(PartnerAPIError):
            await OrderService(session, api, 15).buy_catalog_product(3, 99, request_key="normal")
        api.stock, api.price, api.failure = 10, 100, None
        checkout, order = await service.check(checkout_id, 1, retry=True)
        assert checkout.status == "delivered" and api.balance == 0


@pytest.mark.asyncio
async def test_unknown_outcome_never_resubmits_even_if_error_name_looks_retryable(sessions):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    api.failure = PartnerAPIError("OUT_OF_STOCK", "Ответ потерян", outcome_unknown=True)
    api.consume_before_failure = True
    async with sessions() as session:
        service = SupplierCheckoutService(session, api, 15)
        checkout, order = await service.check(checkout_id, 1)
        assert order.status == OrderStatus.UNCERTAIN and checkout.status == "attention"
        assert api.balance == 0
        api.failure = None
        await service.check(checkout_id, 1, retry=True)
        assert api.order_calls == 1


@pytest.mark.asyncio
async def test_expired_invoice_restores_coupon_and_late_payment_is_not_autospent(sessions):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.invoices[deposit_id]["status"] = "expired"
    async with sessions() as session:
        service = SupplierCheckoutService(session, api, 15)
        checkout, order = await service.check(checkout_id, 1)
        assert checkout.status == "expired"
        assert (await session.scalar(select(PromotionClaim).where(PromotionClaim.user_id == 1))).order_id is None
        api.pay(deposit_id)
        checkout, order = await service.check(checkout_id, 1)
        assert checkout.error_code == "LATE_PAYMENT" and checkout.status == "attention"
        assert api.order_calls == 0


@pytest.mark.asyncio
async def test_recorded_manual_refund_never_mints_wallet_money(sessions):
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    api.stock = 0
    async with sessions() as session:
        service = SupplierCheckoutService(session, api, 15)
        await service.check(checkout_id, 1)
        with pytest.raises(CheckoutError):
            await service.record_refund(checkout_id, 99)
        api.balance -= 100  # An external refund already made by the operator.
        checkout, order = await service.record_refund(checkout_id, 100)
        assert checkout.status == "refunded" and order.status == OrderStatus.FAILED
        assert (await session.get(User, 1)).balance_rub == 500
        assert (await session.scalar(select(PromotionClaim).where(PromotionClaim.user_id == 1))).order_id is None
        await service.record_refund(checkout_id, 100)
        assert api.order_calls == 0


@pytest.mark.asyncio
async def test_background_delivers_and_notifies_once(sessions):
    api = FakeSupplier()
    _, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    bot = SimpleNamespace(send_message=AsyncMock())
    await process_supplier_checkouts_once(bot, sessions, api, [3], 15)
    await process_supplier_checkouts_once(bot, sessions, api, [3], 15)
    assert api.order_calls == 1 and bot.send_message.call_count == 2
    assert bot.send_message.call_args_list[0].args[0] == 1


@pytest.mark.asyncio
async def test_restart_preserves_waiting_and_freezes_interrupted_fulfillment(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'startup-direct.db'}"
    await init_db(url)
    api = FakeSupplier()
    factory = get_session_factory()
    async with factory() as session:
        session.add(User(id=1, balance_rub=77))
        await session.commit()
        promo = PromotionService(session)
        await promo.create("GEMINI100", 38, 10)
        await promo.activate("GEMINI100", 1)
        checkout, order = await SupplierCheckoutService(session, api, 15).create(1, 38, expected_price=100, request_key="restart")
        checkout.status, checkout.supplier_paid = "fulfilling", True
        order.status = OrderStatus.PENDING
        await session.commit()
        checkout_id = checkout.id
    await close_db()
    await init_db(url)
    async with get_session_factory()() as session:
        checkout, order = await SupplierCheckoutService(session, api, 15).check(checkout_id, 1, retry=True)
        assert checkout.status == "attention" and checkout.error_code == "PROCESS_INTERRUPTED"
        assert order.status == OrderStatus.UNCERTAIN
        assert (await session.get(User, 1)).balance_rub == 77
        assert api.order_calls == 0
    await close_db()


@pytest.mark.asyncio
async def test_waiting_invoice_survives_restart_and_is_fulfilled_after_payment(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'waiting-direct.db'}"
    await init_db(url)
    api = FakeSupplier()
    async with get_session_factory()() as session:
        session.add(User(id=1, balance_rub=77))
        await session.commit()
        promo = PromotionService(session)
        await promo.create("GEMINI100", 38, 10)
        await promo.activate("GEMINI100", 1)
        checkout, _ = await SupplierCheckoutService(session, api, 15).create(1, 38, expected_price=100, request_key="waiting")
        checkout_id, deposit_id = checkout.id, checkout.deposit_id
    await close_db()
    await init_db(url)
    async with get_session_factory()() as session:
        checkout, order = await SupplierCheckoutService(session, api, 15).get(checkout_id)
        assert checkout.status == "awaiting_payment" and order.status == OrderStatus.WAITING_PAYMENT
        api.pay(deposit_id)
        checkout, order = await SupplierCheckoutService(session, api, 15).check(checkout_id, 1)
        assert checkout.status == "delivered" and api.order_calls == 1
        assert (await session.get(User, 1)).balance_rub == 77
    await close_db()


@pytest.mark.asyncio
async def test_refund_record_requires_admin_and_explicit_confirmation(sessions):
    from aiogram.filters import CommandObject
    api = FakeSupplier()
    checkout_id, deposit_id = await create_checkout(sessions, api)
    api.pay(deposit_id)
    api.stock = 0
    async with sessions() as session:
        await SupplierCheckoutService(session, api, 15).check(checkout_id, 1)
        message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
        command = CommandObject(command="direct_refunded", args=f"{checkout_id} 100 ВОЗВРАТ_ВЫПОЛНЕН")
        await cmd_admin_refunded(message, command, session, api, 15, [3])
        message.answer.assert_not_called()
        message.from_user.id = 3
        command = CommandObject(command="direct_refunded", args=f"{checkout_id} 100")
        await cmd_admin_refunded(message, command, session, api, 15, [3])
        assert "денег она не переводит" in message.answer.call_args.args[0]
        checkout, _ = await SupplierCheckoutService(session, api, 15).get(checkout_id)
        assert checkout.status == "attention"


@pytest.mark.asyncio
async def test_restart_releases_coupon_when_invoice_link_was_never_persisted(tmp_path):
    from bot.db.models import OrderType
    url = f"sqlite+aiosqlite:///{tmp_path / 'creating-direct.db'}"
    await init_db(url)
    async with get_session_factory()() as session:
        session.add(User(id=1, balance_rub=77))
        await session.commit()
        promo = PromotionService(session)
        await promo.create("GEMINI100", 38, 10)
        await promo.activate("GEMINI100", 1)
        order = Order(user_id=1, type=OrderType.CATALOG, product_id=38,
                      partner_price=100, user_price=100, status=OrderStatus.WAITING_PAYMENT,
                      request_key="creating")
        session.add(order)
        await session.flush()
        claim = await session.scalar(select(PromotionClaim))
        claim.order_id = order.id
        session.add(SupplierCheckout(order_id=order.id, amount_rub=100, status="creating"))
        await session.commit()
    await close_db()
    await init_db(url)
    async with get_session_factory()() as session:
        assert (await session.scalar(select(SupplierCheckout))).status == "failed"
        assert (await session.scalar(select(Order))).status == OrderStatus.FAILED
        assert (await session.scalar(select(PromotionClaim))).order_id is None
        assert (await session.get(User, 1)).balance_rub == 77
    await close_db()


class FakeStorePayment:
    def __init__(self):
        self.invoices = {}
        self.calls = 0
        self.status_calls = 0
        self.failure = None

    async def create_invoice(self, amount, description="", payload=""):
        self.calls += 1
        if self.failure:
            raise self.failure
        identifier = 1000 + self.calls
        self.invoices[identifier] = dict(invoice_id=identifier, status="active", amount=amount,
                                        currency="RUB", payload=payload)
        return {**self.invoices[identifier], "pay_url": "https://t.me/CryptoBot?start=store-test"}

    async def get_invoice(self, identifier):
        self.status_calls += 1
        return dict(self.invoices[identifier])


async def full_checkout(sessions, supplier, store, *, user=3, qty=1, key="full"):
    async with sessions() as session:
        total = 115 * qty if user == 3 else 100 + 115 * (qty - 1)
        checkout, order = await SupplierCheckoutService(session, supplier, 15, store).create(
            user, 38, expected_price=total, request_key=key, qty=qty)
        return checkout.id, checkout.deposit_id, checkout.margin_invoice_id


@pytest.mark.asyncio
async def test_normal_sale_needs_both_payments_and_funds_own_purchase(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin_invoice = await full_checkout(sessions, supplier, store)
    async with sessions() as session:
        service = SupplierCheckoutService(session, supplier, 15, store)
        checkout, order = await service.check(identifier, 3)
        assert order.user_price == 115 and checkout.amount_rub == 100 and checkout.margin_amount_rub == 15
        text, keyboard = checkout_view(checkout, order)
        assert "115 ₽" in text and "100 ₽" in text and "15 ₽" in text
        assert any(button.url == checkout.margin_pay_url for row in keyboard.inline_keyboard for button in row)
        assert all(button.url != checkout.pay_url for row in keyboard.inline_keyboard for button in row)
        store.invoices[margin_invoice]["status"] = "paid"
        checkout, order = await service.check(identifier, 3)
        assert checkout.margin_paid and not checkout.supplier_paid and supplier.order_calls == 0
        _, keyboard = checkout_view(checkout, order)
        assert any(button.url == checkout.pay_url for row in keyboard.inline_keyboard for button in row)
        supplier.pay(deposit)
        checkout, order = await service.check(identifier, 3)
        assert checkout.status == "delivered" and order.status == OrderStatus.SUCCESS
        assert supplier.balance == 0 and supplier.order_calls == 1
        assert (await session.get(User, 3)).balance_rub == 500
        assert await session.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.asyncio
async def test_supplier_only_payment_cannot_bypass_store_fee(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin = await full_checkout(sessions, supplier, store)
    supplier.pay(deposit)
    async with sessions() as session:
        checkout, order = await SupplierCheckoutService(session, supplier, 15, store).check(identifier, 3)
        assert checkout.supplier_paid and not checkout.margin_paid
        assert order.status == OrderStatus.WAITING_PAYMENT and supplier.order_calls == 0
        from bot.services.supplier_funding import protected_supplier_amount
        assert await protected_supplier_amount(session) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("qty,user,total,cost,margin", [(2, 3, 230, 200, 30), (2, 1, 215, 200, 15)])
async def test_multiple_units_fund_full_cost_and_apply_one_coupon(sessions, qty, user, total, cost, margin):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, store_invoice = await full_checkout(sessions, supplier, store, user=user, qty=qty)
    store.invoices[store_invoice]["status"] = "paid"
    supplier.pay(deposit)
    async with sessions() as session:
        checkout, order = await SupplierCheckoutService(session, supplier, 15, store).check(identifier, user)
        assert checkout.status == "delivered" and order.qty == 2
        assert (order.user_price, checkout.amount_rub, checkout.margin_amount_rub) == (total, cost, margin)
        assert supplier.balance == 0 and supplier.order_calls == 1
        assert (await session.get(User, user)).balance_rub == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("amount", 14), ("currency", "USD"), ("payload", "other"), ("invoice_id", 900)])
async def test_store_invoice_mismatch_never_purchases(sessions, field, value):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin = await full_checkout(sessions, supplier, store)
    supplier.pay(deposit)
    store.invoices[margin]["status"] = "paid"
    store.invoices[margin][field] = value
    async with sessions() as session:
        checkout, order = await SupplierCheckoutService(session, supplier, 15, store).check(identifier, 3)
        assert checkout.status == "attention" and checkout.error_code == "MARGIN_PAYMENT_MISMATCH"
        assert supplier.order_calls == 0
        assert (await session.get(User, 3)).balance_rub == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", ["store", "supplier", "neither"])
async def test_expiry_with_partial_payment_is_saved_for_support(sessions, paid):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin = await full_checkout(sessions, supplier, store)
    if paid == "store":
        store.invoices[margin]["status"] = "paid"
        supplier.invoices[deposit]["status"] = "expired"
    else:
        store.invoices[margin]["status"] = "expired"
        if paid == "supplier": supplier.pay(deposit)
    async with sessions() as session:
        checkout, order = await SupplierCheckoutService(session, supplier, 15, store).check(identifier, 3)
        assert checkout.status == ("expired" if paid == "neither" else "attention")
        if paid != "neither": assert checkout.error_code == "PARTIAL_PAYMENT"
        assert supplier.order_calls == 0


@pytest.mark.asyncio
async def test_partial_refund_records_only_already_paid_amount(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin = await full_checkout(sessions, supplier, store)
    store.invoices[margin]["status"] = "paid"
    supplier.invoices[deposit]["status"] = "expired"
    async with sessions() as session:
        service = SupplierCheckoutService(session, supplier, 15, store)
        await service.check(identifier, 3)
        with pytest.raises(CheckoutError): await service.record_refund(identifier, 115)
        checkout, order = await service.record_refund(identifier, 15)
        assert checkout.status == "refunded" and (await session.get(User, 3)).balance_rub == 500


@pytest.mark.asyncio
async def test_duplicate_full_checkout_and_concurrent_fulfillment_use_one_invoice_pair(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    results = await asyncio.gather(full_checkout(sessions, supplier, store), full_checkout(sessions, supplier, store))
    assert results[0][0] == results[1][0] and store.calls == 1 and supplier.invoice_calls == 1
    identifier = results[0][0]
    async with sessions() as session:
        checkout = await session.get(SupplierCheckout, identifier)
        deposit, margin = checkout.deposit_id, checkout.margin_invoice_id
    store.invoices[margin]["status"] = "paid"
    supplier.pay(deposit)
    async def check():
        async with sessions() as session:
            return await SupplierCheckoutService(session, supplier, 15, store).check(identifier, 3)
    await asyncio.gather(check(), check())
    assert supplier.order_calls == 1


@pytest.mark.asyncio
async def test_background_sends_supplier_link_after_fee_once_and_delivery_once(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin = await full_checkout(sessions, supplier, store)
    store.invoices[margin]["status"] = "paid"
    bot = SimpleNamespace(send_message=AsyncMock())
    await process_supplier_checkouts_once(bot, sessions, supplier, [], 15, store)
    await process_supplier_checkouts_once(bot, sessions, supplier, [], 15, store)
    assert bot.send_message.call_count == 1
    assert bot.send_message.call_args.args[0] == 3
    supplier.pay(deposit)
    await process_supplier_checkouts_once(bot, sessions, supplier, [], 15, store)
    await process_supplier_checkouts_once(bot, sessions, supplier, [], 15, store)
    assert supplier.order_calls == 1 and bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_foreign_user_cannot_access_either_full_checkout_payment(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    identifier, deposit, margin = await full_checkout(sessions, supplier, store)
    async with sessions() as session:
        with pytest.raises(CheckoutError):
            await SupplierCheckoutService(session, supplier, 15, store).check(identifier, 2)
    assert supplier.status_calls == 0 and store.status_calls == 0


@pytest.mark.asyncio
async def test_store_gateway_unavailable_does_not_create_supplier_invoice(sessions):
    supplier = FakeSupplier()
    async with sessions() as session:
        with pytest.raises(CheckoutError):
            await SupplierCheckoutService(session, supplier, 15).create(3, 38, expected_price=115, request_key="no-gateway")
        assert await session.scalar(select(Order)) is None
        assert supplier.invoice_calls == 0
        assert (await session.get(User, 3)).balance_rub == 500


@pytest.mark.asyncio
async def test_store_invoice_transport_failure_is_not_retried_and_exposes_no_payment(sessions):
    supplier, store = FakeSupplier(), FakeStorePayment()
    store.failure = RuntimeError("Invoice creation response lost")
    with pytest.raises(RuntimeError):
        await full_checkout(sessions, supplier, store)
    async with sessions() as session:
        checkout = await session.scalar(select(SupplierCheckout))
        assert checkout.status == "failed" and checkout.pay_url is None and checkout.margin_pay_url is None
        assert (await session.get(User, 3)).balance_rub == 500
        await SupplierCheckoutService(session, supplier, 15, store).create(3, 38, expected_price=115, request_key="full")
        assert store.calls == 1 and supplier.invoice_calls == 0


@pytest.mark.asyncio
async def test_legacy_supplier_checkout_schema_migration_keeps_promo_payment_defaults():
    from sqlalchemy import text
    from bot.db.engine import upgrade_schema
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, request_key VARCHAR, supplier VARCHAR, supplier_order_ref VARCHAR)"))
        await connection.execute(text("CREATE TABLE deposits (method VARCHAR, external_id VARCHAR)"))
        await connection.execute(text("CREATE TABLE supplier_checkouts (id INTEGER PRIMARY KEY, amount_rub FLOAT, supplier_paid BOOLEAN)"))
        await connection.execute(text("INSERT INTO supplier_checkouts VALUES (1, 100, 1)"))
        await connection.run_sync(upgrade_schema)
        await connection.run_sync(upgrade_schema)
        row = (await connection.execute(text("SELECT amount_rub, supplier_paid, margin_amount_rub, margin_paid, margin_invoice_id FROM supplier_checkouts"))).one()
        assert row == (100, 1, 0, 1, None)
    await engine.dispose()
