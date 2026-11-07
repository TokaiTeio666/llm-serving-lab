from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from services.api import metrics as m
from services.api.breaker import CircuitBreaker, CircuitState
from services.common.errors import CircuitOpenError, UpstreamError, UpstreamTimeout

log = logging.getLogger("gateway.upstream")

# Statuses that mean "the upstream cannot serve right now" -> trip the breaker.
DEGRADED_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

CIRCUIT_STATE_VALUE = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}


@dataclass
class UpstreamTarget:
    name: str
    base_url: str
    model: str
    breaker: CircuitBreaker

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"


class UpstreamClient:
    """Talks to the model services: timeouts, retries and breaker accounting."""

    def __init__(self, settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        breaker_kwargs = dict(
            fail_threshold=settings.breaker_fail_threshold,
            open_seconds=settings.breaker_open_seconds,
            half_open_probes=settings.breaker_half_open_probes,
        )
        self.primary = UpstreamTarget(
            name="primary",
            base_url=settings.model_a_url,
            model=settings.primary_model,
            breaker=CircuitBreaker("primary", **breaker_kwargs),
        )
        self.fallback = UpstreamTarget(
            name="fallback",
            base_url=settings.model_b_url,
            model=settings.fallback_model,
            breaker=CircuitBreaker("fallback", **breaker_kwargs),
        )
        self.sync_breaker_gauge(self.primary)
        self.sync_breaker_gauge(self.fallback)

    def _timeout(self) -> httpx.Timeout:
        s = self.settings
        return httpx.Timeout(
            connect=s.upstream_connect_timeout,
            read=s.upstream_read_timeout,
            write=s.upstream_read_timeout,
            pool=s.queue_timeout_s,
        )

    def _backoff(self, attempt: int) -> float:
        s = self.settings
        ceiling = min(s.upstream_retry_base_delay * (2**attempt), s.upstream_retry_max_delay)
        return random.uniform(ceiling * 0.5, ceiling)

    def sync_breaker_gauge(self, target: UpstreamTarget) -> None:
        m.CIRCUIT_STATE.labels(upstream=target.name).set(
            CIRCUIT_STATE_VALUE[target.breaker.state]
        )

    async def record_success(self, target: UpstreamTarget) -> None:
        await target.breaker.record_success()
        self.sync_breaker_gauge(target)

    async def record_failure(self, target: UpstreamTarget) -> None:
        await target.breaker.record_failure()
        self.sync_breaker_gauge(target)

    async def complete(
        self,
        target: UpstreamTarget,
        payload: dict[str, Any],
        *,
        request_id: str,
        deadline: float,
    ) -> httpx.Response:
        """Non-streaming upstream call with retries. Streaming is not retryable."""
        attempts = self.settings.upstream_max_retries + 1
        last_error: Exception | None = None

        # The breaker observes one client request, not one attempt. A request
        # that exhausts its retries counts once against the threshold.
        if not await target.breaker.allow():
            m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="circuit_open").inc()
            raise CircuitOpenError(f"circuit for {target.name} is open", upstream=target.name)
        self.sync_breaker_gauge(target)

        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamTimeout(
                    f"{target.name} exhausted the request deadline", upstream=target.name
                )

            started = time.perf_counter()
            try:
                response = await self.client.post(
                    target.chat_url,
                    json=payload,
                    headers={"x-request-id": request_id},
                    timeout=self._timeout(),
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="timeout").inc()
                if attempt + 1 >= attempts:
                    break
                m.UPSTREAM_RETRIES.labels(upstream=target.name, reason="timeout").inc()
                await asyncio.sleep(self._backoff(attempt))
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="transport_error").inc()
                if attempt + 1 >= attempts:
                    break
                m.UPSTREAM_RETRIES.labels(upstream=target.name, reason="transport_error").inc()
                await asyncio.sleep(self._backoff(attempt))
                continue

            m.UPSTREAM_DURATION.labels(upstream=target.name).observe(time.perf_counter() - started)

            degraded = response.status_code >= 500 or response.status_code in DEGRADED_STATUSES
            if degraded:
                m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="upstream_error").inc()
                if attempt + 1 >= attempts:
                    break
                reason = f"http_{response.status_code}"
                m.UPSTREAM_RETRIES.labels(upstream=target.name, reason=reason).inc()
                await asyncio.sleep(self._backoff(attempt))
                continue

            m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="ok").inc()
            await self.record_success(target)
            return response

        await self.record_failure(target)
        if isinstance(last_error, httpx.TimeoutException):
            raise UpstreamTimeout(
                f"{target.name} timed out after {attempts} attempt(s)", upstream=target.name
            ) from last_error
        raise UpstreamError(
            f"{target.name} failed after {attempts} attempt(s)", upstream=target.name
        ) from last_error

    async def stream(
        self,
        target: UpstreamTarget,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        """Streaming upstream call. Never retried: bytes may already be on the wire."""
        if not await target.breaker.allow():
            m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="circuit_open").inc()
            raise CircuitOpenError(
                f"circuit for {target.name} is open", upstream=target.name
            )
        self.sync_breaker_gauge(target)

        started = time.perf_counter()
        try:
            async with self.client.stream(
                "POST",
                target.chat_url,
                json=payload,
                headers={"x-request-id": request_id},
                timeout=self._timeout(),
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    outcome = "upstream_error" if response.status_code >= 500 else "client_error"
                    m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome=outcome).inc()
                    if response.status_code in DEGRADED_STATUSES:
                        await self.record_failure(target)
                    else:
                        await self.record_success(target)
                    raise UpstreamError(
                        f"{target.name} returned HTTP {response.status_code}", upstream=target.name
                    )

                m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="ok").inc()
                async for chunk in response.aiter_bytes():
                    yield chunk
                m.UPSTREAM_DURATION.labels(upstream=target.name).observe(
                    time.perf_counter() - started
                )
                # Only a fully delivered stream counts as a success: a stream
                # that dies halfway through is a failed request.
                await self.record_success(target)
        except UpstreamError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            m.UPSTREAM_REQUESTS.labels(upstream=target.name, outcome="stream_broken").inc()
            await self.record_failure(target)
            raise UpstreamError(f"{target.name} stream failed", upstream=target.name) from exc
        except Exception as exc:
            await self.record_failure(target)
            raise UpstreamError(f"{target.name} stream failed", upstream=target.name) from exc

