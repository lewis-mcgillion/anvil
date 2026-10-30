"""Atomic immutable event ingest into the canonical PostgreSQL schema."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection

MAX_BATCH_EVENTS = 500
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_RUN_BYTES = 64 * 1024 * 1024


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern="^1$")
    event_id: UUID
    run_id: UUID
    stream_id: UUID
    sequence: int = Field(ge=0)
    type: str = Field(pattern=r"^(run\.(started|completed|failed)|message\.created|model\.(requested|chunk|completed|failed)|tool\.(requested|completed|failed)|state\.checkpoint|capture\.gap|fault\.applied)$")
    occurred_at: datetime
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    parent_event_ids: list[UUID] = Field(default_factory=list)
    invocation_id: UUID | None = None
    payload: dict
    replay_safe: bool = False

    @field_validator("event_id", "run_id", "stream_id", "invocation_id")
    @classmethod
    def require_uuidv7(cls, identifier):
        if identifier is not None and identifier.version != 7:
            raise ValueError("Expected UUIDv7")
        return identifier


class Conflict(ValueError):
    pass


def event_hash(event: Event) -> bytes:
    payload = event.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()).digest()


def validate_batch(run_id: UUID, stream_id: UUID, events: list[Event]) -> None:
    if not events or len(events) > MAX_BATCH_EVENTS:
        raise ValueError("Invalid batch size")
    ids = set()
    total = 0
    for event in events:
        if event.run_id != run_id or event.stream_id != stream_id or event.event_id in ids:
            raise Conflict("Event identity or stream mismatch")
        ids.add(event.event_id)
        size = len(event.model_dump_json().encode())
        total += size
        if size > MAX_EVENT_BYTES or total > MAX_BATCH_BYTES:
            raise ValueError("Event payload exceeds ingest limit")


async def accept_batch(engine: AsyncEngine, project_id: UUID, run_id: UUID,
                       stream_id: UUID, events: list[Event]) -> dict[str, int]:
    validate_batch(run_id, stream_id, events)
    accepted = duplicates = 0
    async with project_connection(engine, project_id) as connection:
        status = (await connection.execute(text("""
            SELECT capture_status FROM runs WHERE id = :run AND project_id = :project FOR UPDATE
        """), {"run": run_id, "project": project_id})).scalar_one_or_none()
        if status != "open":
            raise Conflict("Run is sealed or unavailable")
        row = (await connection.execute(text("""
            SELECT next_sequence FROM run_streams
            WHERE project_id = :project AND run_id = :run AND id = :stream FOR UPDATE
        """), {"project": project_id, "run": run_id, "stream": stream_id})).first()
        if row is None:
            raise ValueError("Unknown or inaccessible stream")
        current_bytes = (await connection.execute(text("""
            SELECT COALESCE((SELECT sum(octet_length(payload::text)) FROM events
                     WHERE project_id = :project AND run_id = :run), 0)
                 + COALESCE((SELECT sum(size_bytes) FROM artifacts
                     WHERE project_id = :project AND run_id = :run), 0)
        """), {"project": project_id, "run": run_id})).scalar_one()
        new_bytes = sum(len(json.dumps(event.payload, separators=(",", ":")).encode()) for event in events)
        if current_bytes + new_bytes > MAX_RUN_BYTES:
            raise ValueError("Run storage limit exceeded")
        sequence = row.next_sequence
        for event in events:
            digest = event_hash(event)
            existing = (await connection.execute(text("""
                SELECT content_hash FROM events WHERE project_id = :project AND event_id = :event
            """), {"project": project_id, "event": event.event_id})).scalar_one_or_none()
            if existing is not None:
                if existing != digest:
                    raise Conflict("Event identity reused with different contents")
                duplicates += 1
                continue
            if event.sequence != sequence:
                raise Conflict("Sequence mismatch")
            await connection.execute(text("""
                INSERT INTO events (event_id, project_id, run_id, stream_id, sequence,
                  schema_version, event_type, occurred_at, trace_id, span_id,
                  parent_event_ids, invocation_id, payload, content_hash, replay_safe)
                VALUES (:event, :project, :run, :stream, :sequence, '1', :type,
                  :occurred_at, :trace_id, :span_id, CAST(:parents AS jsonb),
                  :invocation, CAST(:payload AS jsonb), :hash, :replay_safe)
            """), {"event": event.event_id, "project": project_id, "run": run_id,
                   "stream": stream_id, "sequence": event.sequence, "type": event.type,
                   "occurred_at": event.occurred_at, "trace_id": event.trace_id,
                   "span_id": event.span_id, "parents": json.dumps([str(item) for item in event.parent_event_ids]),
                   "invocation": event.invocation_id, "payload": json.dumps(event.payload),
                   "hash": digest, "replay_safe": event.replay_safe})
            sequence += 1
            accepted += 1
        if accepted:
            await connection.execute(text("""
                UPDATE run_streams SET next_sequence = :sequence
                WHERE project_id = :project AND id = :stream
            """), {"sequence": sequence, "project": project_id, "stream": stream_id})
    return {"accepted": accepted, "duplicates": duplicates}
