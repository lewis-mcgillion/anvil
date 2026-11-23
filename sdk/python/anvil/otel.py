"""Map exported OpenTelemetry spans to replay-ineligible Anvil correlation records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def map_span(span: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve trace topology without claiming that OTEL spans are replay evidence."""
    if not isinstance(span, Mapping):
        raise ValueError("Invalid span")
    context = span.get("context", span)
    if not isinstance(context, Mapping):
        raise ValueError("Invalid span context")
    trace_id = context.get("trace_id")
    span_id = context.get("span_id")
    parent = span.get("parent_span_id")
    if parent is None and span.get("parent") is not None:
        if not isinstance(span["parent"], Mapping):
            raise ValueError("Invalid parent span")
        parent = span["parent"].get("span_id")
    if (not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id)
            or not isinstance(span_id, str) or not re.fullmatch(r"[0-9a-f]{16}", span_id)
            or (parent is not None and
                (not isinstance(parent, str) or not re.fullmatch(r"[0-9a-f]{16}", parent)))):
        raise ValueError("Invalid OpenTelemetry trace or span identity")
    attributes = span.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise ValueError("Invalid span attributes")
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": span.get("name"),
        "operation": attributes.get("gen_ai.operation.name"),
        "system": attributes.get("gen_ai.system"),
        "replay_safe": False,
        "replay_eligibility": "ineligible",
    }
