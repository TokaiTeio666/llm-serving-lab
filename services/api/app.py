from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from services.api import metrics as m
from services.api.cache import CacheEntry, TTLCache
from services.api.limiter import AdmissionController, TenantRateLimiter
from services.api.schemas import ChatRequest
from services.api.upstream import UpstreamClient
from services.common.config import APISettings, get_api_settings
from services.common.errors import ServiceError
from services.common.logging import request_id_var, setup_logging, tenant_var
from services.common.metrics import render_latest
from services.common.sse import SSEDecoder

log = logging.getLogger("gateway")

settings: APISettings = get_api_settings()
setup_logging(settings.log_level, settings.log_json)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    limits = httpx.Limits(
        max_connections=max(settings.max_inflight * 2, 32),
        max_keepalive_connections=max(settings.max_inflight, 16),
    )
    client = httpx.AsyncClient(limits=limits, http2=False)
    app.state.client = client
    app.state.upstream = UpstreamClient(settings, client)
    app.state.cache = TTLCache(
        max_entries=settings.cache_max_entries, ttl_s=settings.cache_ttl_s
    )
    app.state.rate_limiter = TenantRateLimiter(
        rpm=settings.rate_limit_rpm,
        tpm=settings.rate_limit_tpm,
        burst=settings.rate_limit_burst,
    )
    app.state.admission = AdmissionController(
        max_inflight=settings.max_inflight, max_queue=settings.max_queue
    )
    log.info(
        "gateway starting",
        extra={
            "primary": settings.model_a_url,
            "fallback": settings.model_b_url,
            "max_inflight": settings.max_inflight,
        },
    )
    try:
        yield
    finally:
        await client.aclose()
        log.info("gateway stopped")


app = FastAPI(title="llm-gateway", version="0.1.0", lifespan=lifespan)


def _error_body(exc: ServiceError, request_id: str) -> dict[str, Any]:
    return {
        "error": {
            "type": exc.error_type,
            "message": exc.message,
            "upstream": exc.upstream,
            "request_id": request_id,
        }
    }


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    tenant = request.headers.get("x-tenant-id") or "default"
    request_id_var.set(request_id)
    tenant_var.set(tenant)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = time.perf_counter() - started
        log.exception(
            "unhandled error",
            extra={"path": request.url.path, "duration_ms": round(elapsed * 1000, 2)},
        )
        raise
    elapsed = time.perf_counter() - started
    response.headers["x-request-id"] = request_id
    log.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(elapsed * 1000, 2),
        },
    )
    return response


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    upstream = request.app.state.upstream
    primary_ready = await _probe(upstream.primary.base_url)
    fallback_ready = await _probe(upstream.fallback.base_url)
    body = {
        "status": "ready" if primary_ready else "degraded",
        "primary_reachable": primary_ready,
        "fallback_reachable": fallback_ready,
        "admission": request.app.state.admission.snapshot(),
    }
    return JSONResponse(body, status_code=200 if primary_ready else 503)


