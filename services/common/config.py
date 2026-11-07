from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    log_level: str = "INFO"
    log_json: bool = True


class APISettings(BaseServiceSettings):
    """Gateway configuration. Everything tunable lives here, env-driven."""

    service_name: str = "llm-api"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Upstream pool: primary is the "good" model, fallback is the degraded one.
    model_a_url: str = "http://127.0.0.1:8101"
    model_b_url: str = "http://127.0.0.1:8102"
    primary_model: str = Field(
        default="llm-fast",
        validation_alias=AliasChoices("API_PRIMARY_MODEL", "PRIMARY_MODEL"),
    )
    fallback_model: str = Field(
        default="llm-lite",
        validation_alias=AliasChoices("API_FALLBACK_MODEL", "FALLBACK_MODEL"),
    )

    # Timeout hierarchy. total bounds the whole upstream exchange including retries.
    upstream_connect_timeout: float = 2.0
    upstream_read_timeout: float = 30.0
    upstream_total_timeout: float = 60.0

    # Retries apply to non-streaming requests only: a partially streamed
    # response cannot be replayed without duplicating tokens on the wire.
    upstream_max_retries: int = 2
    upstream_retry_base_delay: float = 0.05
    upstream_retry_max_delay: float = 1.0

    # Admission control.
    max_inflight: int = 64
    max_queue: int = 256
    queue_timeout_s: float = 5.0

    # Per-tenant token bucket (requests/minute and tokens/minute).
    rate_limit_rpm: int = 120
    rate_limit_tpm: int = 20_000
    rate_limit_burst: int = 30

    # Circuit breaker.
    breaker_fail_threshold: int = 5
    breaker_open_seconds: float = 10.0
    breaker_half_open_probes: int = 2

    # Response cache (exact-match on normalised request, temperature-sensitive).
    cache_enabled: bool = True
    cache_max_entries: int = 512
    cache_ttl_s: int = 300
    cache_max_tokens: int = 512


class ModelServiceSettings(BaseServiceSettings):
    """Model service configuration: latency profile + fault injection."""

    model_name: str = "llm-fast"
    # Which inference backend sits behind the model service: "mock" (the
    # simulated engine) or "vllm" (a real vLLM OpenAI-compatible server).
    model_backend: str = "mock"
    model_ttft_ms: int = 120
    model_tokens_per_sec: float = 90.0
    model_preemption_ratio: float = 0.0
    model_max_output_tokens: int = 256
    model_fault_rate: float = 0.0
    model_max_concurrency: int = 64
    model_max_queue: int = 128
    model_queue_timeout_s: float = 10.0

    # Real vLLM backend.
    vllm_url: str = "http://127.0.0.1:8001"
    vllm_model: str = "Qwen/Qwen2.5-1.5B-Instruct"
    vllm_api_key: str = ""
    vllm_timeout_s: float = 300.0
    vllm_connect_timeout_s: float = 5.0
    vllm_max_model_len: int = 4096


class LoadSettings(BaseServiceSettings):
    """Load harness configuration."""

    load_base_url: str = "http://127.0.0.1:8000"
    load_duration_s: int = 30
    load_concurrency: int = 16
    load_warmup_s: int = 5
    load_stream: bool = True
    load_output: str = "reports/load-latest.json"
    load_ramp_s: int = 5
    load_tenant: str = "loadtest"
    load_prompt_tokens: int = 180
    load_max_tokens: int = 96
    load_timeout_s: float = 60.0


@lru_cache(maxsize=1)
def get_api_settings() -> APISettings:
    return APISettings()


@lru_cache(maxsize=1)
def get_model_settings() -> ModelServiceSettings:
    return ModelServiceSettings()


@lru_cache(maxsize=1)
def get_load_settings() -> LoadSettings:
    return LoadSettings()

