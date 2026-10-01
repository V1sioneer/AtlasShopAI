import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.filters import CommandObject
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.engine import configure_sqlite
from bot.db.models import Base, CatalogPromotion, Order, OrderStatus, PromotionClaim, User
from bot.handlers.catalog import cb_buy_catalog, cb_confirm_catalog
from bot.handlers.promo import cmd_promo_create
from bot.handlers.start import cmd_start
from bot.services.orders import DuplicateOrder, InsufficientUserBalance, OrderOutcomeUnknown, OrderService, PriceChanged
from bot.services.partner_api import OrderResult, PartnerAPIError, Product
from bot.services.promotions import PromotionError, PromotionService


class FakeAPI:
    def __init__(self, failure=None, *, price=100, stock=100):
        self.failure = failure
        self.price = price
        self.stock = stock
        self.calls = 0

    async def get_product(self, product_id):
        return Product(id=product_id, name="Gemini Pro на 18 месяцев", category="Gemini",
                       price=self.price, stock=self.stock, in_stock=self.stock > 0)

    async def create_order(self, product_id, qty):
        self.calls += 1
        if self.failure:
            raise self.failure
        return OrderResult(order_id=900 + self.calls, delivered_data="TEST", price=self.price * qty)


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'promotions.db'}", connect_args={"timeout": 30})
    event.listen(engine.sync_engine, "connect", configure_sqlite)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([User(id=i, balance_rub=500) for i in range(1, 25)])
        await session.commit()
        await PromotionService(session).create("GEMINI100", 38, 10)
    yield factory
    await engine.dispose()


async def activate(sessions, user_id=1):
    async with sessions() as session:
        return await PromotionService(session).activate("GEMINI100", user_id)


@pytest.mark.asyncio
async def test_concurrent_activations_never_exceed_ten(sessions):
    outcomes = await asyncio.gather(*(activate(sessions, i) for i in range(1, 21)), return_exceptions=True)
    assert sum(isinstance(result, CatalogPromotion) for result in outcomes) == 10
    assert sum(isinstance(result, PromotionError) for result in outcomes) == 10
    async with sessions() as session:
        campaign = await PromotionService(session).get_campaign("gemini100")
        assert campaign.claimed_count == 10
        assert await session.scalar(select(func.count(PromotionClaim.id))) == 10


@pytest.mark.asyncio
async def test_repeated_concurrent_activation_uses_one_slot_even_when_last(sessions):
    async with sessions() as session:
        campaign = await PromotionService(session).get_campaign("GEMINI100")
        campaign.max_claims = 1
        await session.commit()
    outcomes = await asyncio.gather(*(activate(sessions) for _ in range(5)), return_exceptions=True)
    assert all(isinstance(result, CatalogPromotion) for result in outcomes)
    async with sessions() as session:
        assert (await PromotionService(session).get_campaign("GEMINI100")).claimed_count == 1
        assert await session.scalar(select(func.count(PromotionClaim.id))) == 1


@pytest.mark.asyncio
async def test_discount_is_exact_cost_for_one_unit_and_only_selected_product(sessions):
    await activate(sessions)
    async with sessions() as session:
        service = PromotionService(session)
        product = await FakeAPI().get_product(38)
        quote = await service.quote(1, product, 1, 15)
        assert (quote.regular_price, quote.payable_price, quote.discount) == (115, 100, 15)
        assert (await service.quote(1, product, 2, 15)).payable_price == 215
        assert (await service.quote(2, product, 1, 15)).payable_price == 115
        assert (await service.quote(1, product.model_copy(update={"id": 99}), 1, 15)).payable_price == 115
        product.price = 100.01
        assert (await service.quote(1, product, 1, 15)).payable_price == 100.01


