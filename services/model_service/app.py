from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.common.config import get_model_settings
from services.common.logging import request_id_var, setup_logging
from services.common.metrics import render_latest
from services.common.sse import SSEDecoder
from services.model_service import metrics as m
from services.model_service.backends import (
    Backend,
    BackendUnavailable,
    InferenceRequest,
    MockBackend,
    VLLMBackend,
)
from services.model_service.engine import InferenceEngine
from services.model_service.faults import VALID_MODES, FaultConfig, FaultInjector

settings = get_model_settings()
setup_logging(settings.log_level, settings.log_json)


def _raise_timer_resolution() -> None:
    """Windows defaults to a 15.6ms timer, which would inflate per-token delays.

    The simulated decode speed is ~8ms per token, so the local numbers would
    otherwise be roughly half the Docker numbers for the same settings.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # pragma: no cover - best effort
        pass


_raise_timer_resolution()

def build_backend() -> Backend:
    """Pick the inference backend. The mock stays the default so tests and CI
    never need a GPU; set MODEL_BACKEND=vllm to point at a real server."""
    if settings.model_backend.lower() == "vllm":
        return VLLMBackend(
            base_url=settings.vllm_url,
            model=settings.vllm_model,
            api_key=settings.vllm_api_key,
            timeout_s=settings.vllm_timeout_s,
            connect_timeout_s=settings.vllm_connect_timeout_s,
            max_model_len=settings.vllm_max_model_len,
        )
    return MockBackend(
        InferenceEngine(
            ttft_ms=settings.model_ttft_ms,
            tokens_per_sec=settings.model_tokens_per_sec,
            max_output_tokens=settings.model_max_output_tokens,
            model_name=settings.model_name,
        )
    )


backend = build_backend()

injector = FaultInjector()


class ChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    max_tokens: int | None = None
    temperature: float = 0.0
    stream: bool = False


class FaultRequest(BaseModel):
    mode: str = "none"
    rate: float = 0.0
    duration_s: float = 30.0
    status_code: int = 500
    slow_extra_ms: int = 2000
    hang_s: float = 120.0


class EngineConfig(BaseModel):
    ttft_ms: int
    tokens_per_sec: float
    max_output_tokens: int
    model: str


class Slot:
    """Admission control mirroring a real inference server's scheduler."""

    def __init__(self, max_concurrency: int, max_queue: int, queue_timeout: float) -> None:
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout
        self._inflight = 0
        self._waiting = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> bool:
        deadline = time.monotonic() + self.queue_timeout
        async with self._cond:
            while self._inflight >= self.max_concurrency:
                if self._waiting >= self.max_queue:
                    return False
                self._waiting += 1
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    return False
                finally:
                    self._waiting -= 1
            self._inflight += 1
            return True

    async def release(self) -> None:
        # Decrement before any await: a cancellation must never leak a slot.
        self._inflight = max(0, self._inflight - 1)
        async with self._cond:
            self._cond.notify()

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def inflight(self) -> int:
        return self._inflight


slot = Slot(settings.model_max_concurrency, settings.model_max_queue, settings.model_queue_timeout_s)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(title="model-service", version="0.1.0", lifespan=lifespan)


async def _apply_fault(mode: str) -> Response | None:
    """Returns a Response when the request must fail, otherwise None."""
    config = injector.config
    if mode == "http_500":
        m.FAULTS_INJECTED.labels(mode=mode).inc()
        return JSONResponse({"error": {"message": "injected upstream failure"}}, status_code=config.status_code)
    if mode == "http_503":
        m.FAULTS_INJECTED.labels(mode=mode).inc()
        return JSONResponse({"error": {"message": "injected upstream overload"}}, status_code=503)
    if mode == "empty":
        m.FAULTS_INJECTED.labels(mode=mode).inc()
        return Response(status_code=200, content=b"", media_type="text/event-stream")
    if mode == "timeout":
        m.FAULTS_INJECTED.labels(mode=mode).inc()
        try:
            await asyncio.sleep(config.hang_s)
        except asyncio.CancelledError:
            raise
        return JSONResponse({"error": {"message": "late"}}, status_code=504)
    if mode == "slow":
        m.FAULTS_INJECTED.labels(mode=mode).inc()
        await asyncio.sleep(config.slow_extra_ms / 1000.0)
    return None


