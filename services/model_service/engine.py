from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass
from typing import Iterator

_VOCAB = (
    "model latency throughput queue capacity inference token prefill decode batch "
    "service gateway cache routing failover replica saturation baseline p95 p99 "
    "timeout retry backoff jitter circuit breaker admission control sharding replica "
    "scheduler evict residency warmup compile graph kernel memory bandwidth "
    "concurrency streaming chunk delta aggregate histogram percentile saturation "
    "observability telemetry trace span cardinality rollout canary regression "
    "dataset annotation evaluation guardrail alignment throughput utilisation "
    "headroom autoscaler partition rebalance checkpoint recovery durability "
    "consistency replication quorum consensus shard partition leader follower "
    "quantisation precision speculative decoding speculative draft verification "
    "prefix reuse kv cache paged attention continuous batching chunked prefill"
).split()


@dataclass
class Completion:
    prompt_tokens: int
    completion_tokens: int
    text: str


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token, the usual rule of thumb."""
    return max(1, len(text) // 4)


def messages_text(messages: list[dict]) -> str:
    return "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)


class InferenceEngine:
    """Simulates a decoder-only LLM with a configurable latency profile.

    TTFT scales with prompt length (prefill), decode speed is fixed per token.
    Output is deterministic for a given prompt so response caching is testable.
    """

    def __init__(
        self,
        *,
        ttft_ms: int,
        tokens_per_sec: float,
        max_output_tokens: int,
        model_name: str,
    ) -> None:
        self.ttft_ms = ttft_ms
        self.tokens_per_sec = tokens_per_sec
        self.max_output_tokens = max_output_tokens
        self.model_name = model_name

    @property
    def per_token_delay(self) -> float:
        if self.tokens_per_sec <= 0:
            return 0.0
        return 1.0 / self.tokens_per_sec

    def prefill_delay(self, prompt_tokens: int) -> float:
        """Prefill is roughly linear in prompt length on top of a fixed floor."""
        base = self.ttft_ms / 1000.0
        return base + (prompt_tokens / 8000.0) * base

    async def pace(self, origin: float, index: int) -> None:
        """Sleep until the absolute deadline for token `index`.

        Sleeping a fixed interval per token accumulates scheduler jitter, so a
        120 tok/s model drifts well past 120 tok/s under load. Anchoring to an
        absolute schedule keeps the simulated decode speed honest.
        """
        deadline = origin + index * self.per_token_delay
        remaining = deadline - time.perf_counter()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def plan(self, prompt: str, max_tokens: int | None) -> tuple[int, int, list[str]]:
        prompt_tokens = estimate_tokens(prompt)
        budget = min(max_tokens or self.max_output_tokens, self.max_output_tokens)
        budget = max(1, budget)
        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12], 16)
        rng = random.Random(seed)
        tokens = [rng.choice(_VOCAB) for _ in range(budget)]
        return prompt_tokens, budget, tokens

    def completion(self, prompt: str, max_tokens: int | None) -> Completion:
        prompt_tokens, count, tokens = self.plan(prompt, max_tokens)
        text = " ".join(tokens)
        return Completion(prompt_tokens=prompt_tokens, completion_tokens=count, text=text)

    def token_iterator(self, prompt: str, max_tokens: int | None) -> Iterator[str]:
        _, _, tokens = self.plan(prompt, max_tokens)
        yield from tokens

    def created_timestamp(self) -> int:
        return int(time.time())

