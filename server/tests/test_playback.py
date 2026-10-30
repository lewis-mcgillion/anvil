"""Determinism, causal ordering and seek equivalence for playback projection."""

from __future__ import annotations

import random

import pytest

from anvil_server.canonical import canonical_hash
from anvil_server.playback import (
    PROJECTION_VERSION,
    CausalCycleError,
    MissingParentError,
    PlaybackError,
    ProjectionIndex,
    SequenceError,
    order_events,
    project,
    projection_hash,
)

RUN = "01900000-0000-7000-8000-00000000r001"
STREAM_A = "01900000-0000-7000-8000-0000000000sa"
STREAM_B = "01900000-0000-7000-8000-0000000000sb"


def event(
    event_id: str,
    sequence: int,
    event_type: str,
    *,
    stream: str = STREAM_A,
    parents: tuple[str, ...] = (),
    invocation: str | None = None,
    payload: dict | None = None,
    occurred_at: str = "2026-01-01T00:00:00.000Z",
) -> dict:
    return {
        "event_id": event_id,
        "run_id": RUN,
        "stream_id": stream,
        "sequence": sequence,
        "type": event_type,
        "occurred_at": occurred_at,
        "parent_event_ids": list(parents),
        "invocation_id": invocation,
        "payload": payload or {},
    }


def simple_run() -> list[dict]:
    return [
        event("e0", 0, "run.started"),
        event("e1", 1, "message.created", payload={"role": "user", "text": "hi"}),
        event("e2", 2, "tool.requested", invocation="inv-1",
              payload={"tool_name": "inventory.lookup", "arguments": {"sku": "ABC"}}),
        event("e3", 3, "tool.completed", invocation="inv-1", parents=("e2",),
              payload={"tool_name": "inventory.lookup", "result": {"available": True}}),
        event("e4", 4, "run.completed", parents=("e3",)),
    ]


class TestCausalOrdering:
    def test_order_is_independent_of_ingestion_order(self):
        events = simple_run()
        expected = [item["event_id"] for item in order_events(events)]
        rng = random.Random(1234)
        for _ in range(50):
            shuffled = events[:]
            rng.shuffle(shuffled)
            assert [item["event_id"] for item in order_events(shuffled)] == expected

    def test_wall_clock_order_is_never_used(self):
        """Reversing every timestamp must not change the projection."""
        events = simple_run()
        reversed_clock = []
        for index, item in enumerate(events):
            copy = dict(item)
            copy["occurred_at"] = f"2026-01-01T00:00:{59 - index:02d}.000Z"
            reversed_clock.append(copy)
        assert projection_hash(project(reversed_clock)) == projection_hash(project(events))

    def test_concurrent_branches_interleave_deterministically(self):
        events = [
            event("a0", 0, "run.started"),
            event("a1", 1, "tool.requested", invocation="inv-a",
                  payload={"tool_name": "alpha", "arguments": {}}),
            event("b0", 0, "tool.requested", stream=STREAM_B, invocation="inv-b",
                  payload={"tool_name": "beta", "arguments": {}}),
            event("b1", 1, "tool.completed", stream=STREAM_B, invocation="inv-b",
                  parents=("b0",), payload={"tool_name": "beta", "result": 1}),
            event("a2", 2, "tool.completed", invocation="inv-a", parents=("a1",),
                  payload={"tool_name": "alpha", "result": 2}),
        ]
        rng = random.Random(7)
        expected = [item["event_id"] for item in order_events(events)]
        assert expected == ["a0", "a1", "a2", "b0", "b1"]
        for _ in range(25):
            shuffled = events[:]
            rng.shuffle(shuffled)
            assert [item["event_id"] for item in order_events(shuffled)] == expected

    def test_cross_stream_parent_constrains_order(self):
        events = [
            event("b0", 0, "message.created", stream=STREAM_B, parents=("a0",)),
            event("a0", 0, "run.started"),
        ]
        assert [item["event_id"] for item in order_events(events)] == ["a0", "b0"]

    def test_missing_parent_fails_closed(self):
        events = [event("e0", 0, "run.started", parents=("ghost",))]
        with pytest.raises(MissingParentError, match="ghost"):
            order_events(events)

    def test_self_parent_is_a_cycle(self):
        events = [event("e0", 0, "run.started", parents=("e0",))]
        with pytest.raises(CausalCycleError):
            order_events(events)

    def test_cycle_across_streams_is_detected(self):
        events = [
            event("a0", 0, "run.started", parents=("b0",)),
            event("b0", 0, "message.created", stream=STREAM_B, parents=("a0",)),
        ]
        with pytest.raises(CausalCycleError, match="Causal cycle"):
            order_events(events)

    def test_sequence_gap_is_rejected(self):
        events = [event("e0", 0, "run.started"), event("e2", 2, "run.completed")]
        with pytest.raises(SequenceError, match="sequence gap"):
            order_events(events)

    def test_duplicate_sequence_is_rejected(self):
        events = [event("e0", 0, "run.started"), event("e1", 0, "run.completed")]
        with pytest.raises(SequenceError, match="duplicate sequence"):
            order_events(events)

    def test_duplicate_event_identity_is_rejected(self):
        events = [event("e0", 0, "run.started"), event("e0", 1, "run.completed")]
        with pytest.raises(PlaybackError, match="Duplicate event identity"):
            order_events(events)

    def test_streams_need_not_start_at_zero(self):
        events = [
            event("e5", 5, "message.created"),
            event("e6", 6, "message.created"),
        ]
        assert [item["event_id"] for item in order_events(events)] == ["e5", "e6"]

    def test_empty_recording_orders_to_nothing(self):
        assert order_events([]) == []