@app.post("/v1/chat/completions")
async def chat_completions(payload: ChatRequest, request: Request) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request_id_var.set(request_id)
    started = time.perf_counter()
    stream_label = str(payload.stream).lower()

    if not payload.messages:
        return JSONResponse({"error": {"message": "messages is required"}}, status_code=400)

    fault = injector.maybe_fault()
    if fault is not None:
        early = await _apply_fault(fault)
        if early is not None:
            m.REQUESTS.labels(model=settings.model_name, status=str(early.status_code), stream=stream_label).inc()
            return early

    if not await slot.acquire():
        m.REJECTED.labels(model=settings.model_name, reason="queue_full").inc()
        m.REQUESTS.labels(model=settings.model_name, status="503", stream=stream_label).inc()
        return JSONResponse(
            {"error": {"message": "model server queue is full", "type": "server_overloaded"}},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    m.ACTIVE.labels(model=settings.model_name).inc()
    try:
        messages = [msg.model_dump() for msg in payload.messages]
        prompt_tokens = backend.estimate_prompt_tokens(messages)
        m.TOKENS.labels(model=settings.model_name, direction="prompt").inc(prompt_tokens)
        inference = InferenceRequest(
            messages=messages,
            max_tokens=payload.max_tokens,
            temperature=payload.temperature,
            stream=payload.stream,
        )

        if payload.stream:
            return await _stream_response(
                request_id=request_id,
                inference=inference,
                started=started,
            )
        return await _blocking_response(
            request_id=request_id,
            prompt_tokens=prompt_tokens,
            inference=inference,
            started=started,
        )
    finally:
        m.ACTIVE.labels(model=settings.model_name).dec()
        await slot.release()


async def _blocking_response(
    *, request_id: str, prompt_tokens: int, inference: InferenceRequest, started: float
) -> Response:
    try:
        body = await backend.complete(inference)
    except BackendUnavailable as exc:
        m.REQUESTS.labels(model=settings.model_name, status=str(exc.status_code), stream="false").inc()
        return JSONResponse(
            {"error": {"message": exc.message, "type": "backend_unavailable"}},
            status_code=exc.status_code,
        )

    usage = body.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens", 0))
    m.TOKENS.labels(model=settings.model_name, direction="completion").inc(completion_tokens)
    m.TTFT.labels(model=settings.model_name).observe(time.perf_counter() - started)
    m.DURATION.labels(model=settings.model_name, stream="false").observe(time.perf_counter() - started)
    m.REQUESTS.labels(model=settings.model_name, status="200", stream="false").inc()
    return JSONResponse(body)


async def _stream_response(
    *, request_id: str, inference: InferenceRequest, started: float
) -> Response:
    decoder = SSEDecoder()
    state: dict[str, Any] = {
        "content_chunks": 0,
        "completion_tokens": 0,
        "first_token_at": None,
        "status": "200",
    }

    async def generator() -> AsyncIterator[bytes]:
        nonlocal state
        try:
            async for chunk in backend.stream(inference):
                now = time.perf_counter()
                if state["first_token_at"] is None:
                    state["first_token_at"] = now
                for event in decoder.feed(chunk):
                    data = event.json_data
                    if not isinstance(data, dict):
                        continue
                    if data.get("usage"):
                        state["completion_tokens"] = int(
                            (data.get("usage") or {}).get(
                                "completion_tokens", state["completion_tokens"]
                            )
                        )
                    for choice in data.get("choices") or []:
                        if (choice.get("delta") or {}).get("content"):
                            state["content_chunks"] += 1
                yield chunk
        except BackendUnavailable as exc:
            state["status"] = str(exc.status_code)
            payload = json.dumps({"error": {"message": exc.message, "type": "backend_unavailable"}})
            yield f"event: error\ndata: {payload}\n\n".encode()
        except asyncio.CancelledError:
            state["status"] = "499"
            raise
        except Exception:
            state["status"] = "500"
            raise

    async def guarded() -> AsyncIterator[bytes]:
        try:
            async for chunk in generator():
                yield chunk
        finally:
            first_token_at = state["first_token_at"]
            if first_token_at is not None:
                m.TTFT.labels(model=settings.model_name).observe(first_token_at - started)
            m.TOKENS.labels(model=settings.model_name, direction="completion").inc(
                state["completion_tokens"] or state["content_chunks"]
            )
            m.DURATION.labels(model=settings.model_name, stream="true").observe(time.perf_counter() - started)
            m.REQUESTS.labels(
                model=settings.model_name, status=state["status"], stream="true"
            ).inc()

    return StreamingResponse(guarded(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "model": settings.model_name, "backend": backend.name}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    """Readiness: the mock is always ready, vLLM has to answer."""
    if backend.name != "vllm":
        return {
            "status": "ready",
            "model": settings.model_name,
            "backend": backend.name,
            "queue_depth": slot.waiting,
        }
    try:
        async with httpx.AsyncClient(timeout=3.0) as probe:
            response = await probe.get(f"{settings.vllm_url.rstrip('/')}/health")
        reachable = response.status_code == 200
    except Exception:
        reachable = False
    return JSONResponse(
        {
            "status": "ready" if reachable else "degraded",
            "model": settings.model_name,
            "backend": backend.name,
            "vllm_reachable": reachable,
            "queue_depth": slot.waiting,
        },
        status_code=200 if reachable else 503,
    )


@app.get("/admin/config")
async def get_config() -> dict[str, Any]:
    return {
        "model": settings.model_name,
        "backend": backend.name,
        "vllm_url": settings.vllm_url if backend.name == "vllm" else None,
        "vllm_model": settings.vllm_model if backend.name == "vllm" else None,
        "faults": injector.stats(),
    }


@app.post("/admin/config")
async def set_config(body: EngineConfig) -> dict[str, Any]:
    """Tune the simulated engine. Has no effect on the vLLM backend."""
    if isinstance(backend, MockBackend):
        backend.engine.ttft_ms = body.ttft_ms
        backend.engine.tokens_per_sec = body.tokens_per_sec
        backend.engine.max_output_tokens = body.max_output_tokens
    return await get_config()


@app.post("/admin/faults")
async def set_faults(body: FaultRequest) -> dict[str, Any]:
    if body.mode not in VALID_MODES:
        return JSONResponse({"error": f"mode must be one of {list(VALID_MODES)}"}, status_code=400)
    injector.configure(
        FaultConfig(
            mode=body.mode,
            rate=body.rate,
            duration_s=body.duration_s,
            status_code=body.status_code,
            slow_extra_ms=body.slow_extra_ms,
            hang_s=body.hang_s,
        )
    )
    return injector.stats()


@app.delete("/admin/faults")
async def clear_faults() -> dict[str, Any]:
    injector.reset()
    return injector.stats()


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=render_latest(), media_type="text/plain; version=0.0.4; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("MODEL_PORT", "8101")),
        log_config=None,
        access_log=False,
    )