async def _probe(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.0) as probe:
            response = await probe.get(f"{base_url.rstrip('/')}/healthz")
            return response.status_code == 200
    except Exception:
        return False


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    upstream = request.app.state.upstream
    return {
        "object": "list",
        "data": [
            {"id": upstream.primary.model, "owned_by": "primary", "tier": "standard"},
            {"id": upstream.fallback.model, "owned_by": "fallback", "tier": "degraded"},
        ],
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(
        content=render_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/admin/status")
async def admin_status(request: Request) -> dict[str, Any]:
    upstream = request.app.state.upstream
    admission = request.app.state.admission
    return {
        "breakers": [
            upstream.primary.breaker.snapshot().__dict__,
            upstream.fallback.breaker.snapshot().__dict__,
        ],
        "admission": admission.snapshot(),
        "cache": request.app.state.cache.stats(),
        "rate_limits": request.app.state.rate_limiter.snapshot(),
        "upstreams": {
            "primary": upstream.primary.base_url,
            "fallback": upstream.fallback.base_url,
        },
    }


@app.delete("/admin/cache")
async def clear_cache(request: Request) -> dict[str, Any]:
    cache: TTLCache = request.app.state.cache
    cache._entries.clear()  # noqa: SLF001 - deliberate admin operation
    return {"cleared": True, "stats": cache.stats()}


def _sse_error_event(exc: ServiceError, request_id: str) -> bytes:
    import json

    payload = json.dumps(
        {"error": {"type": exc.error_type, "message": exc.message, "request_id": request_id}}
    )
    return f"event: error\ndata: {payload}\n\n".encode()


def _usage_from_body(body: bytes) -> dict[str, int]:
    import json

    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {}
    usage = parsed.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
    }


def _pick_target(upstream: UpstreamClient, payload: ChatRequest):
    """Route on the requested model when the client names one explicitly."""
    if payload.model and payload.model == upstream.fallback.model:
        return upstream.fallback
    return upstream.primary


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, payload: ChatRequest) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    tenant = request.headers.get("x-tenant-id") or "default"
    request_id_var.set(request_id)
    tenant_var.set(tenant)

    started = time.perf_counter()
    stream_label = "true" if payload.stream else "false"
    upstream = request.app.state.upstream
    cache: TTLCache = request.app.state.cache
    admission: AdmissionController = request.app.state.admission
    limiter: TenantRateLimiter = request.app.state.rate_limiter

    if not payload.messages:
        m.REQUESTS.labels("chat", "-", tenant, "400", stream_label).inc()
        return JSONResponse(
            {"error": {"type": "invalid_request", "message": "messages is required"}},
            status_code=400,
        )

    prompt_text = "\n".join(msg.content for msg in payload.messages)
    prompt_tokens = estimate_tokens(prompt_text)
    requested_tokens = prompt_tokens + (payload.max_tokens or 0)

    # 1) Rate limit before doing any real work.
    decision = limiter.check(tenant, requested_tokens)
    if not decision.allowed:
        m.REJECTED.labels(reason=decision.reason).inc()
        m.REQUESTS.labels("chat", "-", tenant, "429", stream_label).inc()
        return JSONResponse(
            {
                "error": {
                    "type": "rate_limited",
                    "message": f"tenant '{tenant}' exceeded the {decision.reason} limit",
                    "request_id": request_id,
                }
            },
            status_code=429,
            headers={"Retry-After": str(max(1, int(decision.retry_after_s)))},
        )

    # 2) Cache lookup. A hit costs no upstream call and no admission slot.
    cacheable = (
        settings.cache_enabled
        and payload.temperature == 0
        and prompt_tokens <= settings.cache_max_tokens
    )
    cache_key = None
    if cacheable:
        cache_key = TTLCache.make_key(payload.cache_key_payload())
        entry = cache.get(cache_key)
        if entry is not None:
            m.CACHE.labels(result="hit").inc()
            m.REQUESTS.labels("chat", entry.model, tenant, "200", stream_label).inc()
            m.DURATION.labels("chat", entry.model, stream_label).observe(
                time.perf_counter() - started
            )
            m.TOKENS.labels(model=entry.model, direction="prompt").inc(entry.prompt_tokens)
            m.TOKENS.labels(model=entry.model, direction="completion").inc(
                entry.completion_tokens
            )
            return Response(
                content=entry.body,
                media_type=entry.content_type,
                headers={
                    "x-cache": "HIT",
                    "x-model": entry.model,
                    "x-upstream": "cache",
                },
            )
        m.CACHE.labels(result="miss").inc()

    # 3) Admission control: bound concurrency, bound the wait queue.
    queue_started = time.perf_counter()
    try:
        await admission.acquire(settings.queue_timeout_s)
    except ServiceError as exc:
        m.REJECTED.labels(reason="overloaded").inc()
        m.REQUESTS.labels("chat", "-", tenant, "503", stream_label).inc()
        return JSONResponse(
            _error_body(exc, request_id),
            status_code=503,
            headers={"Retry-After": "1"},
        )
    m.QUEUE_WAIT.observe(time.perf_counter() - queue_started)
    m.INFLIGHT.inc()
    m.QUEUE_DEPTH.set(admission.waiting)

    target = _pick_target(upstream, payload)
    context = {
        "request_id": request_id,
        "tenant": tenant,
        "started": started,
        "stream_label": stream_label,
        "cacheable": cacheable,
        "cache_key": cache_key,
        "prompt_tokens": prompt_tokens,
    }

    if payload.stream:
        return await _streaming_response(request, payload, target, context)

    try:
        return await _blocking_response(request, payload, target, context)
    finally:
        m.INFLIGHT.dec()
        m.QUEUE_DEPTH.set(admission.waiting)
        await admission.release()