class TestProjection:
    def test_projection_is_repeatable_for_a_pinned_fixture(self):
        first, second = project(simple_run()), project(simple_run())
        assert first == second
        assert projection_hash(first) == projection_hash(second)

    def test_projection_hash_is_the_canonical_hash(self):
        projection = project(simple_run())
        assert projection_hash(projection) == canonical_hash(projection)

    def test_projection_records_version_and_outcome(self):
        projection = project(simple_run())
        assert projection["projection_version"] == PROJECTION_VERSION
        assert projection["status"] == "succeeded"
        assert projection["run_id"] == RUN
        assert projection["event_count"] == 5

    def test_tool_invocation_is_resolved(self):
        projection = project(simple_run())
        assert projection["invocations"] == [
            {
                "invocation_id": "inv-1",
                "kind": "tool",
                "name": "inventory.lookup",
                "status": "completed",
                "request_event_id": "e2",
                "arguments": {"sku": "ABC"},
                "result": {"available": True},
                "error": None,
                "chunks": [],
            }
        ]
        assert projection["replay_eligible"] is True

    def test_failed_run_status_and_tool_error(self):
        events = [
            event("e0", 0, "run.started"),
            event("e1", 1, "tool.requested", invocation="inv-1",
                  payload={"tool_name": "t", "arguments": {}}),
            event("e2", 2, "tool.failed", invocation="inv-1", parents=("e1",),
                  payload={"tool_name": "t", "error": {"code": "timeout"}}),
            event("e3", 3, "run.failed", parents=("e2",)),
        ]
        projection = project(events)
        assert projection["status"] == "failed"
        assert projection["invocations"][0]["status"] == "failed"
        assert projection["invocations"][0]["error"] == {"code": "timeout"}

    def test_model_chunks_are_collected_in_causal_order(self):
        events = [
            event("e0", 0, "model.requested", invocation="inv-m",
                  payload={"model": "gpt", "request": {"messages": []}}),
            event("e1", 1, "model.chunk", invocation="inv-m", parents=("e0",),
                  payload={"delta": "Hel"}),
            event("e2", 2, "model.chunk", invocation="inv-m", parents=("e1",),
                  payload={"delta": "lo"}),
            event("e3", 3, "model.completed", invocation="inv-m", parents=("e2",),
                  payload={"response": {"text": "Hello"}}),
        ]
        projection = project(events)
        invocation = projection["invocations"][0]
        assert [chunk["delta"] for chunk in invocation["chunks"]] == ["Hel", "lo"]
        assert invocation["status"] == "completed"

    def test_unresolved_invocation_blocks_replay(self):
        events = [
            event("e0", 0, "run.started"),
            event("e1", 1, "tool.requested", invocation="inv-1",
                  payload={"tool_name": "t", "arguments": {}}),
        ]
        projection = project(events)
        assert projection["replay_eligible"] is False
        assert projection["replay_blockers"] == [
            {"event_id": "e1", "reason": "tool invocation never resolved"}
        ]

    def test_capture_gap_blocks_replay_and_is_reported(self):
        events = [
            event("e0", 0, "run.started"),
            event("e1", 1, "capture.gap", payload={"reason": "buffer overflow", "dropped_events": 12}),
        ]
        projection = project(events)
        assert projection["capture_gaps"] == [
            {"event_id": "e1", "stream_id": STREAM_A, "reason": "buffer overflow", "dropped_events": 12}
        ]
        assert projection["replay_eligible"] is False

    def test_replay_unsafe_payload_blocks_replay(self):
        events = [event("e0", 0, "message.created", payload={"text": "x", "replay_safe": False})]
        projection = project(events)
        assert projection["replay_eligible"] is False
        assert projection["replay_blockers"][0]["reason"] == "event marked replay unsafe"

    def test_applied_faults_are_recorded_as_evidence(self):
        events = [
            event("e0", 0, "fault.applied",
                  payload={"fault_id": "f1", "kind": "tool_error", "target": "inventory.lookup"}),
        ]
        assert project(events)["faults"] == [
            {"event_id": "e0", "fault_id": "f1", "kind": "tool_error", "target": "inventory.lookup"}
        ]

    def test_resolution_without_request_is_a_blocker_not_a_crash(self):
        events = [event("e0", 0, "tool.completed", invocation="inv-x", payload={"result": 1})]
        projection = project(events)
        assert projection["replay_eligible"] is False
        assert "without a matching request" in projection["replay_blockers"][0]["reason"]

    def test_unknown_event_type_is_a_blocker_not_silent_success(self):
        events = [event("e0", 0, "totally.unknown")]
        projection = project(events)
        assert projection["replay_blockers"] == [
            {"event_id": "e0", "reason": "unknown event type totally.unknown"}
        ]

    def test_unsupported_projection_version_is_rejected(self):
        with pytest.raises(PlaybackError, match="Unsupported projection version"):
            project(simple_run(), version="99")

    def test_empty_projection_is_well_formed(self):
        projection = project([], run_id=RUN)
        assert projection["event_count"] == 0
        assert projection["status"] == "running"
        assert projection["replay_eligible"] is True

    def test_projection_does_not_mutate_input_events(self):
        events = simple_run()
        snapshot = canonical_hash(events)
        project(events)
        assert canonical_hash(events) == snapshot


