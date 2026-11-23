"""Bounded-cardinality metrics for Anvil operational phases."""

_PHASES = frozenset({"ingest", "queue", "seal", "replay", "divergence", "model"})
_OUTCOMES = frozenset({"success", "failure", "cancelled", "timeout"})


def metric_counter(*, phase: str, outcome: str, count: int) -> str:
    if phase not in _PHASES or outcome not in _OUTCOMES or type(count) is not int or count < 0:
        raise ValueError("Invalid bounded metrics labels or count")
    return f'anvil_operations_total{{phase="{phase}",outcome="{outcome}"}} {count}\n'


def resource_gauge(*, resource: str, count: int) -> str:
    if resource not in {"runs", "recordings", "events", "jobs"} or type(count) is not int or count < 0:
        raise ValueError("Invalid bounded resource metric")
    return f'anvil_resources{{resource="{resource}"}} {count}\n'
