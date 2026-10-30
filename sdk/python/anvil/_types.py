"""Frozen v1 capture envelope and JSON types."""

from __future__ import annotations

import secrets
import time
import threading
import uuid
from datetime import datetime, timezone
from typing import Literal, TypedDict, Union

JSON = Union[None, bool, int, float, str, list["JSON"], dict[str, "JSON"]]
EventType = Literal[
    "run.started", "run.completed", "run.failed", "message.created",
    "model.requested", "model.chunk", "model.completed", "model.failed",
    "tool.requested", "tool.completed", "tool.failed", "state.checkpoint",
    "capture.gap", "fault.applied",
]


class Event(TypedDict):
    schema_version: Literal["1"]
    event_id: str
    run_id: str
    stream_id: str
    sequence: int
    type: EventType
    occurred_at: str
    trace_id: str
    span_id: str
    parent_event_ids: list[str]
    invocation_id: str
    payload: dict[str, JSON]


class Sequence:
    def __init__(self) -> None:
        self._value = 0
        self._export_value = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            value = self._value
            self._value += 1
            return value

    def export_next(self) -> int:
        with self._lock:
            value = self._export_value
            self._export_value += 1
            return value


def uuid7() -> str:
    value = (int(time.time() * 1000) << 80) | (7 << 76)
    value |= secrets.randbits(12) << 64
    value |= (2 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def check_uuid7(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("expected UUIDv7") from None
    if parsed.version != 7 or str(parsed) != value:
        raise ValueError("expected canonical UUIDv7")
    return value
