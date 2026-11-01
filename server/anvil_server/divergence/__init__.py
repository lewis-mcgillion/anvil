"""Run comparison: alignment and divergence detection."""

from __future__ import annotations

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
    "ALIGNMENT_VERSION",
    "CATEGORIES",
    "DETECTION_VERSION",
    "AlignedPair",
    "Alignment",
    "AlignmentError",
    "Comparison",
    "Finding",
    "Invocation",
    "InvocationGraph",
    "align",
    "build_graph",
    "compare",
    "compare_graphs",
]
