import json
from uuid import UUID

import pytest

from anvil_server.runner_events import postgres_event_sink, run_persisted_recorded_session
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


@pytest.mark.asyncio
async def test_persisted_session_routes_batches_to_capture_ingest(monkeypatch, tmp_path):
    stored = []

    async def ingest(engine, project, run, stream, events):
        stored.append((engine, project, run, stream, events))

    async def isolated(launch, directory, seccomp, **options):
        await options["persist_events"](batch())
        return 1

    monkeypatch.setattr("anvil_server.runner_events.accept_batch", ingest)
    monkeypatch.setattr("anvil_server.runner_events.prepare_runner_socket_directory", lambda path: path.mkdir())
    monkeypatch.setattr("anvil_server.runner_events.run_isolated_recorded_session", isolated)
    result = await run_persisted_recorded_session(
        "engine", PROJECT, "launch", tmp_path / "uds", tmp_path / "profile",
        adapter="google-adk", config="config", fixtures="fixtures",
    )
    assert result == 1
    assert stored[0][:4] == ("engine", PROJECT, UUID(RUN), UUID(STREAM))
    assert not (tmp_path / "uds").exists()


@pytest.mark.asyncio
async def test_persisted_session_reclaims_directory_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("anvil_server.runner_events.prepare_runner_socket_directory", lambda path: path.mkdir())

    async def isolated(*args, **kwargs):
        raise IsolationUnavailable("runner failed")

    monkeypatch.setattr("anvil_server.runner_events.run_isolated_recorded_session", isolated)
    with pytest.raises(IsolationUnavailable, match="runner failed"):
        await run_persisted_recorded_session(
            "engine", PROJECT, "launch", tmp_path / "uds", tmp_path / "profile",
            adapter="google-adk", config="config", fixtures="fixtures",
        )
    assert not (tmp_path / "uds").exists()
