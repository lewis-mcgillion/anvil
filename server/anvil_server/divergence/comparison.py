"""Offline deterministic comparison and independent fail-closed gate statuses."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .alignment import Recording, align, build_graph, digest
from .normalization import NormalizationPolicy, escape


@dataclass(frozen=True)
class Gate:
    status: str
    exit_code: int


def aggregate(deterministic: str, judge: str, infrastructure: str,
              alignment_confidence: str, *, judge_required: bool = False) -> Gate:
    if deterministic not in {"pass", "fail", "inconclusive"}:
        raise ValueError("Unknown deterministic status")
    if judge not in {"pass", "fail", "inconclusive", "not_run"}:
        raise ValueError("Unknown judge status")
    if infrastructure not in {"ok", "error", "cancelled", "timed_out"}:
        raise ValueError("Unknown infrastructure status")
    if alignment_confidence not in {"exact", "partial", "ambiguous"}:
        raise ValueError("Unknown alignment confidence")
    if type(judge_required) is not bool:
        raise ValueError("Judge gate must be explicitly boolean")
    if infrastructure == "cancelled":
        return Gate("cancelled", 4)
    if infrastructure in {"error", "timed_out"}:
        return Gate("infrastructure_error", 3)
    if deterministic == "fail" or (judge_required and judge == "fail"):
        return Gate("fail", 1)
    if deterministic != "pass" or alignment_confidence != "exact":
        return Gate("inconclusive", 5)
    if judge_required and judge != "pass":
        return Gate("inconclusive", 5)
    return Gate("pass", 0)


def _category(kind: str, path: str) -> str:
    field = path.split("/")[1] if path.startswith("/") else ""
    if field == "error" or kind.endswith(".failed"):
        return "error"
    if field in {"usage", "budget", "budgets", "duration_ms"}:
        return "budget"
    if field == "input":
        return "input"
    if kind == "model.requested":
        return "model_request"
    if field in {"tool_name", "arguments_schema_digest"}:
        return "tool_identity"
    if field == "arguments":
        return "tool_arguments"
    if field == "result":
        return "tool_result"
    if field in {"output", "content", "response", "chunk"}:
        return "output"
    return "control_flow" if field == "outcome" else "payload"


def _differences(before: object, after: object, path: str = ""):
    if digest(before) == digest(after):
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys(), key=lambda key: key.encode("utf-16be")):
            child_path = path + "/" + escape(key)
            if key not in before or key not in after:
                yield child_path, key in before, before.get(key), key in after, after.get(key)
            else:
                yield from _differences(before[key], after[key], child_path)
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, (left, right) in enumerate(zip(before, after)):
            yield from _differences(left, right, path + "/" + str(index))
    else:
        yield path, True, before, True, after


def compare(baseline: Recording, candidate: Recording, *,
            policy: NormalizationPolicy | None = None,
            judge: str = "not_run", infrastructure: str = "ok",
            judge_required: bool = False) -> dict:
    policy = policy if policy is not None else NormalizationPolicy()
    left, right = build_graph(baseline), build_graph(candidate)
    alignment = align(left, right)
    findings = []
    normalization_evidence = []
    normalized = []
    for side, graph in (("baseline", left), ("candidate", right)):
        payloads = {}
        for identifier, event in graph.events.items():
            payload, applied = policy.apply(event["payload"])
            payloads[identifier] = payload
            if applied:
                normalization_evidence.append({
                    "side": side, "event_id": identifier, "pointers": list(applied),
                    "original_sha256": digest(event["payload"]),
                    "normalized_sha256": digest(payload),
                })
        normalized.append(payloads)
    configured = set(policy.ignore_pointers + policy.text_pointers)
    matched = {pointer for row in normalization_evidence for pointer in row["pointers"]}
    if configured - matched:
        raise ValueError("Normalization pointers matched no payload: " + ", ".join(sorted(configured - matched)))

    def finding(category: str, path: str, before_id: str | None, after_id: str | None,
                before_present: bool, before: object, after_present: bool, after: object) -> None:
        findings.append({
            "category": category, "path": path,
            "baseline_event_id": before_id, "candidate_event_id": after_id,
            "baseline_present": before_present, "candidate_present": after_present,
            "baseline_sha256": digest(before) if before_present else None,
            "candidate_sha256": digest(after) if after_present else None,
        })

    for before_id, after_id in alignment.pairs:
        before_event, after_event = left.events[before_id], right.events[after_id]
        if before_event["type"] != after_event["type"]:
            finding("error", "/type", before_id, after_id,
                    True, before_event["type"], True, after_event["type"])
        for path, bp, before, ap, after in _differences(normalized[0][before_id], normalized[1][after_id]):
            finding(_category(after_event["type"], path), "/payload" + path,
                    before_id, after_id, bp, before, ap, after)
    for identifier in alignment.missing:
        finding("missing_event", "", identifier, None, True, left.events[identifier], False, None)
    for identifier in alignment.extra:
        finding("extra_event", "", None, identifier, False, None, True, right.events[identifier])
    issues = {"baseline": list(left.issues), "candidate": list(right.issues)}
    if alignment.confidence == "ambiguous" or left.issues or right.issues:
        deterministic = "inconclusive"
    else:
        deterministic = "fail" if findings else "pass"
    gate = aggregate(deterministic, judge, infrastructure, alignment.confidence,
                     judge_required=judge_required)
    return {
        "report_version": "1", "normalization": policy.descriptor(),
        "projection_version": "causal-payload-v1",
        "recording_sha256": {
            "baseline": digest(sorted(left.events.values(), key=lambda event: event["event_id"])),
            "candidate": digest(sorted(right.events.values(), key=lambda event: event["event_id"])),
        },
        "deterministic": deterministic, "judge": judge, "infrastructure": infrastructure,
        "alignment_confidence": alignment.confidence, "judge_required": judge_required,
        "gate": asdict(gate), "alignment": asdict(alignment),
        "evidence_issues": issues,
        "normalization_evidence": sorted(normalization_evidence, key=lambda row: (row["side"], row["event_id"])),
        "findings": sorted(findings, key=lambda row: (
            row["category"], row["baseline_event_id"] or "", row["candidate_event_id"] or "", row["path"])),
    }
