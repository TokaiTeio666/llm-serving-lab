from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Literal

FaultMode = Literal[
    "none",
    "http_500",
    "http_503",
    "timeout",
    "slow",
    "malformed_sse",
    "empty",
]

VALID_MODES: tuple[str, ...] = (
    "none",
    "http_500",
    "http_503",
    "timeout",
    "slow",
    "malformed_sse",
    "empty",
)


@dataclass
class FaultConfig:
    mode: FaultMode = "none"
    rate: float = 0.0
    duration_s: float = 30.0
    status_code: int = 500
    slow_extra_ms: int = 2000
    hang_s: float = 120.0


@dataclass
class FaultInjector:
    """Applies a probabilistic fault to incoming requests for a bounded window.

    This is what turns the load test from "the happy path is fast" into
    "the gateway degrades correctly when the model service misbehaves".
    """

    _config: FaultConfig = field(default_factory=FaultConfig)
    _started_at: float | None = None
    _injected: dict[str, int] = field(default_factory=dict)

    @property
    def config(self) -> FaultConfig:
        return self._config

    def configure(self, config: FaultConfig) -> FaultConfig:
        self._config = config
        self._started_at = time.monotonic() if config.mode != "none" else None
        self._injected.setdefault(config.mode, 0)
        return self._config

    def reset(self) -> None:
        self._config = FaultConfig()
        self._started_at = None
        self._injected = {}

    def active(self) -> bool:
        if self._config.mode == "none" or self._started_at is None:
            return False
        return (time.monotonic() - self._started_at) < self._config.duration_s

    def remaining_s(self) -> float:
        if self._started_at is None or self._config.mode == "none":
            return 0.0
        return max(0.0, self._config.duration_s - (time.monotonic() - self._started_at))

    def maybe_fault(self) -> FaultMode | None:
        if not self.active() or self._config.rate <= 0:
            return None
        if random.random() > self._config.rate:
            return None
        mode = self._config.mode
        self._injected[mode] = self._injected.get(mode, 0) + 1
        return mode

    def stats(self) -> dict:
        return {
            "mode": self._config.mode,
            "rate": self._config.rate,
            "duration_s": self._config.duration_s,
            "active": self.active(),
            "remaining_s": round(self.remaining_s(), 2),
            "injected": dict(self._injected),
        }

