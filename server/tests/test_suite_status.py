import pytest

from anvil_server.suites.status import aggregate_cases, execute_suite_cases


@pytest.mark.parametrize("statuses,result", [
    ([], "inconclusive"), (["pass", "pass"], "pass"),
    (["pass", "inconclusive"], "inconclusive"),
    (["pass", "fail"], "fail"), (["fail", "cancelled"], "cancelled"),
    (["pass", "error"], "error"),
])
def test_suite_status_fail_closed(statuses, result):
    assert aggregate_cases(statuses) == result
    assert aggregate_cases(list(reversed(statuses))) == result


def test_unknown_case_status_never_passes():
    with pytest.raises(ValueError):
        aggregate_cases(["pass", "unknown"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cases,options", [
    (["case"], {"parallelism": True}), (["case"], {"max_attempts": True}),
    (["case"], {"timeout_seconds": float("inf")}),
    (["case"], {"timeout_seconds": float("nan")}),
    ([""], {}), ([None], {}),
])
async def test_suite_rejects_invalid_execution_configuration(cases, options):
    async def execute(case_id):
        raise AssertionError("Invalid cases must never execute")

    with pytest.raises(ValueError, match="Invalid suite"):
        await execute_suite_cases(cases, execute, **options)


@pytest.mark.asyncio
async def test_suite_cases_respect_parallelism_and_independent_results():
    import asyncio

    active = 0
    maximum = 0

    async def execute(case_id):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return "fail" if case_id == "second" else "pass"

    results = await execute_suite_cases(["first", "second", "third"], execute, parallelism=2)
    assert results == {"first": "pass", "second": "fail", "third": "pass"}
    assert maximum == 2
    assert aggregate_cases(list(results.values())) == "fail"


@pytest.mark.asyncio
async def test_suite_case_exception_is_not_a_pass():
    async def execute(case_id):
        raise RuntimeError("runner unavailable")

    assert await execute_suite_cases(["case"], execute) == {"case": "error"}


@pytest.mark.asyncio
async def test_suite_retries_errors_but_not_explicit_failures():
    attempts = {"transient": 0, "failed": 0}

    async def execute(case_id):
        attempts[case_id] += 1
        if case_id == "transient" and attempts[case_id] == 1:
            raise RuntimeError("temporary")
        return "fail" if case_id == "failed" else "pass"

    result = await execute_suite_cases(["transient", "failed"], execute, max_attempts=2)
    assert result == {"transient": "pass", "failed": "fail"}
    assert attempts == {"transient": 2, "failed": 1}


@pytest.mark.asyncio
async def test_suite_timeout_records_error_and_cleans_up():
    import asyncio

    cleaned = asyncio.Event()

    async def execute(case_id):
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    result = await execute_suite_cases(["stalled"], execute, timeout_seconds=0.001)
    assert result == {"stalled": "error"}
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_suite_cancellation_reclaims_all_active_cases():
    import asyncio

    started = set()
    stopped = set()
    both_running = asyncio.Event()

    async def execute(case_id):
        started.add(case_id)
        if len(started) == 2:
            both_running.set()
        try:
            await asyncio.Future()
        finally:
            stopped.add(case_id)

    task = asyncio.create_task(execute_suite_cases(
        ["first", "second", "third"], execute, parallelism=2,
    ))
    await both_running.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert started == stopped == {"first", "second"}
