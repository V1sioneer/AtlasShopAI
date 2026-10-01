from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

from bot.services.catalog_browser import SUPPLIER_LABELS, category_token
from bot.utils.formatting import format_price


def supplier_menu():
    return Keyboard(inline_keyboard=[
        [Button(text=label, callback_data=f"supplier:{code}")]
        for code, label in SUPPLIER_LABELS.items()
    ] + [[Button(text="⭐ Избранное", callback_data="favorites:0")]])


def browser_controls(code, prefs):
    rows = [[Button(text="🔎 Поиск", callback_data=f"catalog_search:{code}"),
             Button(text="✅ Только в наличии" if prefs.in_stock_only else "📦 Все остатки",
                    callback_data=f"catalog_stock:{code}")]]
    if prefs.search_query:
        rows.append([Button(text="Сбросить поиск", callback_data=f"catalog_clear:{code}")])
    rows.append([Button(text="⭐ Избранное", callback_data="favorites:0")])
    return rows


def categories_keyboard(products, code, prefs):
    categories = sorted({p.category or "Другое" for p in products})
    rows = [[Button(text=category, callback_data=f"cat:{code}:{category_token(category)}:0")]
            for category in categories]
    rows.insert(0, [Button(text="Все товары", callback_data=f"cat:{code}:all:0")])
    rows.extend(browser_controls(code, prefs))
    rows.append([Button(text="⬅️ Выбор поставщика", callback_data="catalog_cats")])
    return Keyboard(inline_keyboard=rows)


def products_keyboard(products, prices, code, token, page, pages, prefs, *, favorites=False):
    rows = [[Button(text=f"{'✅' if p.in_stock else '❌'} {p.name} — {format_price(prices[p.id])}",
                    callback_data=f"product:{p.id}:{'fav' if favorites else token}")]
            for p in products]
    navigation = []
    def target(number):
        return f"favorites:{number}" if favorites else f"cat:{code}:{token}:{number}"
    if page > 0:
        navigation.append(Button(text="←", callback_data=target(page - 1)))
    navigation.append(Button(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        navigation.append(Button(text="→", callback_data=target(page + 1)))
    rows.append(navigation)
    if not favorites:
        rows.extend(browser_controls(code, prefs))
        rows.append([Button(text="⬅️ Категории", callback_data=f"supplier:{code}")])
    rows.append([Button(text="⬅️ Выбор поставщика", callback_data="catalog_cats")])
    return Keyboard(inline_keyboard=rows)


def product_keyboard(product, category, is_favorite, *, full_terms=False):
    code = "a" if product.id < 0 else "g"
    rows = []
    if product.in_stock and product.stock > 0:
        rows.extend([[Button(text="Купить 1 шт.", callback_data=f"buy_catalog:{product.id}:1")],
                     [Button(text="Купить несколько", callback_data=f"buy_catalog_qty:{product.id}")]])
    rows.append([Button(text="🔄 Обновить цену и наличие", callback_data=f"product:{product.id}:{category}")])
    rows.append([Button(text="Убрать из избранного" if is_favorite else "⭐ В избранное",
                        callback_data=f"fav_{'del' if is_favorite else 'add'}:{product.id}:{category}")])
    if full_terms:
        rows.append([Button(text="📄 Полные условия товара", callback_data=f"product_terms:{product.id}")])
    rows.append([Button(text="⬅️ Назад", callback_data="favorites:0" if category == "fav"
                        else f"cat:{code}:{category}:0")])
    return Keyboard(inline_keyboard=rows)
