import pytest

from anvil_server.suites.status import aggregate_cases


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
