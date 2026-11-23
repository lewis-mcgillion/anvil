import asyncio

import pytest

from anvil_server.recorded_execution import execute_recorded_session
from anvil_server.replay.fixtures import FixtureSession, ReplayError


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
