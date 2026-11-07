from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import orjson

SSE_DONE = b"data: [DONE]\n\n"


@dataclass
class SSEEvent:
    event: str = "message"
    data: str = ""
    json_data: Any | None = None
    id: str | None = None
    retry: int | None = None

    def encode(self) -> bytes:
        parts: list[str] = []
        if self.id:
            parts.append(f"id: {self.id}\n")
        if self.event:
            parts.append(f"event: {self.event}\n")
        if self.retry is not None:
            parts.append(f"retry: {self.retry}\n")
        if self.json_data is not None:
            parts.append(f"data: {orjson.dumps(self.json_data).decode()}\n\n")
        else:
            parts.append(f"data: {self.data}\n\n")
        return "".join(parts).encode()


@dataclass
class SSEDecoder:
    """Incremental decoder for an SSE byte stream."""

    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buffer.extend(chunk)
        events: list[SSEEvent] = []
        while True:
            separator = self._buffer.find(b"\n\n")
            if separator == -1:
                break
            raw = bytes(self._buffer[:separator])
            del self._buffer[: separator + 2]
            event = self._decode_frame(raw)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[SSEEvent]:
        if not self._buffer.strip():
            self._buffer.clear()
            return []
        raw = bytes(self._buffer)
        self._buffer.clear()
        event = self._decode_frame(raw)
        return [event] if event is not None else []

    @staticmethod
    def _decode_frame(raw: bytes) -> SSEEvent | None:
        event = SSEEvent()
        data_lines: list[str] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line or line.startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field_name == "data":
                data_lines.append(value)
            elif field_name == "event":
                event.event = value
            elif field_name == "id":
                event.id = value
            elif field_name == "retry":
                try:
                    event.retry = int(value)
                except ValueError:
                    pass
        if not data_lines:
            return None
        event.data = "\n".join(data_lines)
        if event.data != "[DONE]":
            try:
                event.json_data = json.loads(event.data)
            except json.JSONDecodeError:
                event.json_data = None
        return event

