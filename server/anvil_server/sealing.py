"""Pure completeness and safe replay-eligibility checks."""

import hashlib
import json
from collections import Counter, defaultdict


class IncompleteCapture(ValueError):
    pass


def seal(run_id: str, project_id: str, events: list[dict], streams: list[dict],
         gaps: list[dict], acknowledge_incomplete: bool = False) -> dict:
    issues = []
    terminals = {stream["stream_id"]: stream["terminal"] for stream in streams}
    if not terminals or not all(terminals.values()):
        issues.append("stream missing terminal")
    by_id = {event["event_id"]: event for event in events}
    if len(by_id) != len(events):
        issues.append("duplicate event identity")
    by_stream = defaultdict(list)
    for event in events:
        if not terminals.get(event["stream_id"]):
            issues.append("event belongs to unknown or incomplete stream")
        by_stream[event["stream_id"]].append(event["sequence"])
        if any(parent not in by_id for parent in event.get("parent_event_ids", [])):
            issues.append("missing causal parent")
    for sequences in by_stream.values():
        ordered = sorted(sequences)
        if len(set(ordered)) != len(ordered) or any(right != left + 1 for left, right in zip(ordered, ordered[1:])):
            issues.append("sequence gap or duplicate")

    visiting, visited = set(), set()

    def visit(event_id):
        if event_id in visiting:
            return True
        if event_id in visited or event_id not in by_id:
            return False
        visiting.add(event_id)
        if any(visit(parent) for parent in by_id[event_id].get("parent_event_ids", [])):
            return True
        visiting.remove(event_id)
        visited.add(event_id)
        return False

    if any(visit(event_id) for event_id in by_id):
        issues.append("causal cycle")
    requested = Counter(event.get("invocation_id") for event in events if event["type"] in {"tool.requested", "model.requested"})
    completed = Counter(event.get("invocation_id") for event in events if event["type"] in {"tool.completed", "tool.failed", "model.completed", "model.failed"})
    if any(invocation is None or requested[invocation] != completed[invocation] for invocation in requested | completed):
        issues.append("unmatched or ambiguous invocation")
    if gaps:
        issues.append("capture gap")
    if issues and not acknowledge_incomplete:
        raise IncompleteCapture(issues[0])
    event_types = {event["type"] for event in events}
    outcome = "failed" if "run.failed" in event_types else "succeeded" if "run.completed" in event_types else "unknown"
    payload = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"schema_version": "1", "run_id": run_id, "project_id": project_id,
            "capture_completeness": "sealed_incomplete" if issues else "sealed_complete",
            "execution_outcome": outcome,
            "replay_eligibility": "eligible" if not issues and all(event.get("replay_safe") is True for event in events) else "ineligible",
            "event_count": len(events), "stream_count": len(streams), "capture_gaps": gaps,
            "payload_sha256": hashlib.sha256(payload).hexdigest()}
