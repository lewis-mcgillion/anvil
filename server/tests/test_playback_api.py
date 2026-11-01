"""The playback HTTP surface is a pure read: no jobs, no provider calls.

These tests stand the FastAPI app up against stub repositories so the route
logic can be exercised without PostgreSQL, while real execution surfaces are
replaced with tripwires that fail the test if playback ever touches them.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from anvil_server import app as app_module
from anvil_server import jobs as jobs_module
from anvil_server.app import app
from anvil_server.playback import ProjectionIndex, projection_hash
from anvil_server.repositories import EventRepository, json_safe
from anvil_server.security import AccessDenied

PROJECT = uuid.UUID("0199b167-0000-7000-8000-000000000011")
RUN = uuid.UUID("0199b167-0000-7000-8000-000000000021")
STREAM_A = "0199b167-0000-7000-8000-0000000000a1"
STREAM_B = "0199b167-0000-7000-8000-0000000000b1"
AUTH = {"authorization": "Bearer test-token"}


def event(event_id, sequence, event_type, *, stream=STREAM_A, parents=(),
          invocation=None, payload=None, occurred_at="2026-01-01T00:00:00.000Z"):
    return {
        "event_id": event_id,
        "run_id": str(RUN),
        "stream_id": stream,
        "sequence": sequence,
        "type": event_type,
        "occurred_at": occurred_at,
        "parent_event_ids": list(parents),
        "invocation_id": invocation,
        "payload": payload or {},
    }


def recording():
    return [
        event("e0", 0, "run.started"),
        event("e1", 1, "message.created", payload={"role": "user", "text": "hello"}),
        event("e2", 2, "tool.requested", invocation="inv-1",
              payload={"tool_name": "inventory.lookup", "arguments": {"sku": "ABC"}}),
        event("e3", 3, "tool.completed", invocation="inv-1", parents=("e2",),
              payload={"tool_name": "inventory.lookup", "result": {"available": True}}),
        event("e4", 4, "run.completed", parents=("e3",)),
    ]


class StubRunRepository:
    def __init__(self, engine):
        self.engine = engine

    async def get(self, project_id, run_id):
        if run_id != RUN:
            return None
        return {"id": run_id, "project_id": project_id, "execution_outcome": "completed"}


class StubEventRepository:
    events: list = []
    calls: list = []

    def __init__(self, engine):
        self.engine = engine

    async def list_for_run(self, project_id, run_id):
        type(self).calls.append((project_id, run_id))
        return [dict(item) for item in type(self).events]


class ExplodingBroker:
    """Any attribute access means playback reached a model provider."""

    def __getattr__(self, name):
        raise AssertionError(f"playback contacted the model provider: {name}")


@pytest.fixture
def client(monkeypatch):
    StubEventRepository.events = recording()
    StubEventRepository.calls = []

    async def allow(engine, request, project_id, required):
        assert required == "runs:read"
        return None

    monkeypatch.setattr(app_module, "authorize", allow)
    monkeypatch.setattr(app_module, "RunRepository", StubRunRepository)
    monkeypatch.setattr(app_module, "EventRepository", StubEventRepository)

    def refuse(*args, **kwargs):
        raise AssertionError("playback scheduled background work")

    for name in ("create", "acquire", "complete", "cancel"):
        monkeypatch.setattr(jobs_module.JobQueue, name, refuse, raising=False)

    with TestClient(app) as test_client:
        # The lifespan assigns state.engine, so the stub must replace it after startup.
        app.state.engine = object()
        try:
            yield test_client
        finally:
            app.state.engine = None


def projection_url(run=RUN, project=PROJECT):
    return f"/v1/projects/{project}/runs/{run}/projection"


def streams_url(run=RUN, project=PROJECT):
    return f"/v1/projects/{project}/runs/{run}/streams"


def test_projection_returns_explicit_recorded_mode_labels(client):
    body = client.get(projection_url(), headers=AUTH).json()
    assert body["mode"] == "recorded_playback"
    assert body["executes_agent_code"] is False
    assert body["executes_tools"] is False
    assert body["contacts_model_provider"] is False
    assert body["schedules_jobs"] is False


def test_projection_matches_direct_projection_hash(client):
    body = client.get(projection_url(), headers=AUTH).json()
    expected = ProjectionIndex(recording(), run_id=str(RUN)).full()
    assert body["projection"] == expected
    assert body["projection_hash"] == projection_hash(expected)


def test_projection_reports_position_and_cursors(client):
    body = client.get(projection_url(), headers=AUTH).json()
    assert body["position"] == 5
    assert body["event_count"] == 5
    assert body["cursor"] == "e4"
    assert body["next_cursor"] is None
    assert body["checkpoint_positions"] == [0]


def test_projection_seek_matches_prefix_projection(client):
    body = client.get(projection_url(), params={"cursor": "e2"}, headers=AUTH).json()
    index = ProjectionIndex(recording(), run_id=str(RUN))
    assert body["position"] == 3
    assert body["projection"] == index.at(3)
    assert body["next_cursor"] == "e3"


@pytest.mark.parametrize("cursor", ["e0", "e1", "e2", "e3", "e4"])
def test_every_cursor_is_seekable(client, cursor):
    response = client.get(projection_url(), params={"cursor": cursor}, headers=AUTH)
    assert response.status_code == 200
    index = ProjectionIndex(recording(), run_id=str(RUN))
    assert response.json()["projection"] == index.at(index.position_of(cursor))


def test_unknown_cursor_is_rejected(client):
    response = client.get(projection_url(), params={"cursor": "nope"}, headers=AUTH)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "invalid_cursor"


def test_unsupported_projection_version_is_rejected(client):
    response = client.get(projection_url(), params={"projection_version": "99"}, headers=AUTH)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "unsupported_projection_version"


def test_unsupported_version_is_rejected_before_any_read(client):
    client.get(projection_url(), params={"projection_version": "99"}, headers=AUTH)
    assert StubEventRepository.calls == []


def test_repeated_requests_are_byte_identical(client):
    first = client.get(projection_url(), headers=AUTH)
    second = client.get(projection_url(), headers=AUTH)
    assert first.content == second.content


def test_timestamp_order_never_changes_the_projection(client):
    reversed_times = []
    for position, item in enumerate(recording()):
        item = dict(item)
        item["occurred_at"] = f"2026-01-01T00:00:{59 - position:02d}.000Z"
        reversed_times.append(item)
    baseline = client.get(projection_url(), headers=AUTH).json()["projection_hash"]
    StubEventRepository.events = reversed_times
    assert client.get(projection_url(), headers=AUTH).json()["projection_hash"] == baseline


def test_playback_schedules_no_jobs_and_calls_no_provider(client, monkeypatch):
    monkeypatch.setattr(app_module, "RunRepository", StubRunRepository)
    app.state.broker = ExplodingBroker()
    try:
        assert client.get(projection_url(), headers=AUTH).status_code == 200
        assert client.get(streams_url(), headers=AUTH).status_code == 200
    finally:
        del app.state.broker


def test_unprojectable_recording_is_surfaced_not_partially_served(client):
    StubEventRepository.events = [event("e9", 9, "run.completed", parents=("missing",))]
    response = client.get(projection_url(), headers=AUTH)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "unprojectable_recording"
    assert "projection" not in response.json()


def test_unknown_run_is_not_found(client):
    response = client.get(projection_url(run=uuid.uuid4()), headers=AUTH)
    assert response.status_code == 404


def test_denied_access_is_not_leaked_as_an_empty_projection(client, monkeypatch):
    async def deny(engine, request, project_id, required):
        raise AccessDenied("Permission denied")

    monkeypatch.setattr(app_module, "authorize", deny)
    response = client.get(projection_url(), headers=AUTH)
    assert response.status_code == 403
    assert "projection" not in response.json()


def test_denied_access_never_reads_events(client, monkeypatch):
    async def deny(engine, request, project_id, required):
        raise AccessDenied("Permission denied")

    monkeypatch.setattr(app_module, "authorize", deny)
    client.get(projection_url(), headers=AUTH)
    assert StubEventRepository.calls == []


def test_playback_fails_closed_without_a_database():
    original = getattr(app.state, "engine", None)
    app.state.engine = None
    try:
        with TestClient(app) as unready:
            response = unready.get(projection_url(), headers=AUTH)
        assert response.status_code == 503
    finally:
        app.state.engine = original


def test_streams_endpoint_reconstructs_presentations(client):
    StubEventRepository.events = [
        event("s0", 0, "run.started"),
        event("s1", 1, "model.requested", invocation="inv-1", payload={"model": "gpt"}),
        event("s2", 2, "model.chunk", invocation="inv-1", parents=("s1",),
              payload={"chunk": "Hel"}),
        event("s3", 3, "model.chunk", invocation="inv-1", parents=("s2",),
              payload={"chunk": "lo"}),
        event("s4", 4, "model.completed", invocation="inv-1", parents=("s3",),
              payload={"text": "Hello"}),
    ]
    body = client.get(streams_url(), headers=AUTH).json()
    assert body["mode"] == "recorded_playback"
    assert len(body["streams"]) == 1
    frames = body["streams"][0]["frames"]
    assert "".join(frame["delta"] for frame in frames) == "Hello"


def test_streams_response_is_json_serialisable_and_stable(client):
    first = client.get(streams_url(), headers=AUTH)
    second = client.get(streams_url(), headers=AUTH)
    assert first.status_code == 200
    assert first.content == second.content


def test_event_repository_exposes_no_write_methods():
    forbidden = ("create", "insert", "update", "delete", "write", "append", "save")
    for name in dir(EventRepository):
        if name.startswith("_"):
            continue
        assert not any(word in name.lower() for word in forbidden), name


def test_json_safe_normalises_driver_types():
    from datetime import datetime, timezone

    assert json_safe(uuid.UUID(int=1)) == str(uuid.UUID(int=1))
    moment = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
    assert json_safe(moment) == "2026-01-02T03:04:05.678Z"
    assert json_safe({"a": [uuid.UUID(int=2)]}) == {"a": [str(uuid.UUID(int=2))]}


def test_json_safe_renders_naive_timestamps_as_utc():
    from datetime import datetime

    assert json_safe(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05.000Z"
