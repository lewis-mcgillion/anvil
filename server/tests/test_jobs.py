from datetime import timedelta

import pytest

from anvil_server.jobs import JobQueue


def test_queue_rejects_nonpositive_lease():
    with pytest.raises(ValueError):
        JobQueue(None, timedelta(seconds=0))


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, -1, True, 1.5, 101])
async def test_queue_rejects_invalid_retry_limits_before_database_access(attempts):
    queue = JobQueue(None, timedelta(seconds=1))
    with pytest.raises(ValueError, match="retry limit"):
        await queue.create(None, None, "seal", max_attempts=attempts)
