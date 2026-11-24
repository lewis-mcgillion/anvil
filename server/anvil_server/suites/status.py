"""Deterministic aggregation that cannot turn missing case evidence into a pass."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from collections.abc import Sequence
from typing import Literal

CaseStatus = Literal["pass", "fail", "inconclusive", "cancelled", "error"]


def aggregate_cases(statuses: Sequence[CaseStatus]) -> CaseStatus:
    if not statuses:
        return "inconclusive"
    if any(status not in {"pass", "fail", "inconclusive", "cancelled", "error"}
           for status in statuses):
        raise ValueError("Unknown suite case status")
    for outcome in ("error", "cancelled", "fail", "inconclusive"):
        if outcome in statuses:
            return outcome
    return "pass"


async def execute_suite_cases(
    cases: Sequence[str], execute: Callable[[str], Awaitable[CaseStatus]],
    *, parallelism: int = 1, timeout_seconds: float = 120,
    max_attempts: int = 1,
) -> dict[str, CaseStatus]:
    if (type(parallelism) is not int or not 1 <= parallelism <= 32
            or type(max_attempts) is not int or not 1 <= max_attempts <= 3
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
            or any(type(case_id) is not str or not case_id for case_id in cases)
            or len(cases) != len(set(cases))):
        raise ValueError("Invalid suite parallelism, deadline, retries or case identity")
    semaphore = asyncio.Semaphore(parallelism)
    results: dict[str, CaseStatus] = {}

    async def run(case_id: str) -> None:
        async with semaphore:
            try:
                status = "error"
                for _ in range(max_attempts):
                    try:
                        async with asyncio.timeout(timeout_seconds):
                            status = await execute(case_id)
                        if status not in {"pass", "fail", "inconclusive", "cancelled", "error"}:
                            raise ValueError("Case returned an invalid status")
                    except Exception:
                        status = "error"
                    if status != "error":
                        break
                results[case_id] = status
            except asyncio.CancelledError:
                results[case_id] = "cancelled"
                raise
            except Exception:
                results[case_id] = "error"

    async with asyncio.TaskGroup() as group:
        for case_id in cases:
            group.create_task(run(case_id))
    return results
