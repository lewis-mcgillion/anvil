"""Deterministic aggregation that cannot turn missing case evidence into a pass."""

import asyncio
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
    if (parallelism < 1 or parallelism > 32 or len(cases) != len(set(cases))
            or timeout_seconds <= 0 or max_attempts < 1 or max_attempts > 3):
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
