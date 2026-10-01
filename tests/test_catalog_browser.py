from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, FavoriteProduct, User
from bot.handlers.catalog import cb_category_page, cb_product_card, cb_supplier, cb_buy_catalog, cb_confirm_catalog
from bot.keyboards.catalog import categories_keyboard, supplier_menu
from bot.services.catalog_browser import category_token, favorite_ids, filter_products, preferences, set_favorite
from bot.services.partner_api import Product


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'browser.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([User(id=1), User(id=2)])
        await session.commit()
    yield factory
    await engine.dispose()


def products():
    return [Product(id=38, name="Gemini Pro", category="Gemini", price=100, stock=2, in_stock=True),
            Product(id=36, name="Claude Pro", category="Claude", price=1890, stock=0, in_stock=False),
            Product(id=-2, name="Gemini AI PRO 18 месяцев", category="GEMINI AI", price=43.5,
                    stock=3, in_stock=True, supplier="aethel", direct_payment_supported=False)]


def callback(data):
    return SimpleNamespace(data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())


@pytest.mark.asyncio
async def test_favorites_are_persistent_idempotent_and_private(sessions):
    async with sessions() as session:
        await set_favorite(session, 1, 38, True)
        await set_favorite(session, 1, 38, True)
        await set_favorite(session, 1, -2, True)
    async with sessions() as session:
        assert await favorite_ids(session, 1) == {38, -2}
        assert await favorite_ids(session, 2) == set()
        await set_favorite(session, 2, 38, False)
        assert await favorite_ids(session, 1) == {38, -2}
        await set_favorite(session, 1, 38, False)
        assert await favorite_ids(session, 1) == {-2}


@pytest.mark.asyncio
async def test_search_and_stock_filter_persist_across_sessions(sessions):
    async with sessions() as session:
        prefs = await preferences(session, 1)
        prefs.search_query, prefs.in_stock_only, prefs.supplier = "GEMINI", True, "aethel"
        await session.commit()
    async with sessions() as session:
        prefs = await preferences(session, 1)
        assert prefs.supplier == "aethel"
        assert [p.id for p in filter_products(products(), prefs)] == [38, -2]
        assert [p.id for p in filter_products(products(), prefs, category_token("Gemini"))] == [38]


def test_supplier_choices_and_multibyte_category_callbacks_fit_telegram_limit():
    rows = supplier_menu().inline_keyboard
    assert rows[0][0].callback_data == "supplier:g" and rows[1][0].callback_data == "supplier:a"
    category = "🛡 " + "Очень длинная категория:" * 10
    product = products()[0].model_copy(update={"category": category})
    keyboard = categories_keyboard([product], "g", SimpleNamespace(search_query="", in_stock_only=False))
    assert all(len(button.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for button in row)
    assert category_token(category) != category_token("another")


@pytest.mark.asyncio
async def test_supplier_selection_loads_only_chosen_supplier_and_names_it(sessions):
    api = SimpleNamespace(get_supplier_products=AsyncMock(return_value=[products()[2]]))
    cb = callback("supplier:a")
    async with sessions() as session:
        await cb_supplier(cb, api, session, await session.get(User, 1), SimpleNamespace(clear=AsyncMock()))
        api.get_supplier_products.assert_awaited_once_with("aethel")
        assert "Поставщик 2 · Aethel" in cb.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_category_is_supplier_specific_and_stock_filter_changes_results(sessions):
    api = SimpleNamespace(get_supplier_products=AsyncMock(return_value=products()[:2]))
    cb = callback("cat:g:all:0")
    async with sessions() as session:
        prefs = await preferences(session, 1)
        prefs.in_stock_only = True
        await session.commit()
        await cb_category_page(cb, api, 15, session, await session.get(User, 1))
        rows = cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        callbacks = [button.callback_data for row in rows for button in row]
        assert any(value.startswith("product:38:") for value in callbacks)
        assert not any(value.startswith("product:36:") for value in callbacks)
        api.get_supplier_products.assert_awaited_once_with("thegodshop")


@pytest.mark.asyncio
async def test_long_product_terms_are_downloadable_and_card_fits_message_limit(sessions):
    product = products()[2].model_copy(update={"description": "<&>" * 2000})
    cb = callback("product:-2:all")
    api = SimpleNamespace(get_product=AsyncMock(return_value=product))
    async with sessions() as session:
        await cb_product_card(cb, api, 15, session, await session.get(User, 1))
        message = cb.message.edit_text.call_args.args[0]
        assert len(message) < 4096 and "&lt;&amp;&gt;" in message
        buttons = cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        assert any(b.callback_data == "product_terms:-2" for row in buttons for b in row)


@pytest.mark.asyncio
async def test_normal_thegodshop_confirmation_has_direct_payment_and_no_wallet(sessions):
    cb = callback("buy_catalog:38:2")
    api = SimpleNamespace(get_product=AsyncMock(return_value=products()[0]))
    async with sessions() as session:
        await cb_buy_catalog(cb, api, 15, session, await session.get(User, 1), cryptobot=SimpleNamespace())
        text = cb.message.edit_text.call_args.args[0]
        assert "230 ₽" in text and "30 ₽" in text and "200 ₽" in text
        callbacks = [b.callback_data for row in cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        assert "supplier_checkout:38:2:230.00" in callbacks
        assert not any(value.startswith("confirm_catalog:") for value in callbacks)


@pytest.mark.asyncio
async def test_old_thegodshop_wallet_confirmation_redirects_before_spending(sessions):
    cb = callback("confirm_catalog:38:1:115.00")
    api = SimpleNamespace(create_order=AsyncMock())
    async with sessions() as session:
        await cb_confirm_catalog(cb, session, await session.get(User, 1), api, 15, [])
        api.create_order.assert_not_called()
        assert "пополнять баланс бота не требуется" in cb.message.edit_text.call_args.args[0]
