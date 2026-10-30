import math

import pytest
from pydantic import ValidationError

from anvil_server.suites import Assertion, Evidence, evaluate


CATALOG = [
    ("output.exact", None, {"a": 1}, {"output": {"a": 1}}, {"output": {"a": 2}}),
    ("output.contains", None, "good", {"output": "very good"}, {"output": "bad"}),
    ("output.schema", None, {"type": "integer"}, {"output": 1}, {"output": "1"}),
    ("output.pointer", "/a~1b/~0key/0", 1,
     {"output": {"a/b": {"~key": [1]}}}, {"output": {"a/b": {"~key": [2]}}}),
    ("tool.called", "lookup", True,
     {"tools": [{"name": "lookup", "arguments": {}}]}, {"tools": []}),
    ("tool.count", "lookup", 1,
     {"tools": [{"name": "lookup", "arguments": {}}]}, {"tools": []}),
    ("tool.arguments", "lookup", {"id": 1},
     {"tools": [{"name": "lookup", "arguments": {"id": 1}}]},
     {"tools": [{"name": "lookup", "arguments": {"id": 2}}]}),
    ("tool.order", None, ["first", "second"],
     {"tool_order": ["first", "second"]}, {"tool_order": ["second", "first"]}),
    ("fixtures.unmatched", None, 0, {"unmatched_fixtures": 0}, {"unmatched_fixtures": 1}),
    ("model.request", None, {"prompt": "hello"},
     {"model_requests": [{"prompt": "hello"}]}, {"model_requests": []}),
    ("outcome", None, "succeeded", {"outcome": "succeeded"}, {"outcome": "failed"}),
    ("budget", "max_tool_calls", 2,
     {"budgets": {"max_tool_calls": 2}}, {"budgets": {"max_tool_calls": 3}}),
    ("fault.applied", None, "fault-1",
     {"faults": [{"fault_id": "fault-1", "applied": True}]},
     {"faults": [{"fault_id": "fault-1", "applied": False}]}),
]


@pytest.mark.parametrize("kind,target,expected,passing,failing", CATALOG)
def test_every_assertion_pass_fail_and_missing(kind, target, expected, passing, failing):
    assertion = Assertion(kind=kind, target=target, expected=expected)
    assert evaluate(assertion, Evidence(complete=True, alignment="exact", **passing)).status == "pass"
    assert evaluate(assertion, Evidence(complete=True, alignment="exact", **failing)).status == "fail"
    result = evaluate(assertion, Evidence(complete=True, alignment="exact"))
    assert result.status == "inconclusive"
    assert "unavailable" in result.reason
    for complete, alignment in [(False, "exact"), (True, "ambiguous"), (True, "partial")]:
        assert evaluate(assertion, Evidence(complete=complete, alignment=alignment, **passing)).status == "inconclusive"


def check(kind, expected, target=None, **data):
    return evaluate(
        Assertion(kind=kind, target=target, expected=expected),
        Evidence(complete=True, alignment="exact", **data),
    ).status


def test_missing_differs_from_json_null():
    assert check("output.exact", None) == "inconclusive"
    assert check("output.exact", None, output=None) == "pass"
    assert check("output.exact", {}, output=None) == "fail"


def test_equality_is_json_typed_and_canonical():
    assert check("output.exact", {"a": 1, "b": 2}, output={"b": 2, "a": 1.0}) == "pass"
    assert check("output.exact", True, output=1) == "fail"
    assert check("output.exact", [1], output=[True]) == "fail"
    assert check("tool.arguments", {"id": True}, target="lookup",
                 tools=[{"name": "lookup", "arguments": {"id": 1}}]) == "fail"


@pytest.mark.parametrize("pointer", ["/missing", "/items/01", "/items/-", "/items/2", "/items/99999999999999999999"])
def test_pointer_missing_and_invalid_array_indices_fail(pointer):
    assert check("output.pointer", None, target=pointer, output={"items": [None]}) == "fail"


def test_pointer_root_numeric_keys_and_escapes():
    assert check("output.pointer", {"x": 1}, target="", output={"x": 1}) == "pass"
    assert check("output.pointer", "value", target="/01", output={"01": "value"}) == "pass"
    assert check("output.pointer", None, target="/items/0", output={"items": [None]}) == "pass"
    assert check("output.pointer", 1, target="/~01", output={"~1": 1}) == "pass"


def test_explicit_absence_and_no_fabricated_order_or_outcome():
    assert check("tool.called", False, target="lookup", tools=[]) == "pass"
    assert check("tool.called", False, target="lookup") == "inconclusive"
    assert check("tool.count", 0, target="lookup", tools=[]) == "pass"
    assert check("tool.order", [], tools=[]) == "inconclusive"
    assert check("tool.order", [], tool_order=[]) == "pass"
    assert check("outcome", "unknown", outcome="unknown") == "inconclusive"
    assert check("outcome", "succeeded", outcome="unknown") == "inconclusive"
    assert check("fault.applied", "f", faults=[]) == "fail"
    assert check("output.contains", "a", output={"a": 1}) == "fail"


