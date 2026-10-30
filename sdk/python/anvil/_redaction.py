"""Fail-closed, copy-on-capture payload policy. Never stores original payloads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping

from ._types import EventType, JSON

_FIELDS = {
    "run.started": {"agent_name", "adapter", "adapter_version", "input"},
    "run.completed": {"outcome", "output", "usage"},
    "run.failed": {"error"},
    "message.created": {"role", "content"},
    "model.requested": {"model", "provider", "request"},
    "model.chunk": {"chunk_index", "chunk"},
    "model.completed": {"model", "response", "usage"},
    "model.failed": {"model", "error"},
    "tool.requested": {"tool_name", "arguments", "arguments_schema_digest"},
    "tool.completed": {"tool_name", "arguments", "result", "duration_ms"},
    "tool.failed": {"tool_name", "arguments", "error"},
    "state.checkpoint": {"state", "label"},
    "capture.gap": {"after_sequence", "expected_count", "reason", "detail"},
    "fault.applied": {"fault_id", "fault_type", "target_invocation_id", "parameters", "simulated_latency_ms"},
}
_SENSITIVE = {
    "input", "output", "content", "request", "response", "arguments", "result",
    "state", "parameters", "detail", "label",
}
_CREDENTIAL = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[_-]?key|credential)"
)
_PATTERNS = (
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*",
    r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+",
    r"\bsk-[A-Za-z0-9_-]+",
)
_MARKER = "[REDACTED]"


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class RedactionPolicy:
    """Only listed payload JSON pointers opt arbitrary data into capture.

    An allowed pointer includes its descendants. Deny pointers take precedence.
    Stream chunks and exception messages are always omitted: secrets can straddle
    chunk boundaries or be embedded in arbitrary exception text.
    """

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for path in (*self.allow, *self.deny):
            if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
                raise ValueError("redaction paths must be non-root RFC 6901 pointers")
        for pattern in self.patterns:
            re.compile(pattern)

    def apply(self, event_type: EventType, payload: Mapping[str, JSON]) -> dict[str, JSON]:
        if event_type not in _FIELDS:
            raise ValueError("unsupported event type")
        redactions: set[str] = set()
        patterns = tuple(re.compile(p) for p in (*_PATTERNS, *self.patterns))

        def scrub(value: JSON, path: str, depth: int = 0) -> JSON:
            if depth > 64:
                raise ValueError("capture payload exceeds maximum nesting")
            if any(path == p or path.startswith(p + "/") for p in self.deny):
                redactions.add(path)
                return _MARKER
            if isinstance(value, str):
                result = value
                for pattern in patterns:
                    result = pattern.sub(_MARKER, result)
                if result != value:
                    redactions.add(path)
                return result
            if value is None or type(value) in (bool, int):
                return value
            if isinstance(value, float) and math.isfinite(value):
                return value
            if isinstance(value, list):
                return [scrub(v, f"{path}/{i}", depth + 1) for i, v in enumerate(value)]
            if isinstance(value, dict):
                result_object: dict[str, JSON] = {}
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise ValueError("capture JSON keys must be strings")
                    if any(p.search(key) for p in patterns):
                        redactions.add(path)
                        continue
                    child_path = f"{path}/{_pointer(key)}"
                    if _CREDENTIAL.search(key):
                        result_object[key] = _MARKER
                        redactions.add(child_path)
                    else:
                        result_object[key] = scrub(child, child_path, depth + 1)
                return result_object
            raise ValueError("capture values must be finite JSON data")

        result: dict[str, JSON] = {}
        for field in _FIELDS[event_type]:
            if field not in payload:
                continue
            path = "/" + field
            value = payload[field]
            if field == "error":
                result[field] = {"type": "agent_error", "message": _MARKER}
                redactions.add(path)
            elif field == "chunk":
                result[field] = _MARKER
                redactions.add(path)
            elif field in _SENSITIVE:
                if any(path == p or path.startswith(p + "/") for p in self.allow):
                    result[field] = scrub(value, path)
                elif isinstance(value, dict) and any(p.startswith(path + "/") for p in self.allow):
                    # Preserve structural objects (e.g. request), but only copy opted-in children.
                    def select(obj: dict[str, JSON], parent: str) -> dict[str, JSON]:
                        selected: dict[str, JSON] = {}
                        for key, child in obj.items():
                            if not isinstance(key, str):
                                raise ValueError("capture JSON keys must be strings")
                            child_path = parent + "/" + _pointer(key)
                            if any(child_path == p or child_path.startswith(p + "/") for p in self.allow):
                                selected[key] = child
                            elif isinstance(child, dict) and any(p.startswith(child_path + "/") for p in self.allow):
                                selected[key] = select(child, child_path)
                            else:
                                redactions.add(parent)
                        return selected
                    result[field] = scrub(select(value, path), path)
                else:
                    result[field] = {} if field == "request" else _MARKER
                    redactions.add(path)
            else:
                result[field] = scrub(value, path)
        if set(payload) - (_FIELDS[event_type] | {"redactions", "replay_safe"}):
            redactions.add("")
        result["redactions"] = sorted(redactions)
        result["replay_safe"] = not redactions and payload.get("replay_safe", True) is True and event_type != "capture.gap"
        return result
