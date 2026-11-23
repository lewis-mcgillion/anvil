"""Persist supervisor event batches through the normal capture policy."""

import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from .events import Event, accept_batch
from .runner_policy import IsolationUnavailable
from .runner_policy import RunnerLaunch
from .runner_pb2 import SupervisorMessage
from .runner_fixtures import RecordedRunnerFixtures
from .runner_supervisor import run_isolated_recorded_session


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


async def run_persisted_recorded_session(
    engine: AsyncEngine, project_id: UUID, launch: RunnerLaunch,
    socket_directory: Path, seccomp_profile: Path, *, adapter: str,
    config: SupervisorMessage, fixtures: RecordedRunnerFixtures,
    timeout_seconds: float = 60,
) -> int:
    return await run_isolated_recorded_session(
        launch, socket_directory, seccomp_profile, adapter=adapter,
        config=config, fixtures=fixtures,
        persist_events=postgres_event_sink(engine, project_id),
        timeout_seconds=timeout_seconds,
    )
