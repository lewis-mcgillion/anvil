import asyncio

import pytest

from anvil_server.recorded_execution import execute_recorded_session
from anvil_server.replay.fixtures import Fixture, FixtureSession, ReplayError


@pytest.mark.asyncio
async def test_recorded_agent_without_fixtures_cannot_use_provider():
    session = FixtureSession([])

    async def agent(fixtures):
        return None

    result = await execute_recorded_session(session, agent)
    assert result["provider_calls"] == 0
    with pytest.raises(ReplayError):
        session.finish()


@pytest.mark.asyncio
async def test_recorded_agent_cancellation_terminates_session():
    session = FixtureSession([])
    started = asyncio.Event()

    async def agent(fixtures):
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(execute_recorded_session(session, agent))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ReplayError):
        session.finish()


@pytest.mark.asyncio
async def test_changed_model_prompt_aborts_without_provider_fallback():
    request = {"model": "recorded", "messages": [{"role": "user", "content": "original"}],
               "tools": [], "system_instruction": "", "generation": {}}
    fixture = Fixture.create(kind="model", path=("root",), ordinal=0,
                             request=request, response={"status": "completed", "response": "answer",
                                                        "input_tokens": 2, "output_tokens": 1})
    session = FixtureSession([fixture])

    async def agent(fixtures):
        changed = {**request, "messages": [{"role": "user", "content": "changed"}]}
        fixtures.resolve(request_id="first", kind="model", path=("root",), ordinal=0,
                         request=changed)

    with pytest.raises(ReplayError) as error:
        await execute_recorded_session(session, agent)
    assert error.value.code == "FIXTURE_MISMATCH"
    with pytest.raises(ReplayError):
        session.finish()


@pytest.mark.asyncio
async def test_unrecorded_tool_request_never_reaches_original_tool():
    session = FixtureSession([])
    executed = []

    async def agent(fixtures):
        fixtures.resolve(request_id="tool", kind="tool", path=("root",), ordinal=0,
                         request={"tool_name": "production.write", "schema_digest": "sha256:" + "a" * 64,
                                  "arguments": {"data": "private"}})
        executed.append("production tool executed")

    with pytest.raises(ReplayError) as error:
        await execute_recorded_session(session, agent)
    assert error.value.code == "FIXTURE_MISSING"
    assert executed == []


@pytest.mark.asyncio
async def test_recorded_agent_crash_is_not_a_successful_replay():
    session = FixtureSession([])

    async def agent(fixtures):
        raise RuntimeError("agent crashed")

    with pytest.raises(RuntimeError, match="agent crashed"):
        await execute_recorded_session(session, agent)
    with pytest.raises(ReplayError):
        session.finish()


@pytest.mark.asyncio
async def test_recorded_agent_consumes_model_fixture_without_provider():
    request = {"model": "recorded", "messages": [{"role": "user", "content": "hello"}],
               "tools": [], "system_instruction": "", "generation": {}}
    fixture = Fixture.create(kind="model", path=("root",), ordinal=0,
                             request=request, response={"status": "completed", "response": "hello",
                                                        "input_tokens": 2, "output_tokens": 1})
    session = FixtureSession([fixture])

    async def agent(fixtures):
        response = fixtures.resolve(request_id="model-call", kind="model", path=("root",),
                                    ordinal=0, request=request)
        assert response["response"] == "hello"

    evidence = await execute_recorded_session(session, agent)
    assert evidence["provider_calls"] == 0
    assert evidence["consumed"] == 1


@pytest.mark.asyncio
async def test_recorded_agent_timeout_closes_session():
    session = FixtureSession([])

    async def agent(fixtures):
        await asyncio.Future()

    with pytest.raises(TimeoutError):
        await execute_recorded_session(session, agent, timeout_seconds=0.001)
    with pytest.raises(ReplayError):
        session.finish()
