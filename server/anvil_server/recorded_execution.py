"""Offline agent execution against finite, strictly matched replay fixtures."""

import asyncio
from collections.abc import Awaitable, Callable

from .replay.fixtures import FixtureSession


async def execute_recorded_session(
    session: FixtureSession, agent: Callable[[FixtureSession], Awaitable[None]],
) -> dict:
    try:
        await agent(session)
        return session.finish()
    except asyncio.CancelledError:
        session.cancel()
        raise
    finally:
        session.close()
