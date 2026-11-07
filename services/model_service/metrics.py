from __future__ import annotations

from services.common.metrics import counter, gauge, histogram

REQUESTS = counter(
    "model_requests_total",
    "Requests handled by the model service.",
    ["model", "status", "stream"],
)

DURATION = histogram(
    "model_request_duration_seconds",
    "End-to-end duration of a model service request.",
    ["model", "stream"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60],
)

TTFT = histogram(
    "model_time_to_first_token_seconds",
    "Time from request acceptance to first streamed token.",
    ["model"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)

TOKENS = counter(
    "model_tokens_total",
    "Tokens produced by the model service.",
    ["model", "direction"],
)

ACTIVE = gauge(
    "model_active_requests",
    "Requests currently being served.",
    ["model"],
)

QUEUE_DEPTH = gauge(
    "model_queue_depth",
    "Requests waiting for an inference slot.",
    ["model"],
)

FAULTS_INJECTED = counter(
    "model_faults_injected_total",
    "Faults injected by the chaos controller.",
    ["mode"],
)

REJECTED = counter(
    "model_rejected_total",
    "Requests rejected before inference.",
    ["model", "reason"],
)

