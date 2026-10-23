"""Real PostgreSQL assertions activated by ANVIL_TEST_DATABASE_URL."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID
import secrets
import time

import pytest
import pytest_asyncio
from sqlalchemy import text

from anvil_server.database import engine_for, project_connection
from anvil_server.events import Conflict, Event, accept_batch
from anvil_server.jobs import JobQueue, LeaseLost


PROJECT = UUID("0199b167-0000-7000-8000-000000000011")
RUN = UUID("0199b167-0000-7000-8000-000000000021")
STREAM = UUID("0199b167-0000-7000-8000-000000000031")


def new_uuid7():
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    return UUID(int=(timestamp << 80) | (7 << 76) | (secrets.randbits(12) << 64)
                | (2 << 62) | secrets.randbits(62))


@pytest_asyncio.fixture
async def engine():
    url = os.environ.get("ANVIL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ANVIL_TEST_DATABASE_URL not configured")
    engine = engine_for(url)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_event_retry_and_conflict(engine):
    async with project_connection(engine, PROJECT) as connection:
        next_sequence = (await connection.execute(text("SELECT next_sequence FROM run_streams WHERE id = :stream"), {"stream": STREAM})).scalar_one()
    event = Event(schema_version="1", event_id=new_uuid7(),
                  run_id=RUN, stream_id=STREAM, sequence=next_sequence, type="message.created",
                  occurred_at=datetime.now(timezone.utc), payload={"message": "safe"})
    assert await accept_batch(engine, PROJECT, RUN, STREAM, [event]) == {"accepted": 1, "duplicates": 0}
    assert await accept_batch(engine, PROJECT, RUN, STREAM, [event]) == {"accepted": 0, "duplicates": 1}
    with pytest.raises(Conflict):
        await accept_batch(engine, PROJECT, RUN, STREAM, [event.model_copy(update={"payload": {"message": "changed"}})])
    async with project_connection(engine, PROJECT) as connection:
        result = (await connection.execute(text("SELECT count(*) FROM events WHERE run_id = :run"), {"run": RUN})).scalar_one()
    assert result >= 1


@pytest.mark.asyncio
async def test_durable_lease_is_exclusive(engine):
    queue = JobQueue(engine, timedelta(seconds=30))
    job = new_uuid7()
    first_token = new_uuid7()
    other_token = new_uuid7()
    await queue.create(PROJECT, job, "export")
    assert (await queue.acquire(PROJECT, first_token))["id"] == job
    assert await queue.acquire(PROJECT, other_token) is None
    with pytest.raises(LeaseLost):
        await queue.complete(PROJECT, job, other_token, "succeeded")
    await queue.heartbeat(PROJECT, job, first_token)
    await queue.complete(PROJECT, job, first_token, "succeeded")
