import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from anvil_server.faults import FaultPlan, Invocation, MockFixture, parse_plan
from anvil_server.faults.contracts import (
    MAX_PAYLOAD_BYTES, MAX_PLAN_BYTES, MAX_SAFE_INTEGER, canonical,
)

ROOT = Path(__file__).resolve().parents[2]


def plan_data(fault_type="tool.empty_response", parameters=None, path="root/lookup"):
    return {
        "fault_plan_version": "1",
        "faults": [{
            "fault_id": "fault-1", "fault_type": fault_type,
            "target_invocation_path": path,
            "parameters": {} if parameters is None else parameters,
        }],
    }


def test_versioned_fixture_and_generated_schema_are_current():
    fixture = ROOT / "fixtures/faults/v1/retry-plan.valid.json"
    plan = parse_plan(fixture.read_bytes())
    assert plan.fault_plan_version == "1"
    schema = json.loads((fixture.parent / "plan.schema.json").read_text())
    assert schema == FaultPlan.model_json_schema()


@pytest.mark.parametrize("change", [
    {"fault_plan_version": "2"}, {"fault_plan_version": 1}, {"unknown": True},
    {"seed": 1}, {"faults": None}, {"faults": "not a list"},
])
def test_invalid_envelope(change):
    with pytest.raises(ValidationError):
        FaultPlan.model_validate(plan_data() | change)


@pytest.mark.parametrize("fault_type,parameters", [
    ("model.provider_failure", {}),
    ("shell.execute", {"command": "curl https://production"}),
    ("tool.exception", {}),
    ("tool.exception", {"error_type": "transient", "message": "x", "retryable": "yes"}),
    ("tool.empty_response", {"response": "undefined"}),
    ("tool.timeout", {"simulated_latency_ms": True}),
    ("tool.timeout", {"simulated_latency_ms": -1}),
    ("tool.timeout", {"simulated_latency_ms": 120000, "jitter_ms": 1}),
    ("tool.timeout", {"simulated_latency_ms": 1.1}),
    ("tool.timeout", {"dispatch": "original"}),
    ("stream.truncate", {"max_bytes": MAX_PAYLOAD_BYTES + 1}),
    ("stream.truncate", {"max_bytes": -1}),
    ("tool.malformed_response", {"text": "x" * (MAX_PAYLOAD_BYTES + 1)}),
    ("prompt.injection", {"text": "\ud800"}),
    ("json_pointer.patch", {"pointer": "/", "value": float("nan")}),
])
def test_typed_parameters_fail_closed(fault_type, parameters):
    with pytest.raises(ValueError):
        FaultPlan.model_validate(plan_data(fault_type, parameters))


@pytest.mark.parametrize("path", [
    "../secret", "/etc/passwd", "root/../secret", "root//call", "root/",
    "https://example.com", r"root\call", "root/%2e%2e", "root/*", "", "root/\x00",
])
def test_selectors_are_exact_logical_paths_not_files_or_urls(path):
    with pytest.raises(ValueError):
        FaultPlan.model_validate(plan_data(path=path))


@pytest.mark.parametrize("pointer", [
    "relative", "/a~2b", "/a~", "/../secret", "/__proto__/x",
    "/constructor/prototype", "/" * 34, "/" + "x" * 1024,
])
def test_unsafe_pointers_rejected(pointer):
    with pytest.raises(ValueError):
        FaultPlan.model_validate(plan_data("json_pointer.patch", {"pointer": pointer, "value": 1}))


def test_conflicts_duplicate_ids_and_forged_evidence_rejected():
    data = plan_data()
    data["faults"] *= 2
    with pytest.raises(ValueError, match="Duplicate fault"):
        FaultPlan.model_validate(data)
    data["faults"][1] = data["faults"][1] | {"fault_id": "other"}
    with pytest.raises(ValueError, match="Conflicting"):
        FaultPlan.model_validate(data)
    data = plan_data()
    data["faults"][0]["applied"] = True
    with pytest.raises(ValueError):
        FaultPlan.model_validate(data)


def test_strict_json_parser_rejects_duplicate_keys_and_resource_exhaustion():
    for data in [
        '{"fault_plan_version":"1","fault_plan_version":"2","faults":[]}',
        '{"fault_plan_version":"1","faults":[],"x":{"a":1,"a":2}}',
        '{"fault_plan_version":"1","faults":[],"x":NaN}',
        "[" * 2000 + "]" * 2000,
        " " * (MAX_PLAN_BYTES + 1),
    ]:
        with pytest.raises(ValueError):
            parse_plan(data)


@pytest.mark.parametrize("value", [
    1.0, float("inf"), MAX_SAFE_INTEGER + 1, -(MAX_SAFE_INTEGER + 1),
    b"binary", {1: "not-string"}, ("tuple",), {"a": {1, 2}},
])
def test_unsupported_json_values(value):
    with pytest.raises(ValueError):
        canonical(value)


def test_depth_nodes_cycles_and_escaped_byte_limits():
    nested = 0
    for _ in range(34):
        nested = [nested]
    cyclic = []
    cyclic.append(cyclic)
    for value in [nested, cyclic, [0] * 10001, "\x00" * (MAX_PAYLOAD_BYTES // 6 + 1)]:
        with pytest.raises(ValueError):
            canonical(value)


def test_canonical_json_uses_utf16_key_order_and_safe_integer_boundaries():
    assert canonical({"\ue000": 1, "\U00010000": 2}) == '{"\U00010000":2,"\ue000":1}'.encode()
    assert canonical({"b": MAX_SAFE_INTEGER, "a": None}) == b'{"a":null,"b":9007199254740991}'
    assert canonical("\n") == b'"\\n"'


def test_fixture_arguments_and_response_are_json_only():
    request = {
        "target_invocation_path": "root/tool", "tool_name": "lookup",
        "schema_digest": "0" * 64, "arguments": {},
    }
    with pytest.raises(ValueError):
        Invocation.model_validate(request | {"arguments": {"amount": 1.5}})
    invocation = Invocation.model_validate(request)
    with pytest.raises(ValueError):
        MockFixture(request=invocation, response={"callable": lambda: None})


def test_dictionary_order_does_not_change_plan_digest():
    data = plan_data("json_pointer.patch", {"pointer": "/value", "value": {"z": 1, "a": 2}})
    left = parse_plan(json.dumps(data))
    right = parse_plan(json.dumps(data, sort_keys=True))
    assert left.digest == right.digest


def test_aliased_oversized_input_is_rejected_before_full_serialization():
    shared = "x" * (MAX_PAYLOAD_BYTES // 2)
    with pytest.raises(ValueError, match="byte limit"):
        canonical([shared] * 9000)


def test_canonical_byte_budget_includes_punctuation_escapes_and_utf8():
    for value, expected in [
        ({"a": [True, False, None]}, b'{"a":[true,false,null]}'),
        ({"": "\u20ac"}, '{"":"\u20ac"}'.encode()),
        ({"a": '"\n'}, b'{"a":"\\"\\n"}'),
    ]:
        assert canonical(value, limit=len(expected)) == expected
        with pytest.raises(ValueError, match="byte limit"):
            canonical(value, limit=len(expected) - 1)
