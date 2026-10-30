"""Conservative causal graph alignment, independent of capture IDs and arrival order."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Mapping

from anvil_schema import validate

from ..canonical import canonical_bytes


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class Recording:
    events: tuple[dict, ...]
    capture_completeness: str
    logical_branches: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Graph:
    events: dict[str, dict]
    keys: dict[str, str]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class Alignment:
    confidence: str
    pairs: tuple[tuple[str, str], ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    ambiguous_baseline: tuple[str, ...]
    ambiguous_candidate: tuple[str, ...]


def build_graph(recording: Recording) -> Graph:
    if recording.capture_completeness not in {
        "open", "sealing", "sealed_complete", "sealed_incomplete",
    }:
        raise ValueError("Unknown capture completeness")
    events = {}
    streams = defaultdict(list)
    invocations = defaultdict(list)
    issues = []
    if recording.capture_completeness != "sealed_complete":
        issues.append("capture is not sealed_complete")
    for source in recording.events:
        encoded = canonical_bytes(source)
        validate("event", encoded)
        event = json.loads(encoded)
        identifier = event["event_id"]
        if identifier in events:
            raise ValueError("Duplicate event identity")
        events[identifier] = event
        streams[event["stream_id"]].append(event["sequence"])
        if event["type"].startswith(("tool.", "model.")):
            invocations[event["invocation_id"]].append(event)
        if event["type"] == "capture.gap" or event["payload"]["redactions"]:
            issues.append(f"incomplete evidence at {identifier}")
    if len({event["run_id"] for event in events.values()}) > 1:
        raise ValueError("A recording must contain only one run")
    for sequences in streams.values():
        counts = Counter(sequences)
        if any(count != 1 for count in counts.values()):
            raise ValueError("Duplicate stream sequence")
        if min(sequences) != 0 or max(sequences) != len(sequences) - 1:
            issues.append("stream sequence gap")
    starts = [event for event in events.values() if event["type"] == "run.started"]
    ends = [event for event in events.values() if event["type"] in {"run.completed", "run.failed"}]
    if len(starts) != 1 or len(ends) != 1:
        issues.append("run requires one start and one terminal event")
    branches = dict(recording.logical_branches)
    if branches.keys() - invocations.keys():
        raise ValueError("Logical branch references an unknown invocation")
    for path in branches.values():
        if not isinstance(path, tuple) or not path or any(not isinstance(p, str) or not p for p in path):
            raise ValueError("Logical branches must be nonempty tuples of nonempty strings")
    requests = {}
    for invocation, group in invocations.items():
        requested = [event for event in group if event["type"].endswith(".requested")]
        terminals = [event for event in group if event["type"].endswith((".completed", ".failed"))]
        if len(requested) != 1 or len(terminals) != 1:
            issues.append(f"unmatched invocation {invocation}")
        if len(requested) == 1:
            requests[invocation] = requested[0]["event_id"]
            family = requested[0]["type"].split(".")[0]
            if any(not event["type"].startswith(family + ".") for event in group):
                issues.append(f"mixed invocation families {invocation}")
        chunks = [event["payload"]["chunk_index"] for event in group if event["type"] == "model.chunk"]
        if chunks and sorted(chunks) != list(range(len(chunks))):
            issues.append(f"missing or duplicate model chunk {invocation}")

    children = defaultdict(list)
    degree = {}
    for identifier, event in events.items():
        parents = event["parent_event_ids"]
        degree[identifier] = len([parent for parent in parents if parent in events])
        for parent in parents:
            if parent not in events:
                issues.append(f"missing parent at {identifier}")
            else:
                children[parent].append(identifier)
        if not parents and event["type"] != "run.started":
            issues.append(f"disconnected root at {identifier}")
        if parents and event["type"] == "run.started":
            issues.append("run start has causal parents")
    queue = deque(identifier for identifier, count in degree.items() if count == 0)
    keys = {}
    # Taint must propagate: descendants of missing evidence cannot align exactly.
    while queue:
        identifier = queue.popleft()
        event = events[identifier]
        kind = event["type"]
        if kind.endswith((".completed", ".failed")):
            kind = kind.split(".")[0] + ".terminal"
        parents = sorted(keys.get(parent, "missing-parent") for parent in event["parent_event_ids"])
        invocation = event["invocation_id"]
        branch = branches.get(invocation, ())
        request = requests.get(invocation) if event["type"].startswith(("tool.", "model.")) else None
        request_key = keys.get(request) if request and request != identifier else None
        if request and request != identifier:
            # A terminal/chunk must be causally downstream of its own request.
            if not _has_ancestor(identifier, request, events):
                issues.append(f"invocation response has no causal request at {identifier}")
        keys[identifier] = digest([kind, parents, list(branch), request_key,
                                   event["payload"].get("chunk_index") if kind == "model.chunk" else None])
        for child in children[identifier]:
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    if len(keys) != len(events):
        raise ValueError("Causal cycle")
    if ends:
        terminal = ends[0]["event_id"]
        ancestors = _ancestors(terminal, events)
        if events.keys() - ancestors - {terminal}:
            issues.append("run terminal does not cover all events")
    return Graph(events, keys, tuple(sorted(set(issues))))


def _ancestors(identifier: str, events: dict[str, dict]) -> set[str]:
    found = set()
    pending = list(events[identifier]["parent_event_ids"])
    while pending:
        parent = pending.pop()
        if parent in events and parent not in found:
            found.add(parent)
            pending.extend(events[parent]["parent_event_ids"])
    return found


def _has_ancestor(identifier: str, parent: str, events: dict[str, dict]) -> bool:
    return parent in _ancestors(identifier, events)


def align(baseline: Graph, candidate: Graph) -> Alignment:
    left = defaultdict(list)
    right = defaultdict(list)
    for identifier, key in baseline.keys.items():
        left[key].append(identifier)
    for identifier, key in candidate.keys.items():
        right[key].append(identifier)
    pairs, missing, extra, ambiguous_left, ambiguous_right = [], [], [], [], []
    for key in sorted(left.keys() | right.keys()):
        before, after = sorted(left[key]), sorted(right[key])
        if len(before) > 1 or len(after) > 1:
            ambiguous_left.extend(before)
            ambiguous_right.extend(after)
        elif before and after:
            pairs.append((before[0], after[0]))
        elif before:
            missing.extend(before)
        else:
            extra.extend(after)
    confidence = "ambiguous" if ambiguous_left or ambiguous_right else (
        "partial" if missing or extra or baseline.issues or candidate.issues else "exact")
    return Alignment(confidence, tuple(pairs), tuple(sorted(missing)), tuple(sorted(extra)),
                     tuple(sorted(ambiguous_left)), tuple(sorted(ambiguous_right)))
