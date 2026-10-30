"""Deterministic assertions over explicitly supplied evidence, never execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import rfc8785
from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from .models import Assertion, Budgets, Closed


class ToolCall(Closed):
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class FaultObservation(Closed):
    fault_id: str = Field(min_length=1)
    applied: bool


class Evidence(Closed):
    """None means unavailable, while empty lists are explicit negative evidence."""

    complete: bool = False
    alignment: Literal["exact", "partial", "ambiguous"] = "ambiguous"
    output: Any = None
    tools: list[ToolCall] | None = None
    # This must be causal order supplied by the engine, not timestamp/list order.
    tool_order: list[str] | None = None
    unmatched_fixtures: int | None = Field(default=None, ge=0)
    model_requests: list[dict[str, Any]] | None = None
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"] | None = None
    budgets: dict[str, float] | None = None
    faults: list[FaultObservation] | None = None

    @model_validator(mode="after")
    def valid_evidence(self):
        if self.budgets is not None:
            for name, value in self.budgets.items():
                if name not in Budgets.model_fields or value < 0:
                    raise ValueError("Unknown budget metric or negative consumption")
                if name not in {"max_wall_time_seconds", "max_estimated_model_spend_usd"} and not value.is_integer():
                    raise ValueError("Call and token counts must be integers")
        if self.faults is not None and len({fault.fault_id for fault in self.faults}) != len(self.faults):
            raise ValueError("Duplicate fault evidence")
        try:
            rfc8785.dumps(self.model_dump())
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("Evidence must be finite canonical JSON") from exc
        return self


@dataclass(frozen=True)
class AssertionResult:
    kind: str
    status: Literal["pass", "fail", "inconclusive"]
    reason: str


def _equal(left: Any, right: Any) -> bool:
    return rfc8785.dumps(left) == rfc8785.dumps(right)


def _pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    if not pointer:
        return True, value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isascii() and token.isdigit():
            if (len(token) > 1 and token.startswith("0")) or len(token) > 16:
                return False, None
            index = int(token)
            if index >= len(value):
                return False, None
            value = value[index]
        else:
            return False, None
    return True, value


def evaluate(assertion: Assertion, evidence: Evidence) -> AssertionResult:
    """Evaluate a snapshot; incomplete or ambiguous evidence can never pass."""
    # Frozen Pydantic objects may contain mutable containers: revalidate copies
    # to avoid trusting values changed by a caller after construction.
    assertion = Assertion.model_validate(assertion.model_dump())
    evidence = Evidence.model_validate(evidence.model_dump(exclude_unset=True))
    kind, expected = assertion.kind, assertion.expected

    def result(passed: bool) -> AssertionResult:
        return AssertionResult(kind, "pass" if passed else "fail", "matched" if passed else "mismatch")

    def missing(reason: str) -> AssertionResult:
        return AssertionResult(kind, "inconclusive", reason)

    if not evidence.complete or evidence.alignment != "exact":
        return missing("Incomplete capture or non-exact alignment")
    if kind.startswith("output."):
        if "output" not in evidence.model_fields_set:
            return missing("Output evidence unavailable")
        if kind == "output.exact":
            return result(_equal(evidence.output, expected))
        if kind == "output.contains":
            return result(isinstance(evidence.output, str) and expected in evidence.output)
        if kind == "output.schema":
            return result(Draft202012Validator(expected).is_valid(evidence.output))
        found, value = _pointer(evidence.output, assertion.target)
        return result(found and _equal(value, expected))
    if kind in {"tool.called", "tool.count", "tool.arguments"}:
        if evidence.tools is None:
            return missing("Tool evidence unavailable")
        calls = [call for call in evidence.tools if call.name == assertion.target]
        if kind == "tool.called":
            return result(bool(calls) == expected)
        if kind == "tool.count":
            return result(len(calls) == expected)
        return result(any(_equal(call.arguments, expected) for call in calls))
    if kind == "tool.order":
        if evidence.tool_order is None:
            return missing("Causal tool ordering evidence unavailable")
        return result(_equal(evidence.tool_order, expected))
    if kind == "fixtures.unmatched":
        if evidence.unmatched_fixtures is None:
            return missing("Fixture consumption evidence unavailable")
        return result(evidence.unmatched_fixtures == expected)
    if kind == "model.request":
        if evidence.model_requests is None:
            return missing("Model request evidence unavailable")
        return result(any(_equal(request, expected) for request in evidence.model_requests))
    if kind == "outcome":
        if evidence.outcome in (None, "unknown"):
            return missing("Execution outcome unavailable or unknown")
        return result(evidence.outcome == expected)
    if kind == "budget":
        if evidence.budgets is None or assertion.target not in evidence.budgets:
            return missing("Measured budget evidence unavailable")
        return result(evidence.budgets[assertion.target] <= expected)
    if kind == "fault.applied":
        if evidence.faults is None:
            return missing("Fault evidence unavailable")
        return result(any(fault.fault_id == expected and fault.applied for fault in evidence.faults))
    raise ValueError(f"Unsupported assertion kind: {kind}")