@pytest.mark.asyncio
async def test_success_consumes_coupon_once_and_records_discount(sessions):
    await activate(sessions)
    api = FakeAPI()
    async with sessions() as session:
        svc = OrderService(session, api, 15)
        _, charged = await svc.buy_catalog_product(1, 38, request_key="first", expected_price=100)
        assert charged == 100
        assert (await session.get(User, 1)).balance_rub == 400
        with pytest.raises(DuplicateOrder):
            await svc.buy_catalog_product(1, 38, request_key="first", expected_price=100)
        with pytest.raises(PriceChanged):
            await svc.buy_catalog_product(1, 38, request_key="stale", expected_price=100)
        assert api.calls == 1
        _, charged = await svc.buy_catalog_product(1, 38, request_key="second", expected_price=115)
        assert charged == 115
        order = await session.scalar(select(Order).where(Order.request_key == "first"))
        assert order.partner_price == 100
        assert json.loads(order.payload_json)["discount"] == 15
        assert json.loads(order.payload_json)["promo_code"] == "GEMINI100"
        _, completed, reserved = await PromotionService(session).stats("GEMINI100")
        assert (completed, reserved) == (1, 0)
        with pytest.raises(PromotionError):
            await PromotionService(session).activate("GEMINI100", 1)


@pytest.mark.asyncio
async def test_insufficient_balance_preserves_coupon_and_creates_no_order(sessions):
    await activate(sessions)
    api = FakeAPI()
    async with sessions() as session:
        user = await session.get(User, 1)
        user.balance_rub = 99
        await session.commit()
        with pytest.raises(InsufficientUserBalance):
            await OrderService(session, api, 15).buy_catalog_product(1, 38, request_key="poor", expected_price=100)
        assert await session.scalar(select(func.count(Order.id))) == 0
        assert (await session.scalar(select(PromotionClaim))).order_id is None
        assert api.calls == 0


@pytest.mark.asyncio
async def test_out_of_stock_does_not_spend_coupon(sessions):
    await activate(sessions)
    async with sessions() as session:
        api = FakeAPI(stock=0)
        with pytest.raises(PartnerAPIError):
            await OrderService(session, api, 15).buy_catalog_product(1, 38, request_key="no-stock", expected_price=100)
        assert (await session.scalar(select(PromotionClaim))).order_id is None
        assert await session.scalar(select(func.count(Order.id))) == 0
        assert api.calls == 0


@pytest.mark.asyncio
async def test_definitive_failure_restores_money_and_coupon_for_retry(sessions):
    await activate(sessions)
    async with sessions() as session:
        api = FakeAPI(PartnerAPIError("OUT_OF_STOCK", "Нет товара", 409))
        svc = OrderService(session, api, 15)
        with pytest.raises(PartnerAPIError):
            await svc.buy_catalog_product(1, 38, request_key="failed", expected_price=100)
        assert (await session.get(User, 1)).balance_rub == 500
        assert (await session.scalar(select(PromotionClaim))).order_id is None
        assert (await session.scalar(select(Order))).status == OrderStatus.FAILED
        api.failure = None
        _, charged = await svc.buy_catalog_product(1, 38, request_key="retry", expected_price=100)
        assert charged == 100
        assert (await session.get(User, 1)).balance_rub == 400


@pytest.mark.asyncio
async def test_unknown_provider_outcome_reserves_coupon_and_money(sessions):
    await activate(sessions)
    async with sessions() as session:
        api = FakeAPI(PartnerAPIError("TRANSPORT_ERROR", "Нет ответа", outcome_unknown=True))
        with pytest.raises(OrderOutcomeUnknown):
            await OrderService(session, api, 15).buy_catalog_product(1, 38, request_key="unknown", expected_price=100)
        assert (await session.get(User, 1)).balance_rub == 400
        assert (await session.scalar(select(PromotionClaim))).order_id is not None
        assert (await session.scalar(select(Order))).status == OrderStatus.UNCERTAIN
        assert (await PromotionService(session).quote(1, await api.get_product(38), 1, 15)).payable_price == 115


