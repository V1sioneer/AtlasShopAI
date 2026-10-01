from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.services.partner_api import Product
from bot.services.pricing import calculate_user_price
from bot.utils.formatting import format_price


# ── Reply Keyboard (main menu) ──────────────────────────────────────


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🛒 Каталог"),
                KeyboardButton(text="🎮 Steam / Игры"),
            ],
            [
                KeyboardButton(text="💰 Мой баланс"),
                KeyboardButton(text="📜 История"),
            ],
            [
                KeyboardButton(text="ℹ️ Информация"),
                KeyboardButton(text="🎟 Промокод"),
            ],
        ],
        resize_keyboard=True,
    )


def help_info_kb(support_username: str = "V1sionHere") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Поддержка клиентов",
                    url=f"https://t.me/{support_username.lstrip('@')}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 Пользовательское соглашение (Оферта)",
                    url="https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Политика конфиденциальности",
                    url="https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99",
                ),
            ],
        ]
    )



# ── Categories & Catalog ─────────────────────────────────────────────

CATEGORY_ICONS = {
    "chat gpt": "🤖",
    "claude": "🧠",
    "gemini": "✨",
    "perplexity": "🔍",
    "grok": "⚡",
    "capcut": "🎬",
    "spotify": "🎵",
    "duolingo": "🦉",
    "гарантией": "🛡️",
}


def get_category_icon(category_name: str) -> str:
    name_lower = category_name.lower()
    for key, icon in CATEGORY_ICONS.items():
        if key in name_lower:
            return icon
    return "📁"


def categories_menu_kb(categories: list[str]) -> InlineKeyboardMarkup:
    """Keyboard displaying available product categories."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for cat in sorted(categories):
        icon = get_category_icon(cat)
        btn = InlineKeyboardButton(
            text=f"{icon} {cat}",
            callback_data=f"cat:{cat}:0",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="📦 Все товары", callback_data="cat:all:0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def catalog_page_kb(
    products: list[Product],
    category: str,
    page: int,
    total_pages: int,
    markup_percent: float,
    prices: dict[int, float] | None = None,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for p in products:
        user_price = prices[p.id] if prices is not None else calculate_user_price(p.price, markup_percent)
        stock_icon = "✅" if p.in_stock else "❌"
        buttons.append([
            InlineKeyboardButton(
                text=f"{stock_icon} {p.name} — {format_price(user_price)}",
                callback_data=f"product:{p.id}:{category}",
            )
        ])

    # Pagination
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"cat:{category}:{page - 1}")
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="noop"
        )
    )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(text="➡️", callback_data=f"cat:{category}:{page + 1}")
        )
    if nav:
        buttons.append(nav)

    # Back to categories
    buttons.append([
        InlineKeyboardButton(text="⬅️ К категориям", callback_data="catalog_cats"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_card_kb(product_id: int, category: str = "all") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Купить 1 шт.",
                    callback_data=f"buy_catalog:{product_id}:1",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 Купить N шт.",
                    callback_data=f"buy_catalog_qty:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"cat:{category}:0",
                ),
            ],
        ]
    )


def confirm_purchase_kb(product_id: int, qty: int, price: float, direct: bool = False,
                        wallet_available: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if direct:
        rows.append([InlineKeyboardButton(
            text="💳 Оплатить криптой напрямую", callback_data=f"supplier_checkout:{product_id}:{qty}:{price:.2f}"
        )])
    if wallet_available:
        rows.append([
            InlineKeyboardButton(
                    text="💰 Купить с баланса" if direct else "✅ Подтвердить",
                    callback_data=f"confirm_catalog:{product_id}:{qty}:{price:.2f}",
                )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_purchase")])
    return InlineKeyboardMarkup(inline_keyboard=rows)




def confirm_external_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"confirm_ext:{action}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel_purchase",
                ),
            ],
        ]
    )


# ── Steam ────────────────────────────────────────────────────────────


def steam_games_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Пополнение Steam",
                    callback_data="steam_topup",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎮 Игры",
                    callback_data="games_list",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"),
            ],
        ]
    )


# ── Balance ──────────────────────────────────────────────────────────


def balance_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Пополнить",
                    callback_data="topup_balance",
                ),
            ],
        ]
    )


# ── History pagination ───────────────────────────────────────────────


def history_page_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"history_page:{page - 1}")
        )
    nav.append(
        InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(text="➡️", callback_data=f"history_page:{page + 1}")
        )
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="💳 Счета прямой оплаты", callback_data="direct_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Generic ──────────────────────────────────────────────────────────


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