class TestProjectionIndex:
    def build(self, count: int = 37, interval: int = 5) -> tuple[ProjectionIndex, list[dict]]:
        events = [event("e0", 0, "run.started")]
        for index in range(1, count - 1):
            if index % 2:
                events.append(
                    event(f"e{index}", index, "tool.requested", invocation=f"inv-{index}",
                          payload={"tool_name": f"tool-{index}", "arguments": {"i": index}})
                )
            else:
                events.append(
                    event(f"e{index}", index, "tool.completed", invocation=f"inv-{index - 1}",
                          parents=(f"e{index - 1}",), payload={"result": index})
                )
        events.append(event(f"e{count - 1}", count - 1, "run.completed"))
        return ProjectionIndex(events, interval=interval), events

    def test_every_seek_equals_the_full_projection_of_that_prefix(self):
        index, events = self.build()
        ordered = order_events(events)
        for position in range(len(ordered) + 1):
            assert index.at(position) == project(ordered[:position])

    def test_random_seeks_match_sequential_results(self):
        index, events = self.build(count=61, interval=7)
        ordered = order_events(events)
        rng = random.Random(99)
        for _ in range(40):
            position = rng.randrange(len(ordered) + 1)
            assert projection_hash(index.at(position)) == projection_hash(project(ordered[:position]))

    def test_full_matches_project(self):
        index, events = self.build()
        assert index.full() == project(events)

    def test_checkpoints_are_created_at_the_configured_interval(self):
        index, _ = self.build(count=37, interval=5)
        assert index.checkpoint_positions == [0, 5, 10, 15, 20, 25, 30, 35]

    def test_interval_does_not_change_results(self):
        _, events = self.build(count=41, interval=5)
        coarse = ProjectionIndex(events, interval=40)
        fine = ProjectionIndex(events, interval=1)
        assert coarse.full() == fine.full() == project(events)
        for position in range(len(events) + 1):
            assert coarse.at(position) == fine.at(position)

    def test_cursor_round_trips_to_its_position(self):
        index, _ = self.build()
        for position in range(1, len(index) + 1):
            assert index.position_of(index.cursor(position)) == position

    def test_cursor_zero_is_the_start_of_the_recording(self):
        index, _ = self.build()
        assert index.cursor(0) is None
        assert index.seek(None)["event_count"] == 0

    def test_seek_by_cursor_matches_seek_by_position(self):
        index, _ = self.build()
        cursor = index.cursor(12)
        assert index.seek(cursor) == index.at(12)

    def test_unknown_cursor_is_rejected(self):
        index, _ = self.build()
        with pytest.raises(PlaybackError, match="not part of this recording"):
            index.seek("not-a-real-event")

    def test_out_of_range_positions_are_rejected(self):
        index, _ = self.build()
        for position in (-1, len(index) + 1):
            with pytest.raises(IndexError):
                index.at(position)

    def test_checkpoint_snapshots_are_isolated_from_callers(self):
        """Mutating a returned projection must not corrupt cached checkpoints."""
        index, _ = self.build()
        first = index.at(20)
        first["invocations"].append({"injected": True})
        first["streams"].clear()
        assert index.at(20) != first
        assert index.at(20) == index.at(20)

    def test_invalid_interval_is_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            ProjectionIndex(simple_run(), interval=0)

    def test_unsupported_version_is_rejected(self):
        with pytest.raises(PlaybackError, match="Unsupported projection version"):
            ProjectionIndex(simple_run(), version="0")

    def test_index_is_order_insensitive(self):
        _, events = self.build()
        rng = random.Random(5)
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert ProjectionIndex(shuffled).full() == ProjectionIndex(events).full()
