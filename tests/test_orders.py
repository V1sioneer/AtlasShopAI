import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, Order, OrderStatus, Transaction, User
from bot.services.orders import DuplicateOrder, OrderOutcomeUnknown, OrderService
from bot.services.partner_api import OrderResult, PartnerAPIError, Product


class FakeAPI:
    def __init__(self, failure: PartnerAPIError | None = None):
        self.failure = failure
        self.calls = 0

    async def get_product(self, product_id: int) -> Product:
        return Product(
            id=product_id,
            name="Test",
            price=100,
            in_stock=True,
            stock=10,
            category="AI",
        )

    async def create_order(self, product_id: int, qty: int) -> OrderResult:
        self.calls += 1
        if self.failure:
            raise self.failure
        return OrderResult(order_id=900, delivered_data="KEY", price=100)


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        value.add(User(id=1, balance_rub=500))
        await value.commit()
        yield value
    await engine.dispose()


@pytest.mark.asyncio
async def test_successful_order_cannot_be_submitted_twice(session):
    api = FakeAPI()
    service = OrderService(session, api, 15)
    result, charged = await service.buy_catalog_product(
        1, 10, request_key="same-click"
    )
    assert result.delivered_data == "KEY"
    assert charged == 115
    with pytest.raises(DuplicateOrder):
        await service.buy_catalog_product(1, 10, request_key="same-click")
    assert api.calls == 1
    assert (await session.get(User, 1)).balance_rub == 385


@pytest.mark.asyncio
async def test_definitive_provider_failure_refunds_once(session):
    api = FakeAPI(PartnerAPIError("OUT_OF_STOCK", "Нет товара", 409))
    service = OrderService(session, api, 15)
    with pytest.raises(PartnerAPIError):
        await service.buy_catalog_product(1, 10, request_key="failed")
    order = (await session.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.FAILED
    assert (await session.get(User, 1)).balance_rub == 500
    assert len((await session.execute(select(Transaction))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_unknown_provider_outcome_freezes_funds(session):
    api = FakeAPI(
        PartnerAPIError(
            "TRANSPORT_ERROR", "Нет ответа", outcome_unknown=True
        )
    )
    service = OrderService(session, api, 15)
    with pytest.raises(OrderOutcomeUnknown):
        await service.buy_catalog_product(1, 10, request_key="unknown")
    order = (await session.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.UNCERTAIN
    assert (await session.get(User, 1)).balance_rub == 385
    assert len((await session.execute(select(Transaction))).scalars().all()) == 1
