import pytest
from sqlalchemy import text

from bot.db.engine import close_db, get_session_factory, init_db
from bot.db.models import Order, OrderStatus, OrderType, User


@pytest.mark.asyncio
async def test_startup_migrates_interrupted_orders_and_enables_wal(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}"
    await init_db(url)
    factory = get_session_factory()
    async with factory() as session:
        session.add(User(id=1, balance_rub=100))
        session.add(
            Order(
                user_id=1,
                type=OrderType.CATALOG,
                partner_price=10,
                user_price=12,
                status=OrderStatus.PENDING,
                request_key="interrupted",
            )
        )
        await session.commit()
    await close_db()

    await init_db(url)
    factory = get_session_factory()
    async with factory() as session:
        order = await session.get(Order, 1)
        assert order.status == OrderStatus.UNCERTAIN
        assert order.error_code == "PROCESS_INTERRUPTED"
        assert (await session.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
    await close_db()
