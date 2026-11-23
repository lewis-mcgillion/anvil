"""Persist supervisor event batches through the normal capture policy."""

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from .events import Event, accept_batch
from .runner_policy import IsolationUnavailable


def postgres_event_sink(engine: AsyncEngine, project_id: UUID):
    async def persist(batch) -> None:
        try:
            run_id = UUID(batch.run_id)
            stream_id = UUID(batch.stream_id)
            events = [Event.model_validate({
                "schema_version": event.schema_version,
                "event_id": event.event_id,
                "run_id": event.run_id,
                "stream_id": event.stream_id,
                "sequence": event.sequence,
                "type": event.type,
                "occurred_at": event.occurred_at,
                "trace_id": event.trace_id or None,
                "span_id": event.span_id or None,
                "parent_event_ids": list(event.parent_event_ids),
                "invocation_id": event.invocation_id or None,
                "payload": json.loads(event.payload),
            }) for event in batch.events]
        except (ValueError, UnicodeError, TypeError) as error:
            raise IsolationUnavailable("Runner event batch failed schema validation") from error
        await accept_batch(engine, project_id, run_id, stream_id, events)

    return persist
