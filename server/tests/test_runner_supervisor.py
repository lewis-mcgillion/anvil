import asyncio

import pytest

from anvil_server.runner_pb2 import RunnerMessage, SupervisorMessage
from anvil_server.runner_policy import IsolationUnavailable
from anvil_server.runner_supervisor import handshake, read_runner_message, receive_terminal_outcome, supervise_recorded_messages, run_recorded_protocol
from anvil_server.runner_fixtures import RecordedRunnerFixtures
from anvil_server.replay.fixtures import FixtureSession


def framed(message):
    payload = message.SerializeToString()
    return len(payload).to_bytes(4, "big") + payload


@pytest.mark.asyncio
async def test_runner_handshake_accepts_versioned_hello():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(hello={"protocol_version": "1", "runner_version": "1.0",
                                                  "adapter": "google-adk", "adapter_version": "2.0"})))
    reader.feed_eof()
    outgoing = []

    class Writer:
        def write(self, data):
            outgoing.append(data)

        async def drain(self):
            pass

    config = SupervisorMessage(config={"run_id": "run", "attempt_id": "attempt"})
    await handshake(reader, Writer(), config, adapter="google-adk")
    assert outgoing == [framed(config)]


@pytest.mark.asyncio
async def test_runner_handshake_rejects_wrong_version():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(hello={"protocol_version": "2", "runner_version": "1.0",
                                                  "adapter": "google-adk", "adapter_version": "2.0"})))
    reader.feed_eof()
    with pytest.raises(IsolationUnavailable, match="mismatch"):
        await handshake(reader, None, SupervisorMessage(config={"run_id": "run"}), adapter="google-adk")


@pytest.mark.asyncio
async def test_runner_message_rejects_large_or_truncated_frames():
    reader = asyncio.StreamReader()
    reader.feed_data((65_537).to_bytes(4, "big"))
    with pytest.raises(IsolationUnavailable, match="size"):
        await read_runner_message(reader)
    truncated = asyncio.StreamReader()
    truncated.feed_data((10).to_bytes(4, "big") + b"x")
    truncated.feed_eof()
    with pytest.raises(IsolationUnavailable, match="invalid"):
        await read_runner_message(truncated)


@pytest.mark.asyncio
async def test_runner_terminal_session_acknowledges_completion():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(finished={"outcome": 1})))
    reader.feed_eof()
    outgoing = []

    class Writer:
        def write(self, data):
            outgoing.append(data)

        async def drain(self):
            pass

    assert await receive_terminal_outcome(reader, Writer()) == 1
    assert outgoing == [framed(SupervisorMessage(finished={}))]


@pytest.mark.asyncio
async def test_runner_terminal_session_rejects_unsolicited_tool_request():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(tool_request={"request_id": "unauthorized"})))
    reader.feed_eof()
    with pytest.raises(IsolationUnavailable, match="unsupported"):
        await receive_terminal_outcome(reader, None)


@pytest.mark.asyncio
async def test_supervisor_acks_recorded_events_and_terminal_outcome():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(emit_events={
        "run_id": "run", "stream_id": "stream", "events": [
            {"run_id": "run", "stream_id": "stream", "sequence": 0},
        ],
    })) + framed(RunnerMessage(finished={"outcome": 1})))
    reader.feed_eof()
    outgoing = []

    class Writer:
        def write(self, data):
            outgoing.append(data)

        async def drain(self):
            pass

    persisted = []

    async def persist(batch):
        persisted.append(batch.stream_id)

    async def finalize():
        persisted.append("finalized")

    assert await supervise_recorded_messages(reader, Writer(), run_id="run", persist_events=persist,
                                              finalize_fixtures=finalize) == 1
    assert persisted == ["stream", "finalized"]
    assert outgoing[0] == framed(SupervisorMessage(event_ack={
        "run_id": "run", "stream_id": "stream", "acked_through_sequence": 0,
    }))
    assert outgoing[1] == framed(SupervisorMessage(finished={}))


