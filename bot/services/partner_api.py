from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Literal, Optional

import httpx
import structlog
from pydantic import AliasChoices, BaseModel, Field, ValidationError

logger = structlog.get_logger()


# ── Pydantic response models ──────────────────────────────────────────


class Product(BaseModel):
    id: int
    name: str
    price: float = Field(ge=0, allow_inf_nan=False)
    in_stock: bool
    stock: int
    category: str = ""
    supplier: str = "thegodshop"
    description: str = ""
    price_usd: str | None = None
    usd_rub_rate: str | None = None
    direct_payment_supported: bool = True


class OrderResult(BaseModel):
    order_id: Annotated[int, Field(gt=0)] | Annotated[str, Field(min_length=1, max_length=128)]
    delivered_data: Optional[str] = None
    price: float = Field(ge=0, allow_inf_nan=False)


class Balance(BaseModel):
    balance: float = Field(ge=0, allow_inf_nan=False)
    discount_percent: float


class DepositResult(BaseModel):
    deposit_id: int
    pay_url: Optional[str] = None
    wallet: Optional[str] = None
    memo: Optional[str] = None
    amount_usdt: float = Field(gt=0, allow_inf_nan=False)
    amount_rub: float = Field(gt=0, allow_inf_nan=False)
    status: Optional[str] = None


class PartnerDepositStatus(BaseModel):
    deposit_id: int
    amount_rub: float = Field(
        gt=0, allow_inf_nan=False, validation_alias=AliasChoices("amount", "amount_rub")
    )
    status: str
    method: str
    amount_usdt: float | None = None


class ExternalOrder(BaseModel):
    order_id: int
    price: float
    status_field: str = "processing"


class ExternalStatus(BaseModel):
    order_id: int
    status: str  # processing | success | completed | failed | uncertain


# ── Exceptions ─────────────────────────────────────────────────────────


class PartnerAPIError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 0, *, outcome_unknown: bool = False):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.outcome_unknown = outcome_unknown
        super().__init__(f"[{code}] {message} (HTTP {http_status})")


# ── Rate limiter (token-bucket) ────────────────────────────────────────