@pytest.mark.asyncio
async def test_concurrent_orders_cannot_both_use_one_coupon(sessions):
    await activate(sessions)
    api = FakeAPI()

    async def buy(key):
        async with sessions() as session:
            return await OrderService(session, api, 15).buy_catalog_product(1, 38, request_key=key, expected_price=100)

    outcomes = await asyncio.gather(buy("a"), buy("b"), return_exceptions=True)
    assert sum(isinstance(result, tuple) for result in outcomes) == 1
    assert sum(isinstance(result, PriceChanged) for result in outcomes) == 1
    assert api.calls == 1
    async with sessions() as session:
        assert (await session.get(User, 1)).balance_rub == 400
        assert await session.scalar(select(func.count(Order.id))) == 1


@pytest.mark.asyncio
async def test_supplier_price_change_requires_new_confirmation(sessions):
    await activate(sessions)
    async with sessions() as session:
        api = FakeAPI(price=110)
        with pytest.raises(PriceChanged):
            await OrderService(session, api, 15).buy_catalog_product(1, 38, request_key="changed", expected_price=100)
        assert api.calls == 0
        assert (await session.get(User, 1)).balance_rub == 500
        assert (await session.scalar(select(PromotionClaim))).order_id is None


@pytest.mark.asyncio
async def test_disable_stops_new_activations_but_honors_existing_coupons(sessions):
    await activate(sessions)
    async with sessions() as session:
        service = PromotionService(session)
        campaign = await service.get_campaign("GEMINI100")
        campaign.is_active = False
        await session.commit()
        with pytest.raises(PromotionError):
            await service.activate("GEMINI100", 2)
        await service.activate("GEMINI100", 1)
        assert (await service.quote(1, await FakeAPI().get_product(38), 1, 15)).payable_price == 100


@pytest.mark.asyncio
async def test_purchase_screen_and_callback_carry_discounted_total(sessions):
    await activate(sessions)
    async with sessions() as session:
        callback = SimpleNamespace(data="buy_catalog:38:1", message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
        await cb_buy_catalog(callback, FakeAPI(), 15, session, await session.get(User, 1))
        args = callback.message.edit_text.call_args
        assert "100 ₽" in args.args[0] and "115 ₽" in args.args[0]
        keyboard = args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        assert "confirm_catalog:38:1:100.00" in callbacks
        assert "supplier_checkout:38:1:100.00" in callbacks


@pytest.mark.asyncio
async def test_old_confirmation_never_buys_without_showing_current_price(sessions):
    async with sessions() as session:
        api = FakeAPI()
        callback = SimpleNamespace(data="confirm_catalog:38:1", answer=AsyncMock())
        await cb_confirm_catalog(callback, session, await session.get(User, 1), api, 15, [])
        assert callback.answer.call_args.kwargs["show_alert"] is True
        assert api.calls == 0


@pytest.mark.asyncio
async def test_non_admin_cannot_create_campaign(sessions):
    async with sessions() as session:
        message = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock())
        await cmd_promo_create(message, CommandObject(command="promo_create", args="FREE 38 1000"), session, FakeAPI(), [1])
        assert await session.scalar(select(func.count(CatalogPromotion.id))) == 1
        message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_campaign_cannot_reset_claims(sessions):
    await activate(sessions)
    async with sessions() as session:
        with pytest.raises(PromotionError):
            await PromotionService(session).create("gemini100", 38, 100)
        campaign = await PromotionService(session).get_campaign("GEMINI100")
        assert (campaign.claimed_count, campaign.max_claims) == (1, 10)


@pytest.mark.asyncio
async def test_promo_link_activates_coupon_and_discloses_missing_stock(sessions):
    async with sessions() as session:
        message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock())
        api = FakeAPI(stock=0)
        await cmd_start(
            message, state, await session.get(User, 1), session, [1],
            CommandObject(command="start", args="promo_GEMINI100"), api, 15,
        )
        assert message.answer.call_count == 2
        response = message.answer.call_args.args[0]
        assert "активирован" in response and "100 ₽" in response
        assert "нет в наличии" in response
        assert "не пополнение баланса" in response
        claim = await session.scalar(select(PromotionClaim))
        assert claim.user_id == 1 and claim.order_id is None
        assert (await session.get(User, 1)).balance_rub == 500
        assert api.calls == 0