async def _blocking_response(
    request: Request,
    payload: ChatRequest,
    target,
    context: dict[str, Any],
) -> Response:
    upstream: UpstreamClient = request.app.state.upstream
    cache: TTLCache = request.app.state.cache
    request_id = context["request_id"]
    tenant = context["tenant"]
    started = context["started"]
    stream_label = context["stream_label"]
    deadline = time.monotonic() + settings.upstream_total_timeout

    try:
        response = await upstream.complete(
            target,
            payload.upstream_payload(target.model),
            request_id=request_id,
            deadline=deadline,
        )
    except ServiceError as exc:
        log.warning(
            "upstream failed; trying fallback",
            extra={"error": exc.message, "type": exc.error_type, "upstream": target.name},
        )
        target = upstream.fallback
        try:
            response = await upstream.complete(
                target,
                payload.upstream_payload(target.model),
                request_id=request_id,
                deadline=deadline,
            )
            m.FALLBACK.labels(reason=exc.error_type).inc()
        except ServiceError as fallback_exc:
            m.REQUESTS.labels(
                "chat", target.model, tenant, str(fallback_exc.http_status), stream_label
            ).inc()
            m.DURATION.labels("chat", target.model, stream_label).observe(
                time.perf_counter() - started
            )
            return JSONResponse(
                _error_body(fallback_exc, request_id),
                status_code=fallback_exc.http_status,
                headers={"Retry-After": "1"},
            )

    body = response.content
    usage = _usage_from_body(body)
    prompt_tokens = usage.get("prompt_tokens") or context["prompt_tokens"]
    completion_tokens = usage.get("completion_tokens", 0)

    m.TOKENS.labels(model=target.model, direction="prompt").inc(prompt_tokens)
    m.TOKENS.labels(model=target.model, direction="completion").inc(completion_tokens)
    m.REQUESTS.labels(
        "chat", target.model, tenant, str(response.status_code), stream_label
    ).inc()
    m.DURATION.labels("chat", target.model, stream_label).observe(time.perf_counter() - started)

    if context["cacheable"] and response.status_code == 200:
        cache.put(
            context["cache_key"],
            CacheEntry(
                body=body,
                content_type="application/json",
                model=target.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )

    return Response(
        content=body,
        media_type="application/json",
        headers={
            "x-cache": "MISS" if context["cacheable"] else "BYPASS",
            "x-model": target.model,
            "x-upstream": target.name,
        },
    )


async def _streaming_response(
    request: Request,
    payload: ChatRequest,
    target,
    context: dict[str, Any],
) -> Response:
    from fastapi.responses import StreamingResponse

    upstream: UpstreamClient = request.app.state.upstream
    cache: TTLCache = request.app.state.cache
    admission: AdmissionController = request.app.state.admission
    request_id = context["request_id"]
    tenant = context["tenant"]
    started = context["started"]
    stream_label = context["stream_label"]

    decoder = SSEDecoder()
    stats: dict[str, Any] = {
        "first_token_at": None,
        "last_chunk_at": None,
        "content_chunks": 0,
    }
    outcome: dict[str, Any] = {"status": "200", "target": target}

    def observe(active, chunk: bytes, now: float) -> None:
        if stats["first_token_at"] is None:
            stats["first_token_at"] = now
            m.TTFT.labels(active.model, "true").observe(now - started)
        elif stats["last_chunk_at"] is not None:
            m.TPOT.labels(active.model).observe(now - stats["last_chunk_at"])
        stats["last_chunk_at"] = now
        for event in decoder.feed(chunk):
            data = event.json_data
            if not isinstance(data, dict):
                continue
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    stats["content_chunks"] += 1

    # Prefetch the first upstream chunk before committing to a response.
    # Until this returns we have not sent a single byte, so the routing
    # decision (primary vs fallback) is still ours to make, and the status
    # code and x-upstream header can both reflect what actually happened.
    active = target
    generator = upstream.stream(
        active,
        payload.upstream_payload(active.model),
        request_id=request_id,
    )
    try:
        try:
            first_chunk = await anext(generator)
        except StopAsyncIteration:
            first_chunk = None
        except ServiceError as exc:
            log.warning(
                "upstream stream failed before first byte; trying fallback",
                extra={
                    "error": exc.message,
                    "type": exc.error_type,
                    "upstream": active.name,
                },
            )
            active = upstream.fallback
            outcome["target"] = active
            generator = upstream.stream(
                active,
                payload.upstream_payload(active.model),
                request_id=request_id,
            )
            try:
                first_chunk = await anext(generator)
            except StopAsyncIteration:
                first_chunk = None
            except ServiceError as fallback_exc:
                m.REQUESTS.labels(
                    "chat", active.model, tenant, str(fallback_exc.http_status), stream_label
                ).inc()
                m.DURATION.labels("chat", active.model, stream_label).observe(
                    time.perf_counter() - started
                )
                m.FALLBACK.labels(reason=exc.error_type).inc()
                m.INFLIGHT.dec()
                m.QUEUE_DEPTH.set(admission.waiting)
                await admission.release()
                return JSONResponse(
                    _error_body(fallback_exc, request_id),
                    status_code=fallback_exc.http_status,
                    headers={"Retry-After": "1"},
                )
            m.FALLBACK.labels(reason=exc.error_type).inc()
    except Exception:
        # Never leak an admission slot, whatever went wrong above.
        m.INFLIGHT.dec()
        m.QUEUE_DEPTH.set(admission.waiting)
        await admission.release()
        raise

    if first_chunk is not None:
        observe(active, first_chunk, time.perf_counter())

    if first_chunk is None:
        m.REQUESTS.labels("chat", active.model, tenant, "502", stream_label).inc()
        m.DURATION.labels("chat", active.model, stream_label).observe(
            time.perf_counter() - started
        )
        m.INFLIGHT.dec()
        m.QUEUE_DEPTH.set(admission.waiting)
        await admission.release()
        return JSONResponse(
            {
                "error": {
                    "type": "empty_upstream_response",
                    "message": "upstream closed the stream without sending data",
                    "request_id": request_id,
                }
            },
            status_code=502,
        )

    async def event_stream() -> AsyncIterator[bytes]:
        buffer = bytearray()
        try:
            buffer.extend(first_chunk)
            yield first_chunk
            try:
                async for chunk in generator:
                    observe(active, chunk, time.perf_counter())
                    buffer.extend(chunk)
                    yield chunk
            except ServiceError as exc:
                # Bytes are already on the wire: we cannot restart without
                # duplicating output, so we surface the failure in-band.
                log.warning(
                    "stream broke after first byte",
                    extra={"error": exc.message, "type": exc.error_type},
                )
                yield _sse_error_event(exc, request_id)
                outcome["status"] = "502"
            else:
                outcome["status"] = "200"
        finally:
            await _finalise_stream(
                request=request,
                context=context,
                outcome=outcome,
                stats=stats,
                buffer=buffer,
                admission=admission,
                cache=cache,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "x-model": active.model,
            "x-upstream": active.name,
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
    )


async def _finalise_stream(
    *,
    request: Request,
    context: dict[str, Any],
    outcome: dict[str, Any],
    stats: dict[str, Any],
    buffer: bytearray,
    admission: AdmissionController,
    cache: TTLCache,
) -> None:
    active = outcome["target"]
    tenant = context["tenant"]
    started = context["started"]
    stream_label = context["stream_label"]

    try:
        completion_tokens = stats["content_chunks"]
        m.TOKENS.labels(model=active.model, direction="prompt").inc(context["prompt_tokens"])
        m.TOKENS.labels(model=active.model, direction="completion").inc(completion_tokens)
        m.REQUESTS.labels("chat", active.model, tenant, outcome["status"], stream_label).inc()
        m.DURATION.labels("chat", active.model, stream_label).observe(
            time.perf_counter() - started
        )

        if context["cacheable"] and outcome["status"] == "200" and buffer:
            cache.put(
                context["cache_key"],
                CacheEntry(
                    body=bytes(buffer),
                    content_type="text/event-stream",
                    model=active.model,
                    prompt_tokens=context["prompt_tokens"],
                    completion_tokens=completion_tokens,
                ),
            )
    finally:
        m.INFLIGHT.dec()
        m.QUEUE_DEPTH.set(admission.waiting)
        await admission.release()

