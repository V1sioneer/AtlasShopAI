from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Optional

from bot.utils.money import money

import httpx
import structlog

logger = structlog.get_logger()


class CryptoBotPayment:
    """CryptoBot payment integration via @CryptoBot API."""

    BASE_URL = "https://pay.crypt.bot/api"

    def __init__(self, token: str) -> None:
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Crypto-Pay-API-Token": token},
            timeout=15.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def create_invoice(
        self,
        amount: float,
        currency: str = "RUB",
        description: str = "Пополнение баланса",
        payload: str = "",
    ) -> dict:
        """Create a payment invoice. Returns dict with invoice_id, pay_url, etc."""
        amount = money(amount)
        if amount <= 0:
            raise ValueError("Некорректная сумма")
        resp = await self._client.post(
            "/createInvoice",
            json={
                "currency_type": "fiat",
                "fiat": currency,
                "amount": str(amount),
                "description": description,
                "payload": payload,
                "expires_in": 3600,  # 1 hour
            },
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"CryptoBot error: {data}")
        result = data["result"]
        return {
            "invoice_id": result["invoice_id"],
            "pay_url": result["pay_url"],
            "amount": float(result["amount"]),
            "currency": result.get("fiat") or result.get("asset"),
            "status": result["status"],
        }

    async def get_invoice(self, invoice_id: int) -> dict:
        """Check invoice status."""
        resp = await self._client.post(
            "/getInvoices",
            json={"invoice_ids": str(invoice_id)},
        )
        data = resp.json()
        if not data.get("ok") or not data["result"]["items"]:
            raise RuntimeError(f"Invoice {invoice_id} not found")
        inv = data["result"]["items"][0]
        return {
            "invoice_id": inv["invoice_id"],
            "status": inv["status"],  # active | paid | expired
            "amount": float(inv["amount"]),
            "currency": inv.get("fiat") or inv.get("asset"),
            "payload": inv.get("payload", ""),
        }

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        """Verify CryptoBot webhook signature."""
        secret = hashlib.sha256(self.token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


class YooKassaPayment:
    """YooKassa (ЮKassa) payment integration."""

    BASE_URL = "https://api.yookassa.ru/v3"

    def __init__(self, shop_id: str, secret_key: str) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            auth=(shop_id, secret_key),
            timeout=15.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def create_payment(
        self,
        amount: float,
        description: str = "Пополнение баланса",
        return_url: str = "https://t.me",
        metadata: Optional[dict] = None,
    ) -> dict:
        """Create a payment. Returns dict with payment_id, confirmation_url."""
        idempotence_key = str(uuid.uuid4())
        amount = money(amount)
        if amount <= 0:
            raise ValueError("Некорректная сумма")
        resp = await self._client.post(
            "/payments",
            headers={"Idempotence-Key": idempotence_key},
            json={
                "amount": {
                    "value": f"{amount:.2f}",
                    "currency": "RUB",
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": return_url,
                },
                "capture": True,
                "description": description,
                "metadata": metadata or {},
            },
        )
        data = resp.json()
        if "id" not in data:
            raise RuntimeError(f"YooKassa error: {data}")
        return {
            "payment_id": data["id"],
            "confirmation_url": data["confirmation"]["confirmation_url"],
            "status": data["status"],
            "amount": float(data["amount"]["value"]),
        }

    async def get_payment(self, payment_id: str) -> dict:
        """Check payment status."""
        resp = await self._client.get(f"/payments/{payment_id}")
        data = resp.json()
        return {
            "payment_id": data["id"],
            "status": data["status"],  # pending | waiting_for_capture | succeeded | canceled
            "amount": float(data["amount"]["value"]),
            "currency": data["amount"]["currency"],
            "metadata": data.get("metadata", {}),
        }
