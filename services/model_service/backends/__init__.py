"""Pluggable inference backends: the simulated engine or a real vLLM server."""

from services.model_service.backends.base import Backend, BackendUnavailable, InferenceRequest
from services.model_service.backends.mock import MockBackend
from services.model_service.backends.vllm import VLLMBackend, VLLMUnavailable

__all__ = [
    "Backend",
    "BackendUnavailable",
    "InferenceRequest",
    "MockBackend",
    "VLLMBackend",
    "VLLMUnavailable",
]

