from dataclasses import replace

import pytest

from anvil_server.runner_limits import RunnerLimitDenied, RunnerLimits


def test_valid_isolated_runner_limits():
    RunnerLimits(500, 128 * 1024 * 1024, 16, 60).validate()


@pytest.mark.parametrize("field,value", [
    ("cpu_millis", 0), ("cpu_millis", 5000), ("memory_bytes", 1),
    ("process_count", 0), ("process_count", 10000), ("wall_seconds", 121),
    ("wall_seconds", True),
])
def test_resource_limit_bypass_fails_closed(field, value):
    with pytest.raises(RunnerLimitDenied):
        replace(RunnerLimits(500, 128 * 1024 * 1024, 16, 60), **{field: value}).validate()
