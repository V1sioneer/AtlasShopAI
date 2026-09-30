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
