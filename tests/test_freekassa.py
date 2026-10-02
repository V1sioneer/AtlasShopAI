import hashlib
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, Deposit, Transaction, User
from bot.services.payments import FreeKassaPayment
from bot.services.freekassa_webhook import FreeKassaWebhookServer
from bot.services.settlement import InvalidPayment, settle_payment


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'freekassa_test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_freekassa_payment_url_generation():
    fk = FreeKassaPayment(
        shop_id="76435",
        secret_1="secret1_val",
        secret_2="secret2_val",
    )
    url = fk.create_payment_url(amount=250.0, order_id=42, currency="RUB", user_id=12345)
    expected_sign = hashlib.md5(b"76435:250:secret1_val:RUB:42").hexdigest()
    assert f"m=76435" in url
    assert f"oa=250" in url
    assert f"o=42" in url
    assert f"s={expected_sign}" in url
    assert f"currency=RUB" in url
    assert f"us_user_id=12345" in url

    # Test amount with kopecks
    url_kopecks = fk.create_payment_url(amount=100.50, order_id="order-99", currency="RUB")
    sign_kopecks = hashlib.md5(b"76435:100.50:secret1_val:RUB:order-99").hexdigest()
    assert f"oa=100.50" in url_kopecks
    assert f"s={sign_kopecks}" in url_kopecks


def test_freekassa_verify_notification():
    fk = FreeKassaPayment(
        shop_id="76435",
        secret_1="secret1_val",
        secret_2="secret2_val",
    )
    # Valid notification
    sign = hashlib.md5(b"76435:250:secret2_val:42").hexdigest()
    data = {
        "MERCHANT_ID": "76435",
        "AMOUNT": "250",
        "MERCHANT_ORDER_ID": "42",
        "SIGN": sign,
    }
    valid, reason = fk.verify_notification(data)
    assert valid is True
    assert reason == ""

    # Invalid signature
    bad_data = {**data, "SIGN": "invalid_hash"}
    valid, reason = fk.verify_notification(bad_data)
    assert valid is False
    assert "подпись" in reason

    # Merchant mismatch
    other_merchant = {**data, "MERCHANT_ID": "99999"}
    valid, reason = fk.verify_notification(other_merchant)
    assert valid is False
    assert "магазина" in reason

    # Missing field
    missing_data = {"MERCHANT_ID": "76435", "AMOUNT": "250"}
    valid, reason = fk.verify_notification(missing_data)
    assert valid is False
    assert "обязательные" in reason


@pytest.mark.asyncio
async def test_freekassa_settlement_and_idempotency(sessions):
    async with sessions() as session:
        session.add(User(id=100, balance_rub=50))
        session.add(
            Deposit(
                id=42,
                user_id=100,
                amount_rub=250,
                method="freekassa",
                external_id="42",
                status="pending",
            )
        )
        await session.commit()

        provider = {
            "order_id": "42",
            "amount": 250,
            "currency": "RUB",
            "status": "completed",
        }
        first = await settle_payment(session, "freekassa", "42", provider, user_id=100)
        second = await settle_payment(session, "freekassa", "42", provider, user_id=100)

        assert first.applied is True
        assert second.applied is False
        assert first.balance == second.balance == 300
        assert len((await session.execute(Transaction.__table__.select())).all()) == 1


@pytest.mark.asyncio
async def test_freekassa_webhook_server_flow(sessions):
    fk = FreeKassaPayment(
        shop_id="76435",
        secret_1="secret1_val",
        secret_2="secret2_val",
    )
    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock()

    # Pre-populate user and deposit
    async with sessions() as session:
        session.add(User(id=777, balance_rub=100))
        session.add(
            Deposit(
                id=55,
                user_id=777,
                amount_rub=500,
                method="freekassa",
                external_id="55",
                status="pending",
            )
        )
        await session.commit()

    server = FreeKassaWebhookServer(
        freekassa=fk,
        session_factory=sessions,
        bot=bot_mock,
        admin_ids=[999],
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # 1. Health check GET
        resp_get = await client.get("/freekassa/notify")
        assert resp_get.status == 200
        assert (await resp_get.text()) == "YES"

        # 2. Invalid sign POST -> 400
        resp_bad = await client.post("/freekassa/notify", data={
            "MERCHANT_ID": "76435",
            "AMOUNT": "500",
            "MERCHANT_ORDER_ID": "55",
            "SIGN": "wrong",
        })
        assert resp_bad.status == 400

        # 3. Valid POST -> 200 YES and applied
        valid_sign = hashlib.md5(b"76435:500:secret2_val:55").hexdigest()
        resp_ok = await client.post("/freekassa/notify", data={
            "MERCHANT_ID": "76435",
            "AMOUNT": "500",
            "MERCHANT_ORDER_ID": "55",
            "SIGN": valid_sign,
        })
        assert resp_ok.status == 200
        assert (await resp_ok.text()) == "YES"

        # Check notifications sent
        assert bot_mock.send_message.call_count == 2  # 1 to user, 1 to admin

        # Check balance in database
        async with sessions() as session:
            user = (await session.execute(User.__table__.select().where(User.id == 777))).fetchone()
            assert user.balance_rub == 600.0

        # 4. Duplicate POST -> still returns YES, no duplicate balance
        resp_dup = await client.post("/freekassa/notify", data={
            "MERCHANT_ID": "76435",
            "AMOUNT": "500",
            "MERCHANT_ORDER_ID": "55",
            "SIGN": valid_sign,
        })
        assert resp_dup.status == 200
        assert (await resp_dup.text()) == "YES"

        async with sessions() as session:
            user = (await session.execute(User.__table__.select().where(User.id == 777))).fetchone()
            assert user.balance_rub == 600.0
    finally:
        await client.close()
