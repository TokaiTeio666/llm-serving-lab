from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


@dataclass
class InferenceRequest:
    messages: list[dict[str, Any]]
    max_tokens: int | None
    temperature: float
    stream: bool


class BackendUnavailable(Exception):
    """The backend could not serve the request (network, unhealthy, timeout)."""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class Backend(Protocol):
    """OpenAI-compatible inference backend.

    Implementations return the wire format as-is: the blocking path returns a
    JSON body, the streaming path yields raw SSE bytes. The model service in
    front of them adds admission control, fault injection and metrics.
    """

    name: str
    model: str

    def estimate_prompt_tokens(self, messages: list[dict[str, Any]]) -> int: ...

    async def complete(self, request: InferenceRequest) -> dict[str, Any]: ...

    def stream(self, request: InferenceRequest) -> AsyncIterator[bytes]: ...