class TokenBucketLimiter:
    """Simple async token-bucket rate limiter."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.tokens = float(rate)
        self.max_tokens = float(rate)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(
                    self.max_tokens, self.tokens + elapsed * self.rate
                )
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
            await asyncio.sleep(1.0 / self.rate)


# ── API Client ─────────────────────────────────────────────────────────


class PartnerAPIClient:
    MAX_RETRIES = 3
    RETRY_BACKOFF = [1.0, 2.0, 4.0]

    def __init__(self, base_url: str, api_key: str, rate_limit: int = 8) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._limiter = TokenBucketLimiter(rate_limit)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": self._api_key},
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Internal request machinery ───────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict[str, Any]:
        last_exc: Optional[Exception] = None

        mutation = method.upper() != "GET"
        retries = 1 if mutation else self.MAX_RETRIES
        for attempt in range(retries):
            await self._limiter.acquire()

            log = logger.bind(
                method=method,
                path=path,
                attempt=attempt + 1,
            )
            try:
                resp = await self._client.request(
                    method, path, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                log.warning("http_transport_error", error=type(exc).__name__)
                last_exc = PartnerAPIError(
                    "TRANSPORT_ERROR", "Нет ответа от поставщика", outcome_unknown=mutation,
                )
                if mutation:
                    raise last_exc from exc
                if attempt < retries - 1:
                    await asyncio.sleep(self.RETRY_BACKOFF[min(attempt, 2)])
                continue

            log.debug(
                "api_response",
                status_code=resp.status_code,
            )

            # Rate limited
            if resp.status_code == 429:
                delay = self.RETRY_BACKOFF[min(attempt, 2)]
                log.warning("rate_limited", retry_after=delay)
                last_exc = PartnerAPIError(
                    "RATE_LIMIT_EXCEEDED", "Rate limited", 429
                )
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                continue

            # Server errors (5xx) — one retry
            if resp.status_code >= 500:
                log.warning("server_error", status_code=resp.status_code)
                last_exc = PartnerAPIError(
                    "SERVER_ERROR",
                    f"Server returned {resp.status_code}",
                    resp.status_code,
                    outcome_unknown=mutation,
                )
                if not mutation and attempt < retries - 1:
                    await asyncio.sleep(1.0)
                    continue
                raise last_exc

            # Parse JSON
            try:
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("Expected object")
            except ValueError as exc:
                raise PartnerAPIError(
                    "INVALID_RESPONSE", "Некорректный ответ поставщика", resp.status_code,
                    outcome_unknown=mutation,
                ) from exc

            if data.get("status") == "error":
                raise PartnerAPIError(
                    code=data.get("code", "UNKNOWN"),
                    message=data.get("message", "Unknown error"),
                    http_status=resp.status_code,
                )

            if resp.is_error:
                raise PartnerAPIError("HTTP_ERROR", "Запрос отклонён поставщиком", resp.status_code)

            return data

        if last_exc:
            raise last_exc
        raise PartnerAPIError("MAX_RETRIES", "Max retries exceeded", 0)

    @staticmethod
    def _order_response(model, data: dict):
        try:
            return model(**data)
        except ValidationError as exc:
            raise PartnerAPIError(
                "INVALID_RESPONSE", "Поставщик не вернул данные созданного заказа",
                outcome_unknown=True,
            ) from exc

    # ── Catalog ──────────────────────────────────────────────────────

    async def get_products(self) -> list[Product]:
        data = await self._request("GET", "/api/v1/catalog/products")
        return [Product(**p) for p in data.get("products", [])]

    async def get_product(self, product_id: int) -> Product:
        data = await self._request("GET", f"/api/v1/catalog/product/{product_id}")
        product_data = data.get("product", data)
        return Product(**product_data)

    # ── Orders ───────────────────────────────────────────────────────

    async def create_order(self, product_id: int, qty: int = 1) -> OrderResult:
        data = await self._request(
            "POST",
            "/api/v1/order/create",
            json_body={"product_id": product_id, "qty": qty},
        )
        return self._order_response(OrderResult, data)

    async def get_order_status(self, order_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/order/status/{order_id}")

    # ── Account ──────────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/api/v1/account/balance")
        try:
            return Balance(**data)
        except ValidationError as exc:
            raise PartnerAPIError("INVALID_RESPONSE", "Не удалось проверить баланс поставщика") from exc

    async def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/api/v1/account/history", params={"limit": min(limit, 50)}
        )
        return data.get("history", [])

    # ── Deposits ─────────────────────────────────────────────────────

    async def deposit_crypto(self, amount_rub: float) -> DepositResult:
        data = await self._request(
            "POST",
            "/api/v1/account/deposit/crypto",
            json_body={"amount_rub": amount_rub},
        )
        return self._order_response(DepositResult, data)

    async def deposit_ton(self, amount_rub: float) -> DepositResult:
        data = await self._request(
            "POST",
            "/api/v1/account/deposit/ton",
            json_body={"amount_rub": amount_rub},
        )
        return self._order_response(DepositResult, data)

    async def get_deposit(self, deposit_id: int) -> PartnerDepositStatus:
        data = await self._request(
            "GET", f"/api/v1/account/deposit/{deposit_id}"
        )
        try:
            return PartnerDepositStatus(**data)
        except ValidationError as exc:
            raise PartnerAPIError("INVALID_RESPONSE", "Не удалось проверить платёж поставщика") from exc


    # ── Steam ────────────────────────────────────────────────────────

    async def buy_steam(self, login: str, amount_rub: float) -> ExternalOrder:
        data = await self._request(
            "POST",
            "/api/v1/steam/buy",
            json_body={"login": login, "amount_rub": amount_rub},
        )
        return self._order_response(ExternalOrder, data)

    # ── Games ────────────────────────────────────────────────────────

    async def buy_game(
        self, variation_id: int, fields: Optional[dict] = None
    ) -> ExternalOrder:
        data = await self._request(
            "POST",
            "/api/v1/games/buy",
            json_body={"variation_id": variation_id, "fields": fields or {}},
        )
        return self._order_response(ExternalOrder, data)

    # ── External status ──────────────────────────────────────────────

    async def get_external_status(self, order_id: int) -> ExternalStatus:
        data = await self._request(
            "GET", f"/api/v1/external/status/{order_id}"
        )
        return ExternalStatus(
            order_id=order_id,
            status=data.get("status", "processing"),
        )
