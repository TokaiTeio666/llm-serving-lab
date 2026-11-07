from __future__ import annotations

import json

import httpx
import pytest

from services.model_service.backends.base import InferenceRequest
from services.model_service.backends.vllm import VLLMBackend, VLLMUnavailable

SSE_BODY = (
    b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":7,"completion_tokens":1}}\n\n'
    b"data: [DONE]\n\n"
)


def _request(stream: bool = False) -> InferenceRequest:
    return InferenceRequest(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=8,
        temperature=0.0,
        stream=stream,
    )


@pytest.mark.asyncio
async def test_complete_returns_vllm_body_untouched() -> None:
    body = {"id": "x", "object": "chat.completion", "usage": {"completion_tokens": 3}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["stream"] is False
        return httpx.Response(200, json=body)

    backend = VLLMBackend(
        base_url="http://vllm:8000",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = await backend.complete(_request())
    assert result == body
    await backend.aclose()


@pytest.mark.asyncio
async def test_stream_passes_sse_bytes_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=SSE_BODY, headers={"content-type": "text/event-stream"}
        )

    backend = VLLMBackend(
        base_url="http://vllm:8000",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    chunks = [chunk async for chunk in backend.stream(_request(stream=True))]
    assert b"".join(chunks) == SSE_BODY
    await backend.aclose()


@pytest.mark.asyncio
async def test_non_200_becomes_vllm_unavailable_with_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="queue is full")

    backend = VLLMBackend(
        base_url="http://vllm:8000",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VLLMUnavailable) as exc:
        await backend.complete(_request())
    assert exc.value.status_code == 503
    await backend.aclose()


@pytest.mark.asyncio
async def test_transport_error_becomes_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    backend = VLLMBackend(
        base_url="http://vllm:8000",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VLLMUnavailable) as exc:
        await backend.complete(_request())
    assert exc.value.status_code == 502
    await backend.aclose()


@pytest.mark.asyncio
async def test_stream_error_becomes_vllm_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="prompt too long")

    backend = VLLMBackend(
        base_url="http://vllm:8000",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VLLMUnavailable) as exc:
        async for _ in backend.stream(_request(stream=True)):
            pass
    assert exc.value.status_code == 400
    await backend.aclose()


@pytest.mark.asyncio
async def test_api_key_becomes_bearer_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    backend = VLLMBackend(
        base_url="http://vllm:8000",
        model="test-model",
        api_key="secret-token",
        transport=httpx.MockTransport(handler),
    )
    await backend.complete(_request())
    assert seen["authorization"] == "Bearer secret-token"
    await backend.aclose()

