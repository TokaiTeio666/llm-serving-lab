from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from services.api import app as gateway
from services.api import metrics as m


def _completion_body(model: str = "llm-fast") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


class FakeUpstream:
    """Stands in for the model services with programmable behaviour."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.primary_status = 200
        self.fallback_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if "8102" in str(request.url):
            status = self.fallback_status
        else:
            status = self.primary_status
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=_completion_body())


@pytest.fixture
async def client(monkeypatch):
    fake = FakeUpstream()
    async with gateway.app.router.lifespan_context(gateway.app):
        gateway.app.state.upstream.client = httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler)
        )
        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
            http.fake = fake  # type: ignore[attr-defined]
            yield http


PAYLOAD = {
    "messages": [{"role": "user", "content": "hello there"}],
    "max_tokens": 8,
    "stream": False,
}


@pytest.mark.asyncio
async def test_completion_returns_upstream_body(client) -> None:
    response = await client.post("/v1/chat/completions", json=PAYLOAD)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    assert response.headers["x-upstream"] == "primary"


@pytest.mark.asyncio
async def test_second_identical_request_is_served_from_cache(client) -> None:
    await client.post("/v1/chat/completions", json=PAYLOAD)
    calls_after_first = len(client.fake.calls)
    response = await client.post("/v1/chat/completions", json=PAYLOAD)
    assert response.headers["x-cache"] == "HIT"
    assert response.headers["x-upstream"] == "cache"
    assert len(client.fake.calls) == calls_after_first


@pytest.mark.asyncio
async def test_upstream_failure_falls_back_to_degraded_model(client) -> None:
    client.fake.primary_status = 500
    response = await client.post("/v1/chat/completions", json=PAYLOAD)
    assert response.status_code == 200
    assert response.headers["x-upstream"] == "fallback"
    assert response.headers["x-model"] == "llm-lite"


@pytest.mark.asyncio
async def test_both_upstreams_down_returns_503(client) -> None:
    client.fake.primary_status = 500
    client.fake.fallback_status = 500
    response = await client.post("/v1/chat/completions", json=PAYLOAD)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


@pytest.mark.asyncio
async def test_rate_limit_returns_429(client) -> None:
    limiter = client._transport.app.state.rate_limiter  # type: ignore[attr-defined]
    limiter._rpm = 1  # noqa: SLF001
    limiter._burst = 1  # noqa: SLF001
    limiter._tenants.clear()  # noqa: SLF001
    first = await client.post("/v1/chat/completions", json=PAYLOAD)
    second = await client.post("/v1/chat/completions", json=PAYLOAD)
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"]


@pytest.mark.asyncio
async def test_admission_control_rejects_when_saturated(client) -> None:
    admission = client._transport.app.state.admission  # type: ignore[attr-defined]
    await admission.acquire(0.1)
    admission.max_inflight = 0
    response = await client.post("/v1/chat/completions", json=PAYLOAD)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "overloaded"
    await admission.release()


@pytest.mark.asyncio
async def test_empty_messages_is_rejected(client) -> None:
    response = await client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_streaming_forwards_sse_and_terminates(client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    client.fake.handler = handler  # type: ignore[method-assign]
    client._transport.app.state.upstream.client = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )
    response = await client.post("/v1/chat/completions", json={**PAYLOAD, "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "data: [DONE]" in body
    assert '"content":"hi"' in body

