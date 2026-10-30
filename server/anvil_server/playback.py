"""Pure, versioned playback projection over immutable captured events.

Playback is read-only by construction. Nothing in this module executes agent
code, invokes tools or contacts a model provider; it folds stored events into a
state document. The projection is a pure function of ``(events, version)``, so
the canonical projection hash is stable across processes, operating systems and
ingestion orders.

Ordering never uses ``occurred_at``. Captured wall-clock timestamps come from
unsynchronised machines and are presentation data only. Order is derived from
declared causal parents plus per-stream sequence, with a total, deterministic
tie-break on ``(stream_id, sequence, event_id)``.

Failures are explicit: missing causal parents, causal cycles, per-stream
sequence gaps or duplicates, and unknown projection versions all raise
:class:`PlaybackError` rather than producing a partial projection. The module
holds no resources, so there is nothing to release; callers own cancellation of
any surrounding operation.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_hash

__all__ = [
    "PROJECTION_VERSION",
    "SUPPORTED_PROJECTION_VERSIONS",
    "PlaybackError",
    "CausalCycleError",
    "MissingParentError",
    "SequenceError",
    "order_events",
    "project",
    "projection_hash",
    "ProjectionIndex",
]

PROJECTION_VERSION = "1"
SUPPORTED_PROJECTION_VERSIONS = frozenset({"1"})

_TERMINAL_STATUS = {
    "run.completed": "succeeded",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}

_REQUEST_TYPES = {"tool.requested": "tool", "model.requested": "model"}
_RESOLUTION_TYPES = {
    "tool.completed": ("tool", "completed"),
    "tool.failed": ("tool", "failed"),
    "model.completed": ("model", "completed"),
    "model.failed": ("model", "failed"),
}


class PlaybackError(ValueError):
    """Base class for projection failures. Playback fails closed."""


class MissingParentError(PlaybackError):
    """A declared causal parent is not present in the supplied events."""


class CausalCycleError(PlaybackError):
    """Causal parent references form a cycle, so no causal order exists."""


class SequenceError(PlaybackError):
    """A stream has duplicate or non-contiguous sequence numbers."""


def _event_key(event: Mapping[str, Any]) -> tuple[str, int, str]:
    return (str(event["stream_id"]), int(event["sequence"]), str(event["event_id"]))


def _parents(event: Mapping[str, Any]) -> list[str]:
    return [str(parent) for parent in event.get("parent_event_ids") or []]


def order_events(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return events in deterministic causal order.

    The result depends only on event content, never on the iteration order of
    the input or on captured timestamps. Within the constraints imposed by
    causal parents and per-stream sequence, events are emitted in ascending
    ``(stream_id, sequence, event_id)`` order so concurrent branches interleave
    identically on every run.
    """
    materialized = list(events)
    by_id: dict[str, Mapping[str, Any]] = {}
    for event in materialized:
        event_id = str(event["event_id"])
        if event_id in by_id:
            raise PlaybackError(f"Duplicate event identity {event_id}")
        by_id[event_id] = event

    per_stream: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in materialized:
        per_stream[str(event["stream_id"])].append(event)

    dependencies: dict[str, set[str]] = {event_id: set() for event_id in by_id}
    dependents: dict[str, set[str]] = defaultdict(set)

    for stream_id, stream_events in per_stream.items():
        stream_events.sort(key=lambda item: int(item["sequence"]))
        sequences = [int(item["sequence"]) for item in stream_events]
        if len(set(sequences)) != len(sequences):
            raise SequenceError(f"Stream {stream_id} has duplicate sequence numbers")
        if any(right != left + 1 for left, right in zip(sequences, sequences[1:])):
            raise SequenceError(f"Stream {stream_id} has a sequence gap")
        for previous, current in zip(stream_events, stream_events[1:]):
            earlier, later = str(previous["event_id"]), str(current["event_id"])
            dependencies[later].add(earlier)
            dependents[earlier].add(later)

    for event in materialized:
        event_id = str(event["event_id"])
        for parent in _parents(event):
            if parent not in by_id:
                raise MissingParentError(
                    f"Event {event_id} references absent causal parent {parent}"
                )
            if parent == event_id:
                raise CausalCycleError(f"Event {event_id} is its own causal parent")
            dependencies[event_id].add(parent)
            dependents[parent].add(event_id)

    ready: list[tuple[tuple[str, int, str], str]] = [
        (_event_key(by_id[event_id]), event_id)
        for event_id, parents in dependencies.items()
        if not parents
    ]
    heapq.heapify(ready)

    ordered: list[Mapping[str, Any]] = []
    remaining = dict(dependencies)
    while ready:
        _, event_id = heapq.heappop(ready)
        ordered.append(by_id[event_id])
        for dependent in sorted(dependents[event_id]):
            remaining[dependent].discard(event_id)
            if not remaining[dependent]:
                heapq.heappush(ready, (_event_key(by_id[dependent]), dependent))

    if len(ordered) != len(materialized):
        unresolved = sorted(set(by_id) - {str(item["event_id"]) for item in ordered})
        raise CausalCycleError(
            f"Causal cycle involving {len(unresolved)} events, first {unresolved[0]}"
        )
    return ordered


