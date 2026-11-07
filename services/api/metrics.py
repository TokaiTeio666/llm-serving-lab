from __future__ import annotations

from services.common.metrics import counter, gauge, histogram

REQUESTS = counter(
    "llm_api_requests_total",
    "Client requests handled by the gateway.",
    ["endpoint", "model", "tenant", "status", "stream"],
)

DURATION = histogram(
    "llm_api_request_duration_seconds",
    "End-to-end gateway request duration.",
    ["endpoint", "model", "stream"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120],
)

TTFT = histogram(
    "llm_api_time_to_first_token_seconds",
    "Time from request start to first token delivered to the client.",
    ["model", "stream"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
)

TPOT = histogram(
    "llm_api_time_per_output_token_seconds",
    "Inter-token latency: time between consecutive streamed tokens.",
    ["model"],
    buckets=[0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5],
)

TOKENS = counter(
    "llm_api_tokens_total",
    "Tokens observed by the gateway.",
    ["model", "direction"],
)

UPSTREAM_REQUESTS = counter(
    "llm_api_upstream_requests_total",
    "Requests the gateway sent to an upstream.",
    ["upstream", "outcome"],
)

UPSTREAM_DURATION = histogram(
    "llm_api_upstream_duration_seconds",
    "Latency of upstream calls as seen by the gateway.",
    ["upstream"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60],
)

UPSTREAM_RETRIES = counter(
    "llm_api_upstream_retry_total",
    "Retries issued against an upstream.",
    ["upstream", "reason"],
)

CIRCUIT_STATE = gauge(
    "llm_api_circuit_state",
    "Circuit breaker state: 0 closed, 1 half-open, 2 open.",
    ["upstream"],
)

INFLIGHT = gauge(
    "llm_api_inflight_requests",
    "Requests currently holding an admission slot.",
)

QUEUE_DEPTH = gauge(
    "llm_api_queue_depth",
    "Requests waiting for an admission slot.",
)

REJECTED = counter(
    "llm_api_rejected_total",
    "Requests rejected before reaching an upstream.",
    ["reason"],
)

CACHE = counter(
    "llm_api_cache_total",
    "Cache lookups by result.",
    ["result"],
)

FALLBACK = counter(
    "llm_api_fallback_total",
    "Requests served by the degraded upstream.",
    ["reason"],
)

QUEUE_WAIT = histogram(
    "llm_api_queue_wait_seconds",
    "Time spent waiting for an admission slot.",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

