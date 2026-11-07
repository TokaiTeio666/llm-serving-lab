from __future__ import annotations

import asyncio

import pytest

from services.api.limiter import AdmissionController, TenantRateLimiter
from services.common.errors import OverloadedError


def test_token_bucket_refills_over_time() -> None:
    limiter = TenantRateLimiter(rpm=60, tpm=1000, burst=2)
    assert limiter.check("t", 10).allowed is True
    assert limiter.check("t", 10).allowed is True
    third = limiter.check("t", 10)
    assert third.allowed is False
    assert third.reason == "request_rate"


def test_tenants_are_isolated() -> None:
    limiter = TenantRateLimiter(rpm=60, tpm=1000, burst=1)
    assert limiter.check("a", 1).allowed is True
    assert limiter.check("a", 1).allowed is False
    assert limiter.check("b", 1).allowed is True


def test_token_limit_rejects_large_request() -> None:
    limiter = TenantRateLimiter(rpm=6000, tpm=100, burst=100)
    decision = limiter.check("t", 5000)
    assert decision.allowed is False
    assert decision.reason == "token_rate"


@pytest.mark.asyncio
async def test_admission_rejects_when_queue_is_full() -> None:
    controller = AdmissionController(max_inflight=1, max_queue=1)
    await controller.acquire(0.1)

    async def second() -> None:
        await controller.acquire(0.1)

    task = asyncio.create_task(second())
    await asyncio.sleep(0.02)
    with pytest.raises(OverloadedError):
        await controller.acquire(0.1)
    await controller.release()
    await task
    await controller.release()


@pytest.mark.asyncio
async def test_admission_times_out_waiting() -> None:
    controller = AdmissionController(max_inflight=1, max_queue=4)
    await controller.acquire(0.1)
    with pytest.raises(OverloadedError):
        await controller.acquire(0.05)
    await controller.release()


@pytest.mark.asyncio
async def test_admission_releases_slots() -> None:
    controller = AdmissionController(max_inflight=2, max_queue=4)
    await controller.acquire(0.1)
    await controller.acquire(0.1)
    assert controller.inflight == 2
    await controller.release()
    await controller.release()
    assert controller.inflight == 0

