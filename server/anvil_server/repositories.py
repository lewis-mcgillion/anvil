"""Project-scoped persistence with no ambient privileged query methods."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection


class RunRepository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def create(self, project_id: UUID, run_id: UUID, status: str = "running") -> None:
        async with project_connection(self.engine, project_id) as connection:
            await connection.execute(text("""
                INSERT INTO runs (id, project_id, execution_outcome)
                VALUES (:run, :project, :status)
            """), {"run": run_id, "project": project_id, "status": status})

    async def get(self, project_id: UUID, run_id: UUID) -> dict | None:
        async with project_connection(self.engine, project_id) as connection:
            row = (await connection.execute(text("""
                SELECT id, project_id, execution_outcome, capture_status,
                       agent_name, adapter, adapter_version, trace_id, started_at,
                       finished_at, created_at FROM runs
                WHERE project_id = :project AND id = :run
            """), {"project": project_id, "run": run_id})).mappings().first()
            return dict(row) if row else None

    async def update_outcome(self, project_id: UUID, run_id: UUID, status: str) -> bool:
        async with project_connection(self.engine, project_id) as connection:
            result = await connection.execute(text("""
                UPDATE runs SET execution_outcome = :status
                WHERE project_id = :project AND id = :run
            """), {"project": project_id, "run": run_id, "status": status})
            return result.rowcount == 1

    async def delete(self, project_id: UUID, run_id: UUID) -> bool:
        async with project_connection(self.engine, project_id) as connection:
            result = await connection.execute(text("""
                DELETE FROM runs WHERE project_id = :project AND id = :run
            """), {"project": project_id, "run": run_id})
            return result.rowcount == 1


class EventRepository:
    """Read-only access to immutable captured events for playback.

    This repository deliberately exposes no write method. Playback must never
    be able to mutate a recording, schedule work or reach an execution surface,
    so the only operation is an ordered read within one project scope.
    """

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def list_for_run(self, project_id: UUID, run_id: UUID) -> list[dict]:
        async with project_connection(self.engine, project_id) as connection:
            rows = (await connection.execute(text("""
                SELECT event_id, run_id, stream_id, sequence, event_type AS type,
                       occurred_at, trace_id, span_id, parent_event_ids,
                       invocation_id, payload
                FROM events WHERE project_id = :project AND run_id = :run
                ORDER BY stream_id, sequence
            """), {"project": project_id, "run": run_id})).mappings().all()
        return [{key: json_safe(value) for key, value in row.items()} for row in rows]


def json_safe(value):
    """Coerce driver-native values into JSON primitives.

    Canonicalization and projection hashing accept JSON types only, so UUID and
    timestamp columns are converted here rather than silently failing later.
    Timestamps are rendered in UTC with a ``Z`` suffix so a recording hashes
    identically regardless of the session time zone that read it.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        moment = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return value
