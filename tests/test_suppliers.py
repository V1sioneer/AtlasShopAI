import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.engine import configure_sqlite, upgrade_schema
from bot.db.models import Base, Order, OrderStatus, User
from bot.services.aethel_api import AethelAPIClient
from bot.services.orders import OrderOutcomeUnknown, OrderService
from bot.services.partner_api import PartnerAPIError, Product
from bot.services.suppliers import SupplierRouter
from bot.handlers.catalog import direct_available
from bot.services.supplier_checkout import CheckoutError, SupplierCheckoutService


def catalog(stock=5, price="0.50"):
    def item(item_id, name, value, quantity):
        return dict(id=item_id, name=name, description="Гарантия 1 час <условия>",
                    price_usd=value, currency="USD", available_quantity=quantity)
    return {"ok": True, "categories": [{"products": [
        item(2, "Gemini AI PRO 18 мес. link", price, stock),
        item(13, "Chat GPT Plus 1M (FW)", "4.60", 2),
        item(33, "Chat GPT K12 2 года", "6.20", 13),
        item(16, "Claude Gift Card Pro", "14.50", 3),
    ]}]}


def client(handler, rate=87):
    api = AethelAPIClient("https://supplier.invalid/api", "fake-key", rate)
    api._client = httpx.AsyncClient(base_url="https://supplier.invalid/api/",
                                    transport=httpx.MockTransport(handler))
    return api


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'suppliers.db'}")
    event.listen(engine.sync_engine, "connect", configure_sqlite)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(User(id=1, balance_rub=500))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_currency_rounding_stock_terms_and_all_products():
    api = client(lambda request: httpx.Response(200, json=catalog(stock=0, price="0.501")))
    try:
        products = await api.get_products()
        assert [p.id for p in products] == [-2, -13, -33, -16]
        assert products[0].price == 43.59
        assert products[0].price_usd == "0.501"
        assert products[0].usd_rub_rate == "87"
        assert not products[0].in_stock and products[0].stock == 0
        assert products[0].description == "Гарантия 1 час <условия>"
        assert not products[0].direct_payment_supported
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_router_keeps_claude_namespaces_and_old_product_links():
    primary = SimpleNamespace(
        get_products=AsyncMock(return_value=[
            Product(id=2, name="Claude Pro", price=2200, stock=2, in_stock=True),
            Product(id=38, name="Gemini", price=100, stock=2, in_stock=True),
            Product(id=8, name="ChatGPT Plus", price=999, stock=2, in_stock=True),
            Product(id=9, name="Spotify", price=200, stock=2, in_stock=True),
        ]), get_product=AsyncMock(return_value="old promotion"),
    )
    api = client(lambda request: httpx.Response(200, json=catalog()))
    router = SupplierRouter(primary, api)
    try:
        products = await router.get_products()
        assert [(p.id, p.supplier) for p in products] == [
            (2, "thegodshop"), (38, "thegodshop"), (8, "thegodshop"), (9, "thegodshop"),
            (-2, "aethel"), (-13, "aethel"), (-33, "aethel"), (-16, "aethel")]
        assert await router.get_product(38) == "old promotion"
        assert (await router.get_product(-2)).supplier == "aethel"
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_aethel_outage_does_not_swap_to_more_expensive_gemini():
    primary = SimpleNamespace(get_products=AsyncMock(return_value=[
        Product(id=38, name="Gemini", price=100, stock=2, in_stock=True),
        Product(id=2, name="Claude Pro", price=2200, stock=2, in_stock=True)]))
    api = client(lambda request: httpx.Response(503))
    try:
        router = SupplierRouter(primary, api)
        assert [p.id for p in await router.get_supplier_products("thegodshop")] == [38, 2]
        with pytest.raises(PartnerAPIError):
            await router.get_supplier_products("aethel")
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_zero_supplier_balance_does_not_debit_buyer_or_create_order(sessions):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=catalog() if request.url.path.endswith("catalog")
                              else {"ok": True, "balance_usd": "0", "currency": "USD"})
    api = client(handler)
    try:
        async with sessions() as session:
            with pytest.raises(PartnerAPIError, match="INSUFFICIENT_BALANCE"):
                await OrderService(session, SupplierRouter(SimpleNamespace(), api), 15).buy_catalog_product(
                    1, -2, request_key="no-funds", expected_price=51)
            assert (await session.get(User, 1)).balance_rub == 500
            assert await session.scalar(select(Order)) is None
            assert "POST" not in calls
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_purchase_uses_correct_provider_id_key_and_persists_supplier(sessions):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("catalog"):
            return httpx.Response(200, json=catalog())
        if request.url.path.endswith("balance"):
            return httpx.Response(200, json={"ok": True, "balance_usd": "1", "currency": "USD"})
        return httpx.Response(200, json={"ok": True, "purchase_id": "aethel-abc", "delivery": ["KEY<test>"], "total_usd": "0.5"})
    api = client(handler)
    primary = SimpleNamespace(create_order=AsyncMock())
    try:
        async with sessions() as session:
            result, cost = await OrderService(session, SupplierRouter(primary, api), 15).buy_catalog_product(
                1, -2, request_key="aethel-request", expected_price=51)
            assert cost == 51 and result.delivered_data == "KEY<test>"
            order = await session.scalar(select(Order))
            assert order.supplier == "aethel" and order.supplier_order_ref == "aethel-abc"
            assert order.partner_order_id is None and order.status == OrderStatus.SUCCESS
            assert (await session.get(User, 1)).balance_rub == 449
            payload = json.loads(order.payload_json)
            assert payload["price_usd"] == "0.50" and payload["supplier_product_id"] == 2
            mutation = [request for request in requests if request.method == "POST"]
            assert len(mutation) == 1
            assert json.loads(mutation[0].content) == {"item_id": 2, "quantity": 1}
            assert len(mutation[0].headers["Idempotency-Key"]) == 64
            primary.create_order.assert_not_called()
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "server", "unknown-envelope", "price-mismatch"])
async def test_ambiguous_purchase_is_never_retried_or_refunded(sessions, failure):
    purchases = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=catalog() if request.url.path.endswith("catalog")
                                  else {"ok": True, "balance_usd": "1", "currency": "USD"})
        purchases.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout("Timeout")
        if failure == "server":
            return httpx.Response(503)
        if failure == "unknown-envelope":
            return httpx.Response(200, json={"ok": True, "delivery": ["saved key"]})
        return httpx.Response(200, json={"ok": True, "purchase_id": 25, "delivery": ["saved key"], "total_usd": "0.7"})
    api = client(handler)
    try:
        async with sessions() as session:
            with pytest.raises(OrderOutcomeUnknown):
                await OrderService(session, SupplierRouter(SimpleNamespace(), api), 15).buy_catalog_product(
                    1, -2, request_key="uncertain", expected_price=51)
            order = await session.scalar(select(Order))
            assert order.status == OrderStatus.UNCERTAIN
            assert (await session.get(User, 1)).balance_rub == 449
            assert len(purchases) == 1
            if failure in ("unknown-envelope", "price-mismatch"):
                assert json.loads(order.payload_json)["supplier_receipt"]["delivery"] == ["saved key"]
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_aethel_cannot_create_a_thegodshop_payment_invoice(sessions):
    api = client(lambda request: httpx.Response(200, json=catalog()))
    primary = SimpleNamespace(deposit_crypto=AsyncMock())
    try:
        product = await api.get_product(-2)
        quote = SimpleNamespace(claim_id=1, payable_price=43.5)
        assert not direct_available(quote, product, 1, True)
        async with sessions() as session:
            with pytest.raises(CheckoutError, match="не поддерживает"):
                await SupplierCheckoutService(session, SupplierRouter(primary, api), 15).create(
                    1, -2, expected_price=43.5, request_key="forged-direct")
            assert await session.scalar(select(Order)) is None
        primary.deposit_crypto.assert_not_called()
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_existing_database_migration_preserves_order():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from sqlalchemy import text
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, request_key VARCHAR, partner_order_id INTEGER)"))
        await connection.execute(text("INSERT INTO orders VALUES (1, 'old', 123)"))
        await connection.execute(text("CREATE TABLE deposits (method VARCHAR, external_id VARCHAR)"))
        await connection.run_sync(upgrade_schema)
        await connection.run_sync(upgrade_schema)
        row = (await connection.execute(text("SELECT supplier, supplier_order_ref, partner_order_id FROM orders"))).one()
        assert row == ("thegodshop", None, 123)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["price", "stock"])
