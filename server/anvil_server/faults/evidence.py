"""Project fault execution evidence into explicit, replay-ineligible event payloads."""

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from anvil_server.database import project_connection
from anvil_server.suites.assertions import FaultObservation

from .engine import Evidence, FaultReport


def fault_applied_payload(evidence: Evidence, invocation_id: UUID) -> dict:
    if evidence.status != "applied" or invocation_id.version != 7:
        raise ValueError("Only applied faults with UUIDv7 invocations emit events")
    return {
        "fault_id": evidence.fault_id,
        "fault_type": evidence.fault_type,
        "target_invocation_id": str(invocation_id),
        "simulated_latency_ms": evidence.simulated_latency_ms,
        "redactions": [],
        "replay_safe": False,
    }


def unreached_fault_ids(report: FaultReport) -> tuple[str, ...]:
    return tuple(item.fault_id for item in report.evidence if item.status == "unreached")


def fault_report_payload(report: FaultReport) -> dict:
    """Return serializable conditions without treating unreached faults as success."""
    return {
        "plan_hash": report.plan_hash,
        "seed": report.seed,
        "execution_status": report.status,
        "calls_consumed": report.calls_consumed,
        "simulated_time_ms": report.simulated_time_ms,
        "applied_fault_ids": [item.fault_id for item in report.evidence if item.status == "applied"],
        "unreached_fault_ids": list(unreached_fault_ids(report)),
        "unused_fixture_paths": list(report.unused_fixture_paths),
        "all_faults_applied": report.all_faults_applied,
        "report_sha256": report.digest,
    }


def suite_fault_observations(report: FaultReport) -> list[FaultObservation]:
    return [FaultObservation(fault_id=item.fault_id, applied=item.status == "applied")
            for item in report.evidence]


async def persist_fault_report(engine: AsyncEngine, project_id: UUID, run_id: UUID,
                               report_id: UUID, report: FaultReport) -> None:
    if any(not isinstance(identifier, UUID) or identifier.version != 7
           for identifier in (project_id, run_id, report_id)):
        raise ValueError("Fault report requires project, run and report UUIDv7 identities")
    payload = fault_report_payload(report)
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            INSERT INTO fault_reports (id, project_id, run_id, report, report_digest)
            VALUES (:id, :project, :run, CAST(:report AS jsonb), :digest)
            ON CONFLICT (project_id, run_id, report_digest) DO NOTHING
        """), {"id": report_id, "project": project_id, "run": run_id,
               "report": json.dumps(payload, sort_keys=True), "digest": report.digest})


async def load_fault_report(engine: AsyncEngine, project_id: UUID, run_id: UUID,
                            report_id: UUID) -> dict | None:
    if any(not isinstance(identifier, UUID) or identifier.version != 7
           for identifier in (project_id, run_id, report_id)):
        raise ValueError("Fault report lookup requires UUIDv7 identities")
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT report, report_digest FROM fault_reports
            WHERE project_id = :project AND run_id = :run AND id = :id
        """), {"project": project_id, "run": run_id, "id": report_id})).mappings().first()
    if row is None:
        return None
    report = row["report"]
    if (not isinstance(report, dict) or report.get("report_sha256") != row["report_digest"]
            or not isinstance(report.get("applied_fault_ids"), list)
            or not isinstance(report.get("unreached_fault_ids"), list)):
        raise ValueError("Stored fault report metadata is invalid")
    return report
