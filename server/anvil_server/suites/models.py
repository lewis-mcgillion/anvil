"""Closed version-alpha suite contract; no execution primitives."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import rfc8785
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Version = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def local_path(value: str) -> str:
    if (not value or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("Reference must be a relative file path within the suite root")
    return value


class Reference(Closed):
    file: str
    sha256: SHA256

    @model_validator(mode="after")
    def safe_path(self):
        local_path(self.file)
        return self


class Agent(Closed):
    image: Annotated[str, Field(
        pattern=r"^(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/)?"
                r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
                r"@sha256:[0-9a-f]{64}$"
    )]
    entrypoint: Annotated[str, Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")]
    adapter: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    adapter_version: Version


class Budgets(Closed):
    max_wall_time_seconds: int = Field(default=120, ge=1)
    max_model_calls: int = Field(default=20, ge=0)
    max_tool_calls: int = Field(default=100, ge=0)
    max_input_tokens: int = Field(default=20000, ge=0)
    max_output_tokens: int = Field(default=10000, ge=0)
    max_estimated_model_spend_usd: float = Field(default=1.0, ge=0, allow_inf_nan=False)


AssertionKind = Literal[
    "output.exact", "output.contains", "output.schema", "output.pointer",
    "tool.called", "tool.count", "tool.arguments", "tool.order",
    "fixtures.unmatched", "model.request", "outcome", "budget", "fault.applied",
]


def validate_output_schema(schema: dict) -> None:
    """Never resolve references or execute untrusted backtracking regexes."""
    def visit(value):
        if not isinstance(value, dict):
            return
        if value.get("$schema", "https://json-schema.org/draft/2020-12/schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("Only JSON Schema draft 2020-12 is supported")
        if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties")):
            raise ValueError("Schema references and regex keywords are unsupported")
        for key in ("not", "if", "then", "else", "items", "contains", "additionalProperties",
                    "propertyNames", "unevaluatedItems", "unevaluatedProperties", "contentSchema"):
            visit(value.get(key))
        for key in ("properties", "$defs", "definitions", "dependentSchemas"):
            children = value.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    visit(child)
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = value.get(key)
            if isinstance(children, list):
                for child in children:
                    visit(child)
    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid output schema: {exc.message}") from exc


class Assertion(Closed):
    kind: AssertionKind
    expected: Any
    target: str | None = None

    @model_validator(mode="after")
    def valid_arguments(self):
        kind, expected = self.kind, self.expected
        try:
            rfc8785.dumps(expected)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("Expected value must be finite canonical JSON") from exc
        needs_target = kind in {"output.pointer", "tool.called", "tool.count", "tool.arguments", "budget"}
        if needs_target != (self.target is not None):
            raise ValueError("Assertion target is required only for pointer/tool/budget assertions")
        if needs_target and kind != "output.pointer" and not self.target:
            raise ValueError("Assertion target cannot be empty")
        if kind == "output.pointer" and not re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", self.target):
            raise ValueError("Invalid RFC 6901 JSON pointer")
        if kind in {"tool.count", "fixtures.unmatched"} and (type(expected) is not int or expected < 0):
            raise ValueError("Expected a nonnegative integer")
        if kind == "tool.called" and type(expected) is not bool:
            raise ValueError("Expected a boolean")
        if kind in {"output.contains", "fault.applied"} and (not isinstance(expected, str) or not expected):
            raise ValueError("Expected a nonempty string")
        if kind == "outcome" and expected not in ("succeeded", "failed", "cancelled", "unknown"):
            raise ValueError("Invalid execution outcome")
        if kind in {"tool.arguments", "model.request", "output.schema"} and not isinstance(expected, dict):
            raise ValueError("Expected an object")
        if kind == "tool.order" and (
            not isinstance(expected, list) or not all(isinstance(item, str) and item for item in expected)
        ):
            raise ValueError("Expected an ordered list of tool names")
        if kind == "budget":
            if self.target not in Budgets.model_fields:
                raise ValueError("Unknown budget metric")
            if type(expected) not in (int, float) or expected < 0:
                raise ValueError("Expected a nonnegative numeric upper bound")
        if kind == "output.schema":
            validate_output_schema(expected)
        return self


class Case(Closed):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
    recording: Reference
    agent: Agent
    model_mode: Literal["recorded"] = "recorded"
    tool_policy: Literal["strict"] = "strict"
    seed: int = Field(ge=0, le=9007199254740991)
    normalizer_version: Literal["1"] = "1"
    parameter_version: Literal["1"] = "1"
    initial_state: dict[str, Any] = Field(default_factory=dict)
    fault_plan: Reference | None = None
    budgets: Budgets = Field(default_factory=Budgets)
    assertions: list[Assertion] = Field(min_length=1, max_length=1000)


class Suite(Closed):
    apiVersion: Literal["anvil.dev/v1alpha1"]
    kind: Literal["RegressionSuite"]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    cases: list[Case] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case id")
        return self


def suite_schema() -> dict:
    """Structural schema; semantic validators additionally apply during parsing."""
    return Suite.model_json_schema()
