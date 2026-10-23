"""Dependency-free verification for the versioned conformance corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
ALLOWED_TYPES = {
    "run.started", "run.completed", "run.failed", "message.created",
    "model.chunk", "model.completed", "tool.requested", "tool.completed",
    "tool.failed", "capture.gap",
}


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def verify() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["canonicalization"] == "RFC8785"
    assert len(manifest["fixtures"]) == 6
    for entry in manifest["fixtures"]:
        recording = json.loads((ROOT / entry["file"]).read_text())
        assert recording["fixture_id"] == entry["id"]
        digest = hashlib.sha256(canonical(recording)).hexdigest()
        assert digest == entry["sha256"], entry["id"]
        run = recording["run"]
        events = recording["events"]
        assert events and len({event["event_id"] for event in events}) == len(events)
        assert all(event["type"] in ALLOWED_TYPES for event in events)
        assert all(event["run_id"] == run["run_id"] for event in events)
        sequences = [event["sequence"] for event in events]
        gaps = [event for event in events if event["type"] == "capture.gap"]
        if gaps:
            assert len(gaps) == 1
            gap = gaps[0]["payload"]
            assert gap["from_sequence"] <= gap["to_sequence"]
            assert gap["events_lost"] == gap["to_sequence"] - gap["from_sequence"] + 1
            assert sequences == [1, 2, gap["to_sequence"] + 1]
        else:
            assert sequences == list(range(1, len(events) + 1))
        assert recording["expected"]["outcome"] == run["outcome"]
        assert recording["expected"]["capture_completeness"] == run["capture_completeness"]
        if any(event["type"] == "capture.gap" for event in events):
            assert run["capture_completeness"] == "sealed_incomplete"
            assert recording["expected"]["replay_eligible"] is False
    print(f"verified {len(manifest['fixtures'])} fixtures")


if __name__ == "__main__":
    verify()
