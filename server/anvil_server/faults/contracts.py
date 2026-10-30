"""Strict, bounded offline profile of the immutable v1 fault-plan envelope."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel, ConfigDict, Field, field_validator, model_validator,
)
from typing_extensions import TypeAliasType

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PLAN_BYTES = 1024 * 1024
MAX_NODES = 10000
MAX_DEPTH = 32
MAX_CALLS = 100
MAX_SAFE_INTEGER = 2**53 - 1
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
LogicalPath = Annotated[str, Field(
    min_length=1, max_length=512,
    pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*(/[A-Za-z0-9_][A-Za-z0-9_.:-]*)*$",
)]
OfflineJSON = TypeAliasType("OfflineJSON", Union[
    None, bool, Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)],
    str, list["OfflineJSON"], dict[str, "OfflineJSON"],
])


def canonical(value: object, *, limit: int = MAX_PAYLOAD_BYTES) -> bytes:
    """JCS for the supported JSON subset: floats and unsafe integers fail closed."""
    nodes = 0
    size = 0

    def account(count: int) -> None:
        nonlocal size
        size += count
        if size > limit:
            raise ValueError("JSON exceeds byte limit")

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("JSON structure exceeds node/depth limit")
        if item is None:
            account(4)
            return
        if type(item) is bool:
            account(4 if item else 5)
            return
        if type(item) is int:
            if abs(item) > MAX_SAFE_INTEGER:
                raise ValueError("JSON integer exceeds interoperable safe range")
            account(len(str(item)))
            return
        if type(item) is str:
            if len(item) > limit:
                raise ValueError("JSON exceeds byte limit")
            account(len(json.dumps(item, ensure_ascii=False).encode("utf-8")))
            return
        if type(item) is list:
            account(2 + max(0, len(item) - 1))
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            account(2 + max(0, len(item) - 1) + len(item))
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise ValueError("Only JSON null, booleans, safe integers, strings, arrays and objects are supported")

    visit(value, 0)

    def encode(item: object) -> str:
        if type(item) is dict:
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in keys) + "}"
        if type(item) is list:
            return "[" + ",".join(encode(child) for child in item) + "]"
        return json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    data = encode(value).encode("utf-8")
    return data


def logical_path(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*(/[A-Za-z0-9_][A-Za-z0-9_.:-]*)*", value):
        raise ValueError("Expected a relative exact logical invocation path")
    if any(segment in {".", ".."} for segment in value.split("/")):
        raise ValueError("Logical path traversal is forbidden")
    return value


def pointer_tokens(value: str) -> list[str]:
    if not value:
        return []
    if len(value) > 1024 or not value.startswith("/") or re.search(r"~(?![01])", value):
        raise ValueError("Invalid RFC 6901 JSON pointer")
    tokens = [part.replace("~1", "/").replace("~0", "~") for part in value[1:].split("/")]
    if len(tokens) > MAX_DEPTH or any(token in {".", "..", "__proto__", "prototype", "constructor"} for token in tokens):
        raise ValueError("Unsafe JSON pointer")
    return tokens


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Parameters(StrictModel):
    simulated_latency_ms: int = Field(default=0, ge=0, le=120000)
    jitter_ms: int = Field(default=0, ge=0, le=120000)

    @model_validator(mode="after")
    def bound_latency(self):
        if self.simulated_latency_ms + self.jitter_ms > 120000:
            raise ValueError("Combined simulated latency exceeds 120 seconds")
        return self


class ExceptionParameters(Parameters):
    error_type: Identifier
    message: str = Field(max_length=4096)
    retryable: bool = False


class EmptyParameters(Parameters):
    response: Literal["null", "object", "array", "text"] = "null"


class PatchParameters(Parameters):
    pointer: str = Field(max_length=1024)
    value: OfflineJSON

    @field_validator("pointer")
    @classmethod
    def validate_pointer(cls, value):
        pointer_tokens(value)
        return value

    @field_validator("value", mode="before")
    @classmethod
    def bound_value(cls, value):
        canonical(value)
        return value


class TruncateParameters(Parameters):
    max_bytes: int = Field(ge=0, le=MAX_PAYLOAD_BYTES)


class TextParameters(Parameters):
    text: str = Field(max_length=MAX_PAYLOAD_BYTES)

    @field_validator("text")
    @classmethod
    def bound_text(cls, value):
        canonical(value)
        return value


class FaultBase(StrictModel):
    fault_id: Identifier
    target_invocation_path: LogicalPath

    @field_validator("target_invocation_path")
    @classmethod
    def validate_path(cls, value):
        return logical_path(value)


class ExceptionFault(FaultBase):
    fault_type: Literal["tool.exception"]
    parameters: ExceptionParameters


class TimeoutFault(FaultBase):
    fault_type: Literal["tool.timeout"]
    parameters: Parameters


class EmptyFault(FaultBase):
    fault_type: Literal["tool.empty_response"]
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


class PatchFault(FaultBase):
    fault_type: Literal["json_pointer.patch"]
    parameters: PatchParameters


class TruncateFault(FaultBase):
    fault_type: Literal["stream.truncate"]
    parameters: TruncateParameters


class TextFault(FaultBase):
    fault_type: Literal["tool.malformed_response", "prompt.injection"]
    parameters: TextParameters


Fault = Annotated[
    ExceptionFault | TimeoutFault | EmptyFault | PatchFault | TruncateFault | TextFault,
    Field(discriminator="fault_type"),
]


class FaultPlan(StrictModel):
    fault_plan_version: Literal["1"]
    faults: list[Fault] = Field(max_length=MAX_CALLS)

    @model_validator(mode="after")
    def reject_conflicts(self):
        ids = [fault.fault_id for fault in self.faults]
        paths = [fault.target_invocation_path for fault in self.faults]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate fault identity")
        if len(set(paths)) != len(paths):
            raise ValueError("Conflicting faults target the same invocation; composition is unsupported")
        canonical(self.model_dump(mode="python"), limit=MAX_PLAN_BYTES)
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="python"), limit=MAX_PLAN_BYTES)).hexdigest()


def parse_plan(data: str | bytes) -> FaultPlan:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("Fault plan exceeds byte limit")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except RecursionError as exc:
        raise ValueError("Fault plan exceeds nesting limit") from exc
    canonical(value, limit=MAX_PLAN_BYTES)
    return FaultPlan.model_validate(value)


class Invocation(StrictModel):
    target_invocation_path: LogicalPath
    tool_name: Identifier
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: dict[str, OfflineJSON]

    @field_validator("target_invocation_path")
    @classmethod
    def validate_path(cls, value):
        return logical_path(value)

    @field_validator("arguments", mode="before")
    @classmethod
    def bound_arguments(cls, value):
        canonical(value)
        return value


class MockFixture(StrictModel):
    request: Invocation
    response: OfflineJSON

    @field_validator("response", mode="before")
    @classmethod
    def bound_response(cls, value):
        canonical(value)
        return value
