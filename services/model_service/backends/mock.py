from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from services.model_service.backends.base import InferenceRequest
from services.model_service.engine import InferenceEngine, estimate_tokens, messages_text


class MockBackend:
    """Simulated decoder-only LLM with a configurable latency profile."""

    name = "mock"

    def __init__(self, engine: InferenceEngine) -> None:
        self.engine = engine
        self.model = engine.model_name

    def estimate_prompt_tokens(self, messages: list[dict[str, Any]]) -> int:
        return estimate_tokens(messages_text(messages))

    def _plan(self, request: InferenceRequest) -> tuple[int, int, list[str]]:
        prompt = messages_text(request.messages)
        return self.engine.plan(prompt, request.max_tokens)

    def _chunk(
        self,
        request_id: str,
        created: int,
        delta: dict[str, Any],
        finish_reason: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            payload["usage"] = usage
        return b"data: " + json.dumps(payload).encode() + b"\n\n"

    async def complete(self, request: InferenceRequest) -> dict[str, Any]:
        prompt_tokens, count, tokens = self._plan(request)
        await asyncio.sleep(self.engine.prefill_delay(prompt_tokens))
        origin = time.perf_counter()
        for index in range(len(tokens)):
            await self.engine.pace(origin, index + 1)
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": " ".join(tokens)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": count,
                "total_tokens": prompt_tokens + count,
            },
        }

    async def stream(self, request: InferenceRequest) -> AsyncIterator[bytes]:
        prompt_tokens, count, tokens = self._plan(request)
        request_id = "mock"
        created = int(time.time())
        await asyncio.sleep(self.engine.prefill_delay(prompt_tokens))
        yield self._chunk(request_id, created, {"role": "assistant", "content": ""})
        origin = time.perf_counter()
        for index, token in enumerate(tokens):
            await self.engine.pace(origin, index + 1)
            yield self._chunk(request_id, created, {"content": token})
        yield self._chunk(
            request_id,
            created,
            {},
            finish_reason="stop",
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": count,
                "total_tokens": prompt_tokens + count,
            },
        )
        yield b"data: [DONE]\n\n"

