"""Deterministic aggregation that cannot turn missing case evidence into a pass."""

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
