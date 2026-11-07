from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Bucket:
    """Continuously refilling token bucket."""

    capacity: float
    refill_per_sec: float
    tokens: float = field(init=False)
    updated_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.updated_at = time.monotonic()

    def try_acquire(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    @property
    def available(self) -> float:
        return self.tokens


@dataclass
class RateLimitDecision:
    allowed: bool
    reason: str = ""
    retry_after_s: float = 0.0
    remaining_tokens: float = 0.0


class TenantRateLimiter:
    """Two-dimensional per-tenant limit: requests/minute and tokens/minute."""

    def __init__(self, *, rpm: int, tpm: int, burst: int) -> None:
        self._rpm = rpm
        self._tpm = tpm
        self._burst = burst
        self._lock = threading.Lock()
        self._tenants: dict[str, tuple[Bucket, Bucket]] = {}

    def _buckets_for(self, tenant: str) -> tuple[Bucket, Bucket]:
        with self._lock:
            pair = self._tenants.get(tenant)
            if pair is None:
                request_bucket = Bucket(capacity=self._burst, refill_per_sec=self._rpm / 60.0)
                token_capacity = max(1000.0, (self._tpm / 60.0) * 10.0)
                token_bucket = Bucket(capacity=token_capacity, refill_per_sec=self._tpm / 60.0)
                pair = (request_bucket, token_bucket)
                self._tenants[tenant] = pair
            return pair

    def check(self, tenant: str, tokens: int) -> RateLimitDecision:
        request_bucket, token_bucket = self._buckets_for(tenant)
        if not request_bucket.try_acquire(1.0):
            return RateLimitDecision(
                allowed=False,
                reason="request_rate",
                retry_after_s=60.0 / max(self._rpm, 1),
                remaining_tokens=token_bucket.available,
            )
        if not token_bucket.try_acquire(float(max(tokens, 1))):
            return RateLimitDecision(
                allowed=False,
                reason="token_rate",
                retry_after_s=60.0 / max(self._tpm, 1),
                remaining_tokens=token_bucket.available,
            )
        return RateLimitDecision(allowed=True, remaining_tokens=token_bucket.available)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                tenant: {
                    "requests_available": round(buckets[0].available, 2),
                    "tokens_available": round(buckets[1].available, 2),
                }
                for tenant, buckets in self._tenants.items()
            }


@dataclass
class AdmissionStats:
    peak_inflight: int = 0
    peak_queue: int = 0
    admitted: int = 0
    rejected_full: int = 0
    rejected_timeout: int = 0


class AdmissionController:
    """Bounded concurrency with a bounded wait queue.

    Rejecting early with 503 is deliberate: a queue that grows without bound
    converts a latency problem into a memory and timeout problem.
    """

    def __init__(self, *, max_inflight: int, max_queue: int) -> None:
        self.max_inflight = max_inflight
        self.max_queue = max_queue
        self._cond = asyncio.Condition()
        self._inflight = 0
        self._waiting = 0
        self._stats = AdmissionStats()

    async def acquire(self, timeout: float) -> None:
        """Raises OverloadedError when the queue is full or the wait times out."""
        from services.common.errors import OverloadedError

        deadline = time.monotonic() + timeout
        async with self._cond:
            while self._inflight >= self.max_inflight:
                if self._waiting >= self.max_queue:
                    self._stats.rejected_full += 1
                    raise OverloadedError(
                        f"admission queue is full ({self._waiting}/{self.max_queue})",
                        upstream="admission",
                    )
                self._waiting += 1
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._stats.rejected_timeout += 1
                        raise OverloadedError("timed out waiting for an inference slot")
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    self._stats.rejected_timeout += 1
                    raise OverloadedError("timed out waiting for an inference slot") from None
                finally:
                    self._waiting -= 1
            self._inflight += 1
            self._stats.admitted += 1
            self._stats.peak_inflight = max(self._stats.peak_inflight, self._inflight)
            self._stats.peak_queue = max(self._stats.peak_queue, self._waiting)

    async def release(self) -> None:
        # Decrement before any await: a cancellation must never leak a slot.
        self._inflight = max(0, self._inflight - 1)
        async with self._cond:
            self._cond.notify()

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def waiting(self) -> int:
        return self._waiting

    def snapshot(self) -> dict[str, int]:
        return {
            "inflight": self._inflight,
            "waiting": self._waiting,
            "peak_inflight": self._stats.peak_inflight,
            "peak_queue": self._stats.peak_queue,
            "admitted": self._stats.admitted,
            "rejected_full": self._stats.rejected_full,
            "rejected_timeout": self._stats.rejected_timeout,
        }

