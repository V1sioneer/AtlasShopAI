"""Route catalog SKUs; positive IDs remain TheGodShop, negative IDs are Aethel."""
from __future__ import annotations

import asyncio

import structlog

from bot.services.aethel_api import AethelAPIClient
from bot.services.partner_api import PartnerAPIClient, PartnerAPIError, Product

logger = structlog.get_logger()


class SupplierRouter:
    def __init__(self, primary: PartnerAPIClient, aethel: AethelAPIClient | None = None):
        self.primary = primary
        self.aethel = aethel

    def __getattr__(self, name):
        # Steam, games, balance, admin deposits, and existing direct invoices
        # continue to use their original provider.
        return getattr(self.primary, name)

    async def get_products(self):
        if self.aethel is None:
            return await self.primary.get_products()
        results = await asyncio.gather(self.primary.get_products(), self.aethel.get_products(),
                                       return_exceptions=True)
        combined = []
        for provider, result in zip(("thegodshop", "aethel"), results):
            if isinstance(result, Exception):
                logger.warning("supplier_catalog_unavailable", supplier=provider, error=type(result).__name__)
                continue
            combined.extend(result)
        if all(isinstance(result, Exception) for result in results):
            raise PartnerAPIError("CATALOG_UNAVAILABLE", "Каталог временно недоступен")
        return combined

    async def get_supplier_products(self, supplier: str):
        if supplier == "thegodshop":
            return await self.primary.get_products()
        if supplier == "aethel" and self.aethel is not None:
            return await self.aethel.get_products()
        raise PartnerAPIError("SUPPLIER_UNAVAILABLE", "Этот поставщик временно недоступен")

    async def get_product(self, product_id: int):
        if product_id < 0:
            if self.aethel is None:
                raise PartnerAPIError("SUPPLIER_UNAVAILABLE", "Aethel пока не подключён")
            return await self.aethel.get_product(product_id)
        # Old links and promotions retain their original SKU and terms.
        return await self.primary.get_product(product_id)

    async def prepare_catalog_purchase(self, session, product, qty, amount):
        if product.supplier == "aethel":
            await self.aethel.prepare(product, qty)
        else:
            from bot.services.supplier_funding import require_supplier_funds
            await require_supplier_funds(session, self.primary, amount)

    async def purchase_catalog(self, product, qty, request_key):
        if product.supplier == "aethel":
            return await self.aethel.purchase(product, qty, request_key)
        return await self.primary.create_order(product.id, qty)

    async def create_order(self, product_id, qty):
        if product_id < 0:
            raise PartnerAPIError("UNSUPPORTED_PAYMENT", "Aethel не поддерживает этот способ оплаты")
        return await self.primary.create_order(product_id, qty)

    async def close(self):
        await self.primary.close()
        if self.aethel is not None:
            await self.aethel.close()