def _initial_state(run_id: str | None, version: str) -> dict[str, Any]:
    return {
        "projection_version": version,
        "run_id": run_id,
        "status": "running",
        "event_count": 0,
        "streams": {},
        "messages": [],
        "invocations": [],
        "checkpoints": [],
        "capture_gaps": [],
        "faults": [],
        "replay_blockers": [],
    }


def _apply(state: dict[str, Any], event: Mapping[str, Any]) -> None:
    """Fold a single event into ``state`` in place.

    Pure with respect to the outside world: it reads only ``event`` and mutates
    only ``state``.
    """
    event_type = str(event["type"])
    event_id = str(event["event_id"])
    stream_id = str(event["stream_id"])
    payload = event.get("payload") or {}
    invocation_id = event.get("invocation_id")
    invocation_id = str(invocation_id) if invocation_id is not None else None

    state["event_count"] += 1
    if state["run_id"] is None and event.get("run_id") is not None:
        state["run_id"] = str(event["run_id"])

    stream = state["streams"].setdefault(
        stream_id, {"event_count": 0, "first_sequence": int(event["sequence"]), "last_sequence": None}
    )
    stream["event_count"] += 1
    stream["last_sequence"] = int(event["sequence"])

    if payload.get("replay_safe") is False:
        state["replay_blockers"].append({"event_id": event_id, "reason": "event marked replay unsafe"})

    if event_type in _TERMINAL_STATUS:
        state["status"] = _TERMINAL_STATUS[event_type]
        return
    if event_type == "run.started":
        state["status"] = "running"
        return
    if event_type == "message.created":
        state["messages"].append(
            {
                "event_id": event_id,
                "role": payload.get("role"),
                "text": payload.get("text"),
                "invocation_id": invocation_id,
            }
        )
        return
    if event_type == "state.checkpoint":
        state["checkpoints"].append({"event_id": event_id, "state": payload.get("state")})
        return
    if event_type == "capture.gap":
        state["capture_gaps"].append(
            {
                "event_id": event_id,
                "stream_id": stream_id,
                "reason": payload.get("reason"),
                "dropped_events": payload.get("dropped_events"),
            }
        )
        state["replay_blockers"].append({"event_id": event_id, "reason": "capture gap"})
        return
    if event_type == "fault.applied":
        state["faults"].append(
            {
                "event_id": event_id,
                "fault_id": payload.get("fault_id"),
                "kind": payload.get("kind"),
                "target": payload.get("target"),
            }
        )
        return
    if event_type in _REQUEST_TYPES:
        state["invocations"].append(
            {
                "invocation_id": invocation_id,
                "kind": _REQUEST_TYPES[event_type],
                "name": payload.get("tool_name") or payload.get("model"),
                "status": "pending",
                "request_event_id": event_id,
                "arguments": payload.get("arguments") or payload.get("request"),
                "result": None,
                "error": None,
                "chunks": [],
            }
        )
        return
    if event_type in _RESOLUTION_TYPES:
        kind, status = _RESOLUTION_TYPES[event_type]
        record = _find_pending(state, invocation_id, kind)
        if record is None:
            state["replay_blockers"].append(
                {"event_id": event_id, "reason": f"{event_type} without a matching request"}
            )
            return
        record["status"] = status
        record["result"] = payload.get("result") or payload.get("response")
        record["error"] = payload.get("error")
        return
    if event_type == "model.chunk":
        record = _find_pending(state, invocation_id, "model")
        if record is None:
            state["replay_blockers"].append(
                {"event_id": event_id, "reason": "model.chunk without a matching request"}
            )
            return
        record["chunks"].append({"event_id": event_id, "delta": payload.get("delta")})
        return
    state["replay_blockers"].append({"event_id": event_id, "reason": f"unknown event type {event_type}"})