def test_output_data_is_not_executed_or_interpolated():
    assert check("output.exact", "$(id)", output="$(id)") == "pass"


def test_schema_keywords_in_literal_data_or_property_names_are_not_executed():
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "$ref": {"const": "https://evil.invalid/"},
        },
        "required": ["pattern", "$ref"],
    }
    assert check("output.schema", schema, output={"pattern": "(a+)+$", "$ref": "https://evil.invalid/"}) == "pass"
    assert check("output.schema", {"const": {"$ref": "file:///etc/passwd"}},
                 output={"$ref": "file:///etc/passwd"}) == "pass"


@pytest.mark.parametrize("data", [
    {"kind": "unknown", "expected": True},
    {"kind": "outcome", "expected": "succeeded", "unknown": True},
    {"kind": "outcome", "expected": "running"},
    {"kind": "outcome", "expected": "succeeded", "target": "bad"},
    {"kind": "tool.count", "expected": 1},
    {"kind": "tool.count", "expected": True, "target": "t"},
    {"kind": "tool.count", "expected": -1, "target": "t"},
    {"kind": "tool.called", "expected": 1, "target": "t"},
    {"kind": "tool.called", "expected": True, "target": ""},
    {"kind": "tool.arguments", "expected": [], "target": "t"},
    {"kind": "tool.order", "expected": ["a", 1]},
    {"kind": "model.request", "expected": []},
    {"kind": "fixtures.unmatched", "expected": -1},
    {"kind": "fault.applied", "expected": ""},
    {"kind": "output.contains", "expected": []},
    {"kind": "output.exact", "expected": float("nan")},
    {"kind": "output.pointer", "expected": 1, "target": "not/pointer"},
    {"kind": "output.pointer", "expected": 1, "target": "/~2"},
    {"kind": "budget", "expected": -1, "target": "max_model_calls"},
    {"kind": "budget", "expected": True, "target": "max_model_calls"},
    {"kind": "budget", "expected": 1, "target": "unknown"},
    {"kind": "output.schema", "expected": {"type": "invalid"}},
    {"kind": "output.schema", "expected": {"$schema": "http://json-schema.org/draft-07/schema#"}},
    {"kind": "output.schema", "expected": {"$ref": "https://evil.invalid/schema"}},
    {"kind": "output.schema", "expected": {"$ref": "file:///etc/passwd"}},
    {"kind": "output.schema", "expected": {"$dynamicRef": "#recursive"}},
    {"kind": "output.schema", "expected": {"properties": {"x": {"$ref": "#/local"}}}},
    {"kind": "output.schema", "expected": {"items": {"$ref": "https://evil.invalid/"}}},
    {"kind": "output.schema", "expected": {"anyOf": [{"$dynamicRef": "#recursive"}]}},
    {"kind": "output.schema", "expected": {"unevaluatedProperties": {"pattern": "(a+)+$"}}},
    {"kind": "output.schema", "expected": {"pattern": "(a+)+$"}},
    {"kind": "output.schema", "expected": {"patternProperties": {"(a+)+$": {}}}},
])
def test_invalid_assertions_reject(data):
    with pytest.raises(ValidationError):
        Assertion.model_validate(data)


@pytest.mark.parametrize("data", [
    {"complete": 1}, {"alignment": "assumed"}, {"unknown": "value"},
    {"unmatched_fixtures": -1}, {"unmatched_fixtures": True},
    {"outcome": "running"}, {"output": math.inf}, {"output": {"a": 2**53}},
    {"tools": [{"name": "t"}]}, {"tools": [{"name": "t", "arguments": {}, "command": "bad"}]},
    {"budgets": {"unknown": 1.0}}, {"budgets": {"max_tool_calls": -1.0}},
    {"budgets": {"max_tool_calls": math.nan}},
    {"budgets": {"max_tool_calls": 1.5}},
    {"faults": [{"fault_id": "f", "applied": True}, {"fault_id": "f", "applied": False}]},
])
def test_invalid_evidence_rejects(data):
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)


def test_evaluation_revalidates_mutable_nested_inputs():
    assertion = Assertion(kind="output.schema", expected={"type": "string"})
    assertion.expected["$ref"] = "https://evil.invalid/"
    with pytest.raises(ValidationError):
        evaluate(assertion, Evidence(output="safe", complete=True, alignment="exact"))
    evidence = Evidence(complete=True, alignment="exact", budgets={"max_tool_calls": 1.0})
    evidence.budgets["max_tool_calls"] = math.inf
    with pytest.raises(ValidationError):
        evaluate(Assertion(kind="budget", target="max_tool_calls", expected=10), evidence)
