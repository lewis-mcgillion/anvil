import pytest

from anvil_server.faults import FaultEngine, FaultPlan, Invocation, MockFixture
from anvil_server.faults.evidence import fault_applied_payload, fault_report_payload, unreached_fault_ids
from anvil_server.identifiers import new_uuid7


def test_applied_and_unreached_faults_remain_distinct():
    plan = FaultPlan.model_validate({"fault_plan_version": "1", "faults": [
        {"fault_id": "first", "fault_type": "tool.empty_response",
         "target_invocation_path": "root/first", "parameters": {"response": "object"}},
        {"fault_id": "second", "fault_type": "tool.empty_response",
         "target_invocation_path": "root/second", "parameters": {"response": "object"}},
    ]})
    request = Invocation.model_validate({"target_invocation_path": "root/first",
                                          "tool_name": "lookup", "schema_digest": "a" * 64,
                                          "arguments": {}})
    engine = FaultEngine(plan, [MockFixture(request=request, response={})], seed=1)
    engine.invoke(request)
    report = engine.finish()
    assert unreached_fault_ids(report) == ("second",)
    payload = fault_applied_payload(report.evidence[0], new_uuid7())
    assert payload["fault_id"] == "first"
    assert payload["replay_safe"] is False
    with pytest.raises(ValueError):
        fault_applied_payload(report.evidence[1], new_uuid7())
    projected = fault_report_payload(report)
    assert projected["applied_fault_ids"] == ["first"]
    assert projected["unreached_fault_ids"] == ["second"]
    assert projected["all_faults_applied"] is False
    assert projected["report_sha256"] == report.digest
