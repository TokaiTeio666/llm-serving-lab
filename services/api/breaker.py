from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


@dataclass
class BreakerSnapshot:
    name: str
    state: str
    consecutive_failures: int
    opened_at: float | None
    half_open_probes_inflight: int


class CircuitBreaker:
    """Three-state breaker: closed -> open -> half-open -> closed.

    Failures are consecutive. A single success in the closed state resets the
    counter, so a flapping upstream does not trip the breaker on isolated errors.
    """

    def __init__(
        self,
        name: str,
        *,
        fail_threshold: int = 5,
        open_seconds: float = 10.0,
        half_open_probes: int = 2,
    ) -> None:
        self.name = name
        self._fail_threshold = max(1, fail_threshold)
        self._open_seconds = open_seconds
        self._half_open_probes = max(1, half_open_probes)

        self._lock = asyncio.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at: float | None = None
        self._probes_inflight = 0
        self._trip_count = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def trip_count(self) -> int:
        return self._trip_count

    async def allow(self) -> bool:
        """Returns True when a request may be sent to the upstream."""
        async with self._lock:
            if self._state is CircuitState.CLOSED:
                return True

            if self._state is CircuitState.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0.0)
                if elapsed < self._open_seconds:
                    return False
                self._state = CircuitState.HALF_OPEN
                self._successes = 0
                self._probes_inflight = 0

            if self._probes_inflight >= self._half_open_probes:
                return False
            self._probes_inflight += 1
            return True

    async def record_success(self) -> None:
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._probes_inflight = max(0, self._probes_inflight - 1)
                self._successes += 1
                if self._successes >= self._half_open_probes:
                    self._state = CircuitState.CLOSED
                    self._failures = 0
                    self._successes = 0
                    self._probes_inflight = 0
                    self._opened_at = None
            else:
                self._failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._probes_inflight = max(0, self._probes_inflight - 1)
                self._trip()
                return
            self._failures += 1
            if self._failures >= self._fail_threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._failures = 0
        self._successes = 0
        self._probes_inflight = 0
        self._trip_count += 1

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            name=self.name,
            state=self._state.value,
            consecutive_failures=self._failures,
            opened_at=self._opened_at,
            half_open_probes_inflight=self._probes_inflight,
        )

