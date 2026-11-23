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
