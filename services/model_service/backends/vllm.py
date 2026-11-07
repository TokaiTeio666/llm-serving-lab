from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx

from services.model_service.backends.base import BackendUnavailable, InferenceRequest

log = logging.getLogger("model_service.vllm")


class VLLMUnavailable(BackendUnavailable):
    """vLLM returned an error status."""


class VLLMBackend:
    """Talks to a real vLLM OpenAI-compatible server.

    vLLM already speaks the OpenAI SSE dialect, so the stream is passed
    through byte for byte. Re-serialising it would only add latency and
    risk corrupting the chunk framing.
    """

    name = "vllm"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 300.0,
        connect_timeout_s: float = 5.0,
        max_model_len: int = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_model_len = max_model_len
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout_s, read=timeout_s, write=timeout_s, pool=connect_timeout_s
            ),
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
            transport=transport,
        )

    def estimate_prompt_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough estimate only; vLLM reports the authoritative count in usage."""
        text = "\n".join(str(m.get("content", "")) for m in messages)
        return max(1, len(text) // 4)

    def _payload(self, request: InferenceRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "stream": request.stream,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def complete(self, request: InferenceRequest) -> dict[str, Any]:
        payload = {**self._payload(request), "stream": False}
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise VLLMUnavailable(f"vLLM timed out: {exc}", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise VLLMUnavailable(f"vLLM unreachable: {exc}", status_code=502) from exc

        if response.status_code != 200:
            raise VLLMUnavailable(
                f"vLLM returned HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise VLLMUnavailable("vLLM returned a non-JSON body", status_code=502) from exc

    async def stream(self, request: InferenceRequest) -> AsyncIterator[bytes]:
        payload = {**self._payload(request), "stream": True}
        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise VLLMUnavailable(
                        f"vLLM returned HTTP {response.status_code}: {body[:200]!r}",
                        status_code=response.status_code,
                    )
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.TimeoutException as exc:
            raise VLLMUnavailable(f"vLLM stream timed out: {exc}", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise VLLMUnavailable(f"vLLM stream failed: {exc}", status_code=502) from exc

    async def aclose(self) -> None:
        await self._client.aclose()

