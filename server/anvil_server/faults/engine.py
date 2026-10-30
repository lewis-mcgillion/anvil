"""Offline fault execution: only supplied fixture values can be returned."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from .contracts import (
    MAX_CALLS, MAX_PLAN_BYTES, MAX_SAFE_INTEGER, EmptyFault, ExceptionFault,
    Fault, FaultPlan, Invocation, MockFixture, PatchFault, TextFault, TimeoutFault,
    TruncateFault, canonical, pointer_tokens,
)


class FaultExecutionError(ValueError):
    """Invalid/missing fixture or unsafe mutation; never dispatch to a real tool."""


class FaultCancelled(FaultExecutionError):
    """Cooperative cancellation; the engine is terminal after cancellation."""


@dataclass(frozen=True)
class ToolFailure:
    type: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class ToolResult:
    response: JsonValue
    error: ToolFailure | None = None


@dataclass(frozen=True)
class Evidence:
    fault_id: str
    fault_type: str
    target_invocation_path: str
    status: Literal["applied", "unreached"]
    simulated_latency_ms: int
    simulated_time_ms: int


@dataclass(frozen=True)
class FaultReport:
    plan_hash: str
    seed: int
    status: Literal["open", "completed", "cancelled", "error"]
    calls_consumed: int
    simulated_time_ms: int
    evidence: tuple[Evidence, ...]
    unused_fixture_paths: tuple[str, ...]

    @property
    def all_faults_applied(self) -> bool:
        return self.status == "completed" and all(item.status == "applied" for item in self.evidence)

    @property
    def digest(self) -> str:
        from dataclasses import asdict

        value = asdict(self)
        value["evidence"] = [asdict(item) for item in self.evidence]
        value["unused_fixture_paths"] = list(self.unused_fixture_paths)
        return hashlib.sha256(canonical(value, limit=MAX_PLAN_BYTES)).hexdigest()


def patch(response: JsonValue, fault: PatchFault) -> JsonValue:
    tokens = pointer_tokens(fault.parameters.pointer)
    value = deepcopy(fault.parameters.value)
    if not tokens:
        return value
    result = deepcopy(response)
    parent = result
    for index, token in enumerate(tokens):
        if isinstance(parent, dict):
            if token not in parent:
                raise FaultExecutionError("JSON pointer must reference an existing member")
            key = token
        elif isinstance(parent, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or len(token) > 8:
                raise FaultExecutionError("JSON pointer requires a canonical array index")
            key = int(token)
            if key >= len(parent):
                raise FaultExecutionError("JSON pointer index out of bounds")
        else:
            raise FaultExecutionError("JSON pointer traverses a scalar")
        if index == len(tokens) - 1:
            parent[key] = value
        else:
            parent = parent[key]
    return result


class FaultEngine:
    """Single-consumer engine; callers supply causal order, never timestamps."""

    def __init__(
        self, plan: FaultPlan, fixtures: Sequence[MockFixture], *, seed: int,
        cancelled: Callable[[], bool] | None = None,
    ):
        if type(seed) is not int or not 0 <= seed <= MAX_SAFE_INTEGER:
            raise ValueError("Seed must be an interoperable nonnegative integer")
        if len(fixtures) > MAX_CALLS:
            raise ValueError("Too many mock fixtures")
        self._plan = FaultPlan.model_validate(plan.model_dump(mode="python"))
        self._fixtures = {}
        for fixture in fixtures:
            snapshot = MockFixture.model_validate(fixture.model_dump(mode="python"))
            path = snapshot.request.target_invocation_path
            if path in self._fixtures:
                raise FaultExecutionError("Ambiguous fixture logical identity")
            self._fixtures[path] = snapshot
        self._faults = {fault.target_invocation_path: fault for fault in self._plan.faults}
        self._seed = seed
        self._cancelled = cancelled
        self._consumed: set[str] = set()
        self._evidence: dict[str, Evidence] = {}
        self._clock = 0
        self._status: Literal["open", "completed", "cancelled", "error"] = "open"
        # Preflight every resolvable mutation before consuming any fixture.
        for path, fault in self._faults.items():
            fixture = self._fixtures.get(path)
            if fixture is not None:
                self._apply(fixture.response, fault)

    def _apply(self, response: JsonValue, fault: Fault) -> ToolResult:
        error = None
        if isinstance(fault, ExceptionFault):
            params = fault.parameters
            response = None
            error = ToolFailure(params.error_type, params.message, params.retryable)
        elif isinstance(fault, TimeoutFault):
            response = None
            error = ToolFailure("timeout", "Simulated tool timeout", True)
        elif isinstance(fault, EmptyFault):
            response = {"null": None, "object": {}, "array": [], "text": ""}[fault.parameters.response]
        elif isinstance(fault, PatchFault):
            response = patch(response, fault)
        elif isinstance(fault, TruncateFault):
            if not isinstance(response, str):
                raise FaultExecutionError("Stream truncation requires a recorded text fixture")
            response = response.encode("utf-8")[:fault.parameters.max_bytes].decode("utf-8", errors="ignore")
        elif isinstance(fault, TextFault):
            if fault.fault_type == "prompt.injection" and not isinstance(response, str):
                raise FaultExecutionError("Prompt injection requires a recorded text fixture")
            response = fault.parameters.text
        else:
            raise FaultExecutionError("Unsupported fault type")
        canonical(response)
        return ToolResult(deepcopy(response), error)

    def _latency(self, fault: Fault) -> int:
        material = canonical([self._seed, fault.fault_id, fault.target_invocation_path])
        draw = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return fault.parameters.simulated_latency_ms + draw % (fault.parameters.jitter_ms + 1)

    def _check_cancelled(self) -> None:
        if self._cancelled is not None and self._cancelled():
            self._status = "cancelled"
            raise FaultCancelled("Offline fault execution cancelled")

    def invoke(self, request: Invocation) -> ToolResult:
        if self._status != "open":
            raise FaultExecutionError("Engine is terminal")
        try:
            self._check_cancelled()
            request = Invocation.model_validate(request.model_dump(mode="python"))
            path = request.target_invocation_path
            fixture = self._fixtures.get(path)
            if fixture is None:
                raise FaultExecutionError("Unmatched fixture invocation")
            if path in self._consumed:
                raise FaultExecutionError("Fixture already consumed")
            if (
                request.tool_name != fixture.request.tool_name
                or request.schema_digest != fixture.request.schema_digest
                or canonical(request.arguments) != canonical(fixture.request.arguments)
            ):
                raise FaultExecutionError("Fixture invocation name, schema or arguments mismatch")
            fault = self._faults.get(path)
            result = ToolResult(deepcopy(fixture.response)) if fault is None else self._apply(fixture.response, fault)
            self._check_cancelled()
            self._consumed.add(path)
            if fault is not None:
                latency = self._latency(fault)
                self._clock += latency
                self._evidence[fault.fault_id] = Evidence(
                    fault.fault_id, fault.fault_type, path, "applied", latency, self._clock,
                )
            return result
        except FaultCancelled:
            raise
        except (ValueError, TypeError) as exc:
            self._status = "error"
            raise FaultExecutionError(str(exc)) from exc

    def finish(self) -> FaultReport:
        if self._status == "open":
            self._check_cancelled()
            self._status = "completed"
        return self.report()

    def report(self) -> FaultReport:
        evidence = tuple(
            self._evidence.get(fault.fault_id) or Evidence(
                fault.fault_id, fault.fault_type, fault.target_invocation_path, "unreached", 0, self._clock,
            )
            for fault in self._plan.faults
        )
        return FaultReport(
            self._plan.digest, self._seed, self._status, len(self._consumed), self._clock,
            evidence, tuple(sorted(set(self._fixtures) - self._consumed)),
        )


def execute(
    plan: FaultPlan, fixtures: Sequence[MockFixture], ordered_calls: Sequence[Invocation], *,
    seed: int, cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[ToolResult, ...], FaultReport]:
    if len(ordered_calls) > MAX_CALLS:
        raise ValueError("Too many ordered calls")
    engine = FaultEngine(plan, fixtures, seed=seed, cancelled=cancelled)
    results = tuple(engine.invoke(request) for request in ordered_calls)
    return results, engine.finish()
