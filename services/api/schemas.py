from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    max_tokens: int | None = Field(default=256, ge=1, le=4096)
    temperature: float = 0.0
    stream: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    def cache_key_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [m.model_dump() for m in self.messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

    def upstream_payload(self, model: str) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [m.model_dump() for m in self.messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": self.stream,
        }


class ErrorBody(BaseModel):
    error: dict[str, Any]

