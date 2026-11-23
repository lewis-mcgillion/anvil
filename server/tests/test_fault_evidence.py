import pytest

from anvil_server.faults import FaultEngine, FaultPlan, Invocation, MockFixture
from anvil_server.faults.evidence import fault_applied_payload, fault_report_payload, load_fault_report, persist_fault_report, suite_fault_observations, unreached_fault_ids
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
    observations = suite_fault_observations(report)
    assert [(item.fault_id, item.applied) for item in observations] == [("first", True), ("second", False)]


@pytest.mark.asyncio
async def test_fault_report_persistence_uses_project_scope(monkeypatch):
    from contextlib import asynccontextmanager

    captured = []

    class Connection:
        async def execute(self, statement, parameters):
            captured.append(parameters)

    @asynccontextmanager
    async def connection(engine, project_id):
        assert project_id == identifiers[0]
        yield Connection()

    monkeypatch.setattr("anvil_server.faults.evidence.project_connection", connection)
    identifiers = tuple(new_uuid7() for _ in range(3))
    report = FaultEngine(FaultPlan.model_validate({"fault_plan_version": "1", "faults": []}), [], seed=1).finish()
    await persist_fault_report(None, *identifiers, report)
    assert captured[0]["project"] == identifiers[0]
    assert captured[0]["run"] == identifiers[1]
    assert captured[0]["digest"] == report.digest


def test_fault_report_migration_enforces_project_isolation():
    from pathlib import Path

    migration = (Path(__file__).parents[2] / "migrations/0009_fault_reports.sql").read_text()
    assert "ALTER TABLE fault_reports FORCE ROW LEVEL SECURITY" in migration
    assert "CREATE POLICY fault_reports_project_isolation" in migration


@pytest.mark.asyncio
async def test_fault_report_read_rejects_digest_mismatch(monkeypatch):
    from contextlib import asynccontextmanager

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"report": {"report_sha256": "0" * 64,
                               "applied_fault_ids": [], "unreached_fault_ids": []},
                    "report_digest": "1" * 64}

    class Connection:
        async def execute(self, *args):
            return Result()

    @asynccontextmanager
    async def connection(engine, project):
        yield Connection()

    monkeypatch.setattr("anvil_server.faults.evidence.project_connection", connection)
    with pytest.raises(ValueError, match="metadata"):
        await load_fault_report(None, *(new_uuid7() for _ in range(3)))
