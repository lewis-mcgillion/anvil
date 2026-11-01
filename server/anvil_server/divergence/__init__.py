"""Run comparison: alignment and divergence detection."""

from __future__ import annotations

from .gating import (
    AGGREGATION_VERSION,
    DETERMINISTIC_STATUSES,
    EXIT_CODES,
    INFRASTRUCTURE_STATUSES,
    JUDGE_STATUSES,
    OUTCOMES,
    Gate,
    InvalidStatusError,
    Outcome,
    aggregate,
    aggregate_comparison,
)
from .alignment import (
    ALIGNMENT_VERSION,
    AlignedPair,
    Alignment,
    AlignmentError,
    Invocation,
    InvocationGraph,
    align,
    build_graph,
)
from .detect import (
    CATEGORIES,
    DETECTION_VERSION,
    Comparison,
    Finding,
    compare,
    compare_graphs,
)

__all__ = [
    "AGGREGATION_VERSION",
    "ALIGNMENT_VERSION",
    "AlignedPair",
    "Alignment",
    "AlignmentError",
    "CATEGORIES",
    "Comparison",
    "DETECTION_VERSION",
    "DETERMINISTIC_STATUSES",
    "EXIT_CODES",
    "Finding",
    "Gate",
    "INFRASTRUCTURE_STATUSES",
    "InvalidStatusError",
    "Invocation",
    "InvocationGraph",
    "JUDGE_STATUSES",
    "OUTCOMES",
    "Outcome",
    "aggregate",
    "aggregate_comparison",
    "align",
    "build_graph",
    "compare",
    "compare_graphs",
]
