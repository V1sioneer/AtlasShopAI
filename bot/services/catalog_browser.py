"""Supplier-specific browsing and persistent customer preferences."""
from __future__ import annotations

import hashlib

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from bot.db.models import CatalogPreference, FavoriteProduct
from bot.services.partner_api import PartnerAPIError

SUPPLIERS = {"g": "thegodshop", "a": "aethel"}
SUPPLIER_LABELS = {"g": "Поставщик 1 · TheGodShop", "a": "Поставщик 2 · Aethel"}


def supplier_code(product_id: int) -> str:
    return "a" if product_id < 0 else "g"


def category_token(category: str) -> str:
    return hashlib.sha256(category.encode("utf-8")).hexdigest()[:12]


async def preferences(session, user_id):
    prefs = await session.get(CatalogPreference, user_id)
    if prefs is None:
        prefs = CatalogPreference(user_id=user_id, supplier="thegodshop", in_stock_only=False, search_query="")
        session.add(prefs)
        await session.commit()
    return prefs


async def supplier_products(api, code: str):
    if code not in SUPPLIERS:
        raise PartnerAPIError("SUPPLIER_UNAVAILABLE", "Поставщик не найден")
    fetch = getattr(api, "get_supplier_products", None)
    if fetch is not None:
        return await fetch(SUPPLIERS[code])
    return [p for p in await api.get_products() if p.supplier == SUPPLIERS[code]]


def filter_products(products, prefs, category="all"):
    query = prefs.search_query.casefold().strip()
    return [p for p in products
            if (category == "all" or category_token(p.category or "Другое") == category)
            and (not prefs.in_stock_only or (p.in_stock and p.stock > 0))
            and (not query or query in (p.name + " " + p.category).casefold())]


async def favorite_ids(session, user_id):
    return set((await session.scalars(select(FavoriteProduct.product_id).where(
        FavoriteProduct.user_id == user_id))).all())


async def set_favorite(session, user_id, product_id, wanted):
    if wanted:
        await session.execute(insert(FavoriteProduct).values(user_id=user_id, product_id=product_id)
                              .on_conflict_do_nothing())
    else:
        await session.execute(delete(FavoriteProduct).where(
            FavoriteProduct.user_id == user_id, FavoriteProduct.product_id == product_id))
    await session.commit()
