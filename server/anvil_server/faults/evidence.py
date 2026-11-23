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