@pytest.mark.asyncio
async def test_supervisor_rejects_event_sequence_gap():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(emit_events={
        "run_id": "run", "stream_id": "stream", "events": [
            {"run_id": "run", "stream_id": "stream", "sequence": 2},
        ],
    })))
    reader.feed_eof()
    async def persist(batch):
        raise AssertionError("Invalid batches must never persist")

    with pytest.raises(IsolationUnavailable, match="sequence"):
        await supervise_recorded_messages(reader, None, run_id="run", persist_events=persist)


@pytest.mark.asyncio
async def test_supervisor_never_acks_unpersisted_events():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(emit_events={
        "run_id": "run", "stream_id": "stream", "events": [
            {"run_id": "run", "stream_id": "stream", "sequence": 0},
        ],
    })))
    reader.feed_eof()
    outgoing = []

    class Writer:
        def write(self, data):
            outgoing.append(data)

        async def drain(self):
            pass

    async def failed_persistence(batch):
        raise RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await supervise_recorded_messages(reader, Writer(), run_id="run",
                                          persist_events=failed_persistence)
    assert outgoing == []


@pytest.mark.asyncio
async def test_supervisor_only_forwards_correlated_fixture_model_response():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(model_request={"request_id": "model-1"}))
                     + framed(RunnerMessage(finished={"outcome": 1})))
    reader.feed_eof()
    outgoing = []

    class Writer:
        def write(self, data):
            outgoing.append(data)

        async def drain(self):
            pass

    async def persist(batch):
        pass

    async def resolve(request):
        return SupervisorMessage(model_response={"request_id": request.request_id,
                                                  "error": {"code": "MODEL_UNMATCHED"}})

    async def persist_empty():
        pass

    assert await supervise_recorded_messages(reader, Writer(), run_id="run",
                                              persist_events=persist, resolve_model=resolve,
                                              finalize_fixtures=persist_empty) == 1
    assert outgoing[0] == framed(SupervisorMessage(model_response={
        "request_id": "model-1", "error": {"code": "MODEL_UNMATCHED"},
    }))


@pytest.mark.asyncio
async def test_supervisor_rejects_mismatched_fixture_response_id():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(tool_request={"request_id": "tool-1"})))
    reader.feed_eof()

    async def persist(batch):
        pass

    async def resolve(request):
        return SupervisorMessage(tool_response={"request_id": "other", "result_json": b"{}"})

    with pytest.raises(IsolationUnavailable, match="correlation"):
        await supervise_recorded_messages(reader, None, run_id="run",
                                          persist_events=persist, resolve_tool=resolve)


@pytest.mark.asyncio
async def test_successful_runner_cannot_skip_fixture_finalization():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(finished={"outcome": 1})))
    reader.feed_eof()

    async def persist(batch):
        pass

    with pytest.raises(IsolationUnavailable, match="fixture verification"):
        await supervise_recorded_messages(reader, None, run_id="run", persist_events=persist)


@pytest.mark.asyncio
async def test_recorded_protocol_handshake_through_terminal_completion():
    reader = asyncio.StreamReader()
    reader.feed_data(framed(RunnerMessage(hello={
        "protocol_version": "1", "runner_version": "1.0", "adapter": "google-adk",
        "adapter_version": "2.0",
    })) + framed(RunnerMessage(finished={"outcome": 1})))
    reader.feed_eof()
    outgoing = []

    class Writer:
        def write(self, data):
            outgoing.append(data)

        async def drain(self):
            pass

    async def persist(batch):
        pass

    config = SupervisorMessage(config={"run_id": "run", "attempt_id": "attempt"})
    assert await run_recorded_protocol(reader, Writer(), adapter="google-adk", config=config,
                                       fixtures=RecordedRunnerFixtures(FixtureSession([])),
                                       persist_events=persist) == 1
    assert outgoing == [framed(config), framed(SupervisorMessage(finished={}))]
