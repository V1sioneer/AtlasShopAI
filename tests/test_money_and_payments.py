import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, Deposit, Transaction, User
from bot.services.settlement import InvalidPayment, settle_payment
from bot.utils.money import money, topup_amount


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'payments.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_money_rejects_non_finite_and_excess_precision():
    for value in ("nan", "inf", "-inf", "12.345"):
        with pytest.raises(ValueError):
            money(value)
    assert topup_amount("50.01") == 51
    with pytest.raises(ValueError):
        topup_amount("50001")


@pytest.mark.asyncio
async def test_settlement_credits_saved_owner_and_amount_exactly_once(sessions):
    async with sessions() as session:
        session.add(User(id=100, balance_rub=10))
        session.add(
            Deposit(
                user_id=100,
                amount_rub=250,
                method="cryptobot",
                external_id="77",
                status="pending",
            )
        )
        await session.commit()

        provider = {
            "invoice_id": 77,
            "status": "paid",
            "amount": 250,
            "currency": "RUB",
            "payload": "100:250",
        }
        first = await settle_payment(session, "cryptobot", "77", provider, user_id=100)
        second = await settle_payment(session, "cryptobot", "77", provider, user_id=100)

        assert first.applied is True
        assert second.applied is False
        assert first.balance == second.balance == 260
        assert len((await session.execute(Transaction.__table__.select())).all()) == 1


@pytest.mark.asyncio
async def test_settlement_rejects_tampering(sessions):
    async with sessions() as session:
        session.add_all([User(id=100, balance_rub=0), User(id=200, balance_rub=0)])
        session.add(
            Deposit(
                user_id=100,
                amount_rub=250,
                method="yookassa",
                external_id="pay-1",
                status="pending",
            )
        )
        await session.commit()
        provider = {
            "payment_id": "pay-1",
            "status": "succeeded",
            "amount": 999,
            "currency": "RUB",
            "metadata": {"user_id": "100"},
        }
        with pytest.raises(InvalidPayment):
            await settle_payment(session, "yookassa", "pay-1", provider, user_id=100)
        provider["amount"] = 250
        with pytest.raises(InvalidPayment):
            await settle_payment(session, "yookassa", "pay-1", provider, user_id=200)
