"""Project fault execution evidence into explicit, replay-ineligible event payloads."""

from uuid import UUID

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