async def test_provider_changes_before_purchase_do_not_debit_buyer(sessions, changed):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        return httpx.Response(200, json=catalog(
            price="0.60" if calls > 1 and changed == "price" else "0.50",
            stock=0 if calls > 1 and changed == "stock" else 5))
    api = client(handler)
    try:
        async with sessions() as session:
            with pytest.raises(PartnerAPIError):
                await OrderService(session, SupplierRouter(SimpleNamespace(), api), 15).buy_catalog_product(
                    1, -2, request_key="changed", expected_price=51)
            assert (await session.get(User, 1)).balance_rub == 500
            assert await session.scalar(select(Order)) is None
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_history_download_is_owned_and_html_is_escaped(sessions):
    from bot.handlers.history import cb_order_data, _build_history_text
    from bot.db.models import OrderType
    async with sessions() as session:
        order = Order(user_id=1, supplier="aethel", type=OrderType.CATALOG,
                      status=OrderStatus.SUCCESS, delivered_data="secret<key>&", user_price=51)
        session.add(order)
        await session.commit()
        assert "secret&lt;key&gt;&amp;" in _build_history_text([order], 0, 1)
        callback = SimpleNamespace(data=f"order_data:{order.id}", answer=AsyncMock(),
                                   message=SimpleNamespace(answer_document=AsyncMock()))
        await cb_order_data(callback, session, SimpleNamespace(id=2))
        callback.message.answer_document.assert_not_called()
        await cb_order_data(callback, session, SimpleNamespace(id=1))
        document = callback.message.answer_document.call_args.args[0]
        assert document.data == b"secret<key>&"


@pytest.mark.asyncio
async def test_aethel_promotion_uses_own_sku_and_preserves_payment_restriction(sessions):
    from bot.services.promotions import PromotionService
    api = client(lambda request: httpx.Response(200, json=catalog()))
    try:
        async with sessions() as session:
            service = PromotionService(session)
            await service.create("AETHEL_TEST", -2, 10)
            await service.activate("AETHEL_TEST", 1)
            product = await api.get_product(-2)
            quote = await service.quote(1, product, 1, 15)
            assert quote.payable_price == 43.5 and quote.regular_price == 51
            assert not direct_available(quote, product, 1, True)
            other = Product(id=2, name="Claude", price=2200, stock=3, in_stock=True)
            assert (await service.quote(1, other, 1, 15)).claim_id is None
    finally:
        await api.close()
