"""Aethel's documented seller API. No purchase retries or invented invoices."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib

import httpx

from bot.services.partner_api import OrderResult, PartnerAPIError, Product


def decimal_amount(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("Invalid amount")
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PartnerAPIError("INVALID_RESPONSE", "Некорректная сумма у поставщика") from exc


class AethelAPIClient:
    def __init__(self, base_url: str, api_key: str, usd_rub_rate: float):
        self.rate = decimal_amount(usd_rub_rate)
        if self.rate <= 0:
            raise ValueError("Aethel requires an explicitly configured USD/RUB funding rate")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(30, connect=5),
        )

    async def close(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, body=None, key=None):
        mutation = method != "GET"
        headers = {"Idempotency-Key": key} if key else None
        try:
            response = await self._client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise PartnerAPIError("TRANSPORT_ERROR", "Нет ответа от Aethel", outcome_unknown=mutation) from exc
        if response.is_error:
            codes = {401: "AUTH_FAILED", 403: "AUTH_FAILED", 402: "INSUFFICIENT_BALANCE",
                     404: "PRODUCT_NOT_FOUND", 409: "PURCHASE_CONFLICT", 429: "RATE_LIMIT_EXCEEDED"}
            raise PartnerAPIError(codes.get(response.status_code, "HTTP_ERROR"),
                                  "Запрос Aethel отклонён", response.status_code,
                                  outcome_unknown=mutation and response.status_code >= 500)
        try:
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError("Expected successful object")
        except (ValueError, TypeError) as exc:
            raise PartnerAPIError("INVALID_RESPONSE", "Некорректный ответ Aethel",
                                  outcome_unknown=mutation) from exc
        return data

    async def get_balance_usd(self) -> Decimal:
        data = await self._request("GET", "v1/balance")
        if data.get("currency") != "USD":
            raise PartnerAPIError("INVALID_RESPONSE", "Неизвестная валюта баланса Aethel")
        return decimal_amount(data.get("balance_usd"))

    async def get_products(self) -> list[Product]:
        data = await self._request("GET", "v1/catalog")
        products = []
        seen = set()
        try:
            for category in data["categories"]:
                for item in category["products"]:
                    # Only the requested Gemini link and ChatGPT Plus plans.
                    # K12 and other services are not equivalent replacements.
                    name = item["name"]
                    normalized = name.lower().replace(" ", "")
                    if item["id"] == 2 and "gemini" in normalized:
                        group = "Gemini"
                    elif "chatgptplus" in normalized:
                        group = "ChatGPT"
                    else:
                        continue
                    item_id, stock = item["id"], item["available_quantity"]
                    if type(item_id) is not int or item_id <= 0 or item_id in seen:
                        raise ValueError("Invalid item id")
                    if type(stock) is not int or stock < 0 or item["currency"] != "USD":
                        raise ValueError("Invalid stock or currency")
                    seen.add(item_id)
                    usd = decimal_amount(item["price_usd"])
                    rub = (usd * self.rate).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
                    products.append(Product(
                        id=-item_id, name=name, category=group, price=float(rub),
                        stock=stock, in_stock=stock > 0, supplier="aethel",
                        description=item.get("description", ""), price_usd=str(usd),
                        usd_rub_rate=str(self.rate), direct_payment_supported=False,
                    ))
        except (KeyError, TypeError, ValueError) as exc:
            raise PartnerAPIError("INVALID_RESPONSE", "Некорректный каталог Aethel") from exc
        return products

    async def get_product(self, product_id: int) -> Product:
        for product in await self.get_products():
            if product.id == product_id:
                return product
        raise PartnerAPIError("PRODUCT_NOT_FOUND", "Товар Aethel не найден", 404)

    async def prepare(self, product: Product, qty: int):
        current = await self.get_product(product.id)
        if current.price_usd != product.price_usd or current.usd_rub_rate != product.usd_rub_rate:
            raise PartnerAPIError("PRICE_CHANGED", "Цена поставщика изменилась, обновите карточку")
        if not current.in_stock or current.stock < qty:
            raise PartnerAPIError("OUT_OF_STOCK", "Товар Aethel закончился", 409)
        if await self.get_balance_usd() < decimal_amount(product.price_usd) * qty:
            raise PartnerAPIError("INSUFFICIENT_BALANCE", "Недостаточно средств на балансе Aethel", 402)

    async def purchase(self, product: Product, qty: int, request_key: str) -> OrderResult:
        # Persisted request_key determines the provider idempotency key. If an
        # outcome is unknown, the caller retains the order for manual review.
        key = hashlib.sha256(("atlas:aethel:" + request_key).encode()).hexdigest()
        data = await self._request("POST", "v1/purchases",
                                   body={"item_id": -product.id, "quantity": qty}, key=key)
        try:
            reference = data["purchase_id"]
            delivery = data["delivery"]
            if not isinstance(delivery, (str, list)) or not delivery:
                raise ValueError("No delivery")
            if isinstance(delivery, list):
                if len(delivery) != qty or any(not isinstance(value, str) or not value for value in delivery):
                    raise ValueError("Incomplete delivery")
                delivery = "\n\n".join(delivery)
            # Documented delivery is required. Unexpected success envelopes
            # are quarantined rather than reported as success or retried.
            if type(reference) not in (str, int) or isinstance(reference, bool):
                raise ValueError("Invalid purchase reference")
            if "total_usd" in data and decimal_amount(data["total_usd"]) != decimal_amount(product.price_usd) * qty:
                raise ValueError("Purchase price mismatch")
            return OrderResult(order_id=reference, delivered_data=delivery, price=product.price * qty)
        except (KeyError, ValueError, TypeError, PartnerAPIError) as exc:
            error = PartnerAPIError("INVALID_RESPONSE", "Выдача Aethel требует проверки поддержкой",
                                    outcome_unknown=True)
            error.supplier_receipt = data
            raise error from exc

    async def get_purchase(self, reference: str):
        from urllib.parse import quote
        return await self._request("GET", "v1/purchases/" + quote(reference, safe=""))
