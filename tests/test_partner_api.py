import httpx
import pytest

from bot.services.partner_api import PartnerAPIClient, PartnerAPIError


@pytest.mark.asyncio
async def test_mutating_request_is_not_retried_after_server_error():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"status": "error"})

    api = PartnerAPIClient("https://provider.invalid", "test-key")
    await api._client.aclose()
    api._client = httpx.AsyncClient(
        base_url="https://provider.invalid",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PartnerAPIError) as caught:
            await api.create_order(1, 1)
        assert caught.value.outcome_unknown is True
        assert calls == 1
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_partner_deposit_status_accepts_actual_amount_response():
    api = PartnerAPIClient("https://provider.invalid", "test-key")
    await api._client.aclose()
    api._client = httpx.AsyncClient(
        base_url="https://provider.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "deposit_id": 14, "amount": 100.0, "method": "crypto", "status": "paid",
            "created_at": "2026-10-01", "paid_at": "2026-10-01",
        })),
    )
    try:
        status = await api.get_deposit(14)
        assert status.amount_rub == 100 and status.status == "paid" and status.method == "crypto"
        assert status.amount_usdt is None
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_invalid_partner_payment_response_is_not_a_confirmed_payment():
    api = PartnerAPIClient("https://provider.invalid", "test-key")
    await api._client.aclose()
    api._client = httpx.AsyncClient(
        base_url="https://provider.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "deposit_id": 14, "amount": "nan", "method": "crypto", "status": "paid",
        })),
    )
    try:
        with pytest.raises(PartnerAPIError) as caught:
            await api.get_deposit(14)
        assert caught.value.code == "INVALID_RESPONSE"
    finally:
        await api.close()
