import json
from uuid import UUID

import pytest

from anvil_server.runner_events import postgres_event_sink
from anvil_server.runner_pb2 import RunnerMessage
from anvil_server.runner_policy import IsolationUnavailable


RUN = "0199b167-0000-7000-8000-000000000021"
STREAM = "0199b167-0000-7000-8000-000000000031"
EVENT = "0199b167-0000-7000-8000-000000000041"
PROJECT = UUID("0199b167-0000-7000-8000-000000000011")


def batch(payload=b'{"message":"hello"}'):
    return RunnerMessage(emit_events={
        "run_id": RUN, "stream_id": STREAM, "events": [{
            "schema_version": "1", "event_id": EVENT, "run_id": RUN,
            "stream_id": STREAM, "sequence": 0, "type": "message.created",
            "occurred_at": "2026-01-01T00:00:00Z", "payload": payload,
        }],
    }).emit_events


@pytest.mark.asyncio
async def test_runner_event_sink_uses_capture_ingest(monkeypatch):
    calls = []

    async def ingest(engine, project, run, stream, events):
        calls.append((engine, project, run, stream, events))

    monkeypatch.setattr("anvil_server.runner_events.accept_batch", ingest)
    await postgres_event_sink("engine", PROJECT)(batch())
    assert len(calls) == 1
    assert calls[0][:4] == ("engine", PROJECT, UUID(RUN), UUID(STREAM))
    assert calls[0][4][0].payload == {"message": "hello"}


@pytest.mark.asyncio
async def test_runner_event_sink_rejects_invalid_payload(monkeypatch):
    async def ingest(*args):
        pytest.fail("invalid events must never reach storage")

    monkeypatch.setattr("anvil_server.runner_events.accept_batch", ingest)
    with pytest.raises(IsolationUnavailable, match="schema validation"):
        await postgres_event_sink("engine", PROJECT)(batch(b"not-json"))
