from __future__ import annotations

import pytest

from services.api.breaker import CircuitBreaker, CircuitState


@pytest.mark.asyncio
async def test_breaker_stays_closed_below_threshold() -> None:
    breaker = CircuitBreaker("t", fail_threshold=3, open_seconds=0.1)
    for _ in range(2):
        assert await breaker.allow() is True
        await breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_breaker_opens_at_threshold() -> None:
    breaker = CircuitBreaker("t", fail_threshold=3, open_seconds=0.1)
    for _ in range(3):
        await breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert await breaker.allow() is False


@pytest.mark.asyncio
async def test_breaker_success_resets_failure_count() -> None:
    breaker = CircuitBreaker("t", fail_threshold=3, open_seconds=0.1)
    await breaker.record_failure()
    await breaker.record_failure()
    await breaker.record_success()
    await breaker.record_failure()
    await breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_breaker_half_opens_then_closes() -> None:
    breaker = CircuitBreaker("t", fail_threshold=1, open_seconds=0.05, half_open_probes=1)
    await breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert await breaker.allow() is False

    await __import__("asyncio").sleep(0.08)
    assert await breaker.allow() is True
    await breaker.record_success()
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_breaker_reopens_on_failed_probe() -> None:
    breaker = CircuitBreaker("t", fail_threshold=1, open_seconds=0.05, half_open_probes=1)
    await breaker.record_failure()
    await __import__("asyncio").sleep(0.08)
    assert await breaker.allow() is True
    await breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