def _find_pending(state: dict[str, Any], invocation_id: str | None, kind: str) -> dict[str, Any] | None:
    if invocation_id is None:
        return None
    for record in reversed(state["invocations"]):
        if record["invocation_id"] == invocation_id and record["kind"] == kind:
            return record
    return None


def _finalize(state: dict[str, Any]) -> dict[str, Any]:
    pending = [record for record in state["invocations"] if record["status"] == "pending"]
    for record in pending:
        state["replay_blockers"].append(
            {
                "event_id": record["request_event_id"],
                "reason": f"{record['kind']} invocation never resolved",
            }
        )
    state["replay_eligible"] = not state["replay_blockers"]
    return state


def project(
    events: Iterable[Mapping[str, Any]],
    *,
    version: str = PROJECTION_VERSION,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Fold ``events`` into a read-only projection under a pinned ``version``.

    Raises :class:`PlaybackError` for unknown versions or any ordering problem.
    No job is scheduled and no external system is contacted.
    """
    if version not in SUPPORTED_PROJECTION_VERSIONS:
        raise PlaybackError(f"Unsupported projection version {version!r}")
    state = _initial_state(run_id, version)
    for event in order_events(events):
        _apply(state, event)
    return _finalize(state)


def projection_hash(projection: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 of a projection document."""
    return canonical_hash(projection)


class ProjectionIndex:
    """Seekable projection with cached checkpoints.

    The index orders events once and stores a projection snapshot every
    ``interval`` events. :meth:`at` replays only the events after the nearest
    snapshot, so a random seek returns exactly the projection that folding the
    same prefix from scratch would produce.
    """

    def __init__(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        interval: int = 100,
        version: str = PROJECTION_VERSION,
        run_id: str | None = None,
    ) -> None:
        if interval < 1:
            raise ValueError("Checkpoint interval must be at least 1")
        if version not in SUPPORTED_PROJECTION_VERSIONS:
            raise PlaybackError(f"Unsupported projection version {version!r}")
        self.version = version
        self.interval = interval
        self._run_id = run_id
        self.events: list[Mapping[str, Any]] = order_events(events)
        self._checkpoints: list[tuple[int, dict[str, Any]]] = []

        state = _initial_state(run_id, version)
        self._checkpoints.append((0, _copy(state)))
        for position, event in enumerate(self.events, start=1):
            _apply(state, event)
            if position % interval == 0:
                self._checkpoints.append((position, _copy(state)))

    def __len__(self) -> int:
        return len(self.events)

    @property
    def checkpoint_positions(self) -> list[int]:
        return [position for position, _ in self._checkpoints]

    def cursor(self, position: int) -> str | None:
        """Return the opaque event cursor at ``position`` (1-based count)."""
        if position < 0 or position > len(self.events):
            raise IndexError(f"Position {position} is outside the recording")
        if position == 0:
            return None
        return str(self.events[position - 1]["event_id"])

    def position_of(self, cursor: str) -> int:
        """Return the prefix length that ends at ``cursor``."""
        for position, event in enumerate(self.events, start=1):
            if str(event["event_id"]) == cursor:
                return position
        raise PlaybackError(f"Cursor {cursor!r} is not part of this recording")

    def at(self, position: int) -> dict[str, Any]:
        """Return the projection of the first ``position`` ordered events."""
        if position < 0 or position > len(self.events):
            raise IndexError(f"Position {position} is outside the recording")
        base_position, base_state = 0, None
        for checkpoint_position, snapshot in self._checkpoints:
            if checkpoint_position <= position:
                base_position, base_state = checkpoint_position, snapshot
            else:
                break
        state = _copy(base_state) if base_state is not None else _initial_state(self._run_id, self.version)
        for event in self.events[base_position:position]:
            _apply(state, event)
        return _finalize(state)

    def seek(self, cursor: str | None) -> dict[str, Any]:
        """Return the projection up to and including ``cursor``."""
        return self.at(0 if cursor is None else self.position_of(cursor))

    def full(self) -> dict[str, Any]:
        return self.at(len(self.events))


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value
