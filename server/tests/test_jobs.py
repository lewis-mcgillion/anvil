from datetime import timedelta

import pytest

from anvil_server.jobs import JobQueue


def test_queue_rejects_nonpositive_lease():
    with pytest.raises(ValueError):
        JobQueue(None, timedelta(seconds=0))
