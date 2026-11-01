"""Durable, audited deletion of a run or recording and everything derived
from it.

Deletion is the one operation that cannot be undone and cannot be verified
afterwards from the data it destroyed, so it is done in a single transaction
that also writes the audit row. If the transaction fails, nothing is deleted
and no audit row claims otherwise; if it commits, the evidence that it
happened commits with it.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import text

from . import audit
from .database import project_connection
from .identifiers import new_uuid7
from .jobs import JobQueue


class DeletionError(ValueError):
    """The deletion cannot be carried out. The message names no payload."""


async def _recordings_of_run(connection, project_id: UUID, run_id: UUID) -> list[UUID]:
    rows = await connection.execute(text("""
        SELECT id FROM recordings WHERE project_id = :project AND run_id = :run
    """), {"project": project_id, "run": run_id})
    return [row[0] for row in rows]


async def plan(connection, project_id: UUID, target_kind: str, target_id: UUID) -> dict:
    """Return what deleting this target would destroy, without destroying it.

    Separated from the deletion itself so the graph can be asserted on, and so
    the number of bytes recorded in the audit row is measured from the rows
    that were actually removed rather than estimated.
    """
    if target_kind == "run":
        runs = [target_id]
        recordings = await _recordings_of_run(connection, project_id, target_id)
    elif target_kind == "recording":
        runs = []
        recordings = [target_id]
    else:
        # Suites have no storage yet; the route rejects them before a job
        # exists, and a worker must not invent a meaning for one.
        raise DeletionError("Unsupported deletion target")

    comparisons = [row[0] for row in await connection.execute(text("""
        SELECT id FROM comparisons
        WHERE project_id = :project
          AND (baseline_recording_id = ANY(CAST(:recordings AS uuid[])) OR candidate_run_id = ANY(CAST(:runs AS uuid[])))
    """), {"project": project_id, "recordings": recordings, "runs": runs})]
    exports = [row[0] for row in await connection.execute(text("""
        SELECT job_id FROM archive_exports
        WHERE project_id = :project AND recording_id = ANY(CAST(:recordings AS uuid[]))
    """), {"project": project_id, "recordings": recordings})]
    size = (await connection.execute(text("""
        SELECT coalesce(sum(octet_length(payload::text)), 0)
             + coalesce((SELECT sum(size_bytes) FROM artifacts
                         WHERE project_id = :project AND run_id = ANY(CAST(:runs AS uuid[]))), 0)
        FROM events WHERE project_id = :project AND run_id = ANY(CAST(:runs AS uuid[]))
    """), {"project": project_id, "runs": runs})).scalar_one()
    return {"runs": runs, "recordings": recordings, "comparisons": comparisons,
            "exports": exports, "bytes": int(size or 0)}


async def _destroy(connection, project_id: UUID, graph: dict) -> None:
    parameters = {"project": project_id, "runs": graph["runs"],
                  "recordings": graph["recordings"],
                  "comparisons": graph["comparisons"], "exports": graph["exports"]}
    # Ordered from derived data back to its source: every statement removes
    # rows that the next statement's targets are referenced by.
    for statement in (
        "DELETE FROM archive_exports WHERE project_id = :project AND job_id = ANY(CAST(:exports AS uuid[]))",
        "DELETE FROM comparison_divergences WHERE project_id = :project"
        " AND comparison_id = ANY(CAST(:comparisons AS uuid[]))",
        "DELETE FROM comparisons WHERE project_id = :project AND id = ANY(CAST(:comparisons AS uuid[]))",
        "DELETE FROM recordings WHERE project_id = :project AND id = ANY(CAST(:recordings AS uuid[]))",
        "DELETE FROM seal_requests WHERE project_id = :project AND run_id = ANY(CAST(:runs AS uuid[]))",
        "DELETE FROM runs WHERE project_id = :project AND id = ANY(CAST(:runs AS uuid[]))",
    ):
        await connection.execute(text(statement), parameters)


async def process_deletion(engine, project_id: UUID, job_id: UUID) -> bool:
    """Carry out one queued deletion job under a lease."""
    queue = JobQueue(engine, timedelta(seconds=30))
    token = new_uuid7()
    if await queue.acquire_job(project_id, job_id, token) is None:
        return False
    async with project_connection(engine, project_id) as connection:
        request = (await connection.execute(text("""
            SELECT id, target_kind, target_id, audit_event_id FROM deletion_requests
            WHERE project_id = :project AND job_id = :job
        """), {"project": project_id, "job": job_id})).mappings().first()
    if request is None:
        await queue.complete(project_id, job_id, token, "failed")
        return False
    if request["audit_event_id"] is not None:
        # Already carried out; a retried lease must not delete a second graph
        # or write a second audit row for the same request.
        await queue.complete(project_id, job_id, token, "succeeded")
        return True
    try:
        async with project_connection(engine, project_id) as connection:
            graph = await plan(connection, project_id,
                               request["target_kind"], request["target_id"])
            await _destroy(connection, project_id, graph)
            audit_id = await audit.record(
                connection, project_id, kind="delete", reason="user_request",
                root_ids=[request["target_id"]],
                resource_ids=graph["runs"] + graph["recordings"] + graph["comparisons"],
                size_bytes=graph["bytes"])
            await connection.execute(text("""
                UPDATE deletion_requests SET audit_event_id = :audit
                WHERE project_id = :project AND id = :id
            """), {"audit": audit_id, "project": project_id, "id": request["id"]})
    except Exception as failure:
        # Nothing was deleted: the transaction that would have deleted it was
        # rolled back by the context manager before this ran. The recorded
        # error names the failure's type and not its message, because a
        # database error message can quote the row that caused it.
        async with project_connection(engine, project_id) as connection:
            await connection.execute(text("""
                UPDATE jobs SET error = :error
                WHERE id = :job AND project_id = :project AND lease_token = :token
            """), {"job": job_id, "project": project_id, "token": token,
                   "error": "deletion failed: " + type(failure).__name__})
        try:
            await queue.complete(project_id, job_id, token, "failed")
        except ValueError:
            return False
        return False
    await queue.complete(project_id, job_id, token, "succeeded")
    return True
