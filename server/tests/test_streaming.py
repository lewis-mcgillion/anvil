"""Streaming reconstruction and presentation-only playback clock behaviour."""

from __future__ import annotations

import random

import pytest

from anvil_server.canonical import canonical_hash
from anvil_server.streaming import (
    Frame,
    ManualClock,
    PlaybackSession,
    StreamingError,
    reconstruct_streams,
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
    occurred_at: str | None = "2026-01-01T00:00:00.000Z",
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


def streamed_run(deltas=("Hel", "lo, ", "world"), *, complete: bool = True) -> list[dict]:
    events = [
        event("m0", 0, "model.requested", invocation="inv-m", payload={"model": "gpt-test"})
    ]
    previous = "m0"
    for index, delta in enumerate(deltas):
        identifier = f"c{index}"
        events.append(
            event(
                identifier,
                index + 1,
                "model.chunk",
                invocation="inv-m",
                parents=(previous,),
                payload={"chunk": delta},
                occurred_at=f"2026-01-01T00:00:{index:02d}.500Z",
            )
        )
        previous = identifier
    if complete:
        events.append(
            event(
                "done",
                len(deltas) + 1,
                "model.completed",
                invocation="inv-m",
                parents=(previous,),
                payload={"response": {"text": "".join(deltas)}},
            )
        )
    return events


class TestReconstruction:
    def test_chunks_reassemble_into_the_original_text(self):
        [record] = reconstruct_streams(streamed_run())
        assert record["text"] == "Hello, world"
        assert record["status"] == "completed"
        assert record["truncated"] is False
        assert record["model"] == "gpt-test"

    def test_frames_carry_cumulative_text(self):
        [record] = reconstruct_streams(streamed_run())
        assert [frame.delta for frame in record["frames"]] == ["Hel", "lo, ", "world"]
        assert [frame.text for frame in record["frames"]] == ["Hel", "Hello, ", "Hello, world"]
        assert [frame.final for frame in record["frames"]] == [False, False, True]

    def test_reconstruction_is_independent_of_ingestion_order(self):
        events = streamed_run()
        expected = reconstruct_streams(events)[0]["text"]
        rng = random.Random(3)
        for _ in range(25):
            shuffled = events[:]
            rng.shuffle(shuffled)
            assert reconstruct_streams(shuffled)[0]["text"] == expected

    def test_offsets_are_relative_to_the_first_chunk(self):
        [record] = reconstruct_streams(streamed_run())
        assert [frame.offset for frame in record["frames"]] == [0.0, 1.0, 2.0]

    def test_offsets_never_go_backwards_when_clocks_disagree(self):
        """Unsynchronised capture clocks must not produce negative delays."""
        events = [
            event("m0", 0, "model.requested", invocation="inv-m", payload={"model": "m"}),
            event("c0", 1, "model.chunk", invocation="inv-m", parents=("m0",),
                  payload={"chunk": "a"}, occurred_at="2026-01-01T00:00:10.000Z"),
            event("c1", 2, "model.chunk", invocation="inv-m", parents=("c0",),
                  payload={"chunk": "b"}, occurred_at="2026-01-01T00:00:05.000Z"),
            event("c2", 3, "model.chunk", invocation="inv-m", parents=("c1",),
                  payload={"chunk": "c"}, occurred_at="2026-01-01T00:00:12.000Z"),
        ]
        [record] = reconstruct_streams(events)
        offsets = [frame.offset for frame in record["frames"]]
        assert offsets == [0.0, 0.0, 2.0]
        assert offsets == sorted(offsets)
        assert record["text"] == "abc"

    def test_missing_timestamps_fall_back_to_the_previous_offset(self):
        events = [
            event("m0", 0, "model.requested", invocation="inv-m", payload={"model": "m"}),
            event("c0", 1, "model.chunk", invocation="inv-m", parents=("m0",),
                  payload={"chunk": "a"}, occurred_at=None),
            event("c1", 2, "model.chunk", invocation="inv-m", parents=("c0",),
                  payload={"chunk": "b"}, occurred_at=None),
        ]
        [record] = reconstruct_streams(events)
        assert [frame.offset for frame in record["frames"]] == [0.0, 0.0]
        assert record["text"] == "ab"

    def test_unparseable_timestamp_is_treated_as_absent(self):
        events = [
            event("m0", 0, "model.requested", invocation="inv-m", payload={"model": "m"}),
            event("c0", 1, "model.chunk", invocation="inv-m", parents=("m0",),
                  payload={"chunk": "a"}, occurred_at="not-a-timestamp"),
        ]
        [record] = reconstruct_streams(events)
        assert [frame.offset for frame in record["frames"]] == [0.0]

    def test_truncated_stream_is_reported_not_silently_completed(self):
        [record] = reconstruct_streams(streamed_run(complete=False))
        assert record["truncated"] is True
        assert record["status"] == "pending"
        assert record["text"] == "Hello, world"

    def test_failed_stream_is_not_truncated(self):
        events = streamed_run(complete=False)
        events.append(
            event("fail", 4, "model.failed", invocation="inv-m", parents=("c2",),
                  payload={"error": {"code": "provider_error"}})
        )
        [record] = reconstruct_streams(events)
        assert record["status"] == "failed"
        assert record["truncated"] is False

    def test_parallel_streams_are_kept_separate(self):
        events = [
            event("a0", 0, "model.requested", invocation="inv-a", payload={"model": "m"}),
            event("a1", 1, "model.chunk", invocation="inv-a", parents=("a0",), payload={"chunk": "A1"}),
            event("b0", 0, "model.requested", stream=STREAM_B, invocation="inv-b", payload={"model": "m"}),
            event("b1", 1, "model.chunk", stream=STREAM_B, invocation="inv-b",
                  parents=("b0",), payload={"chunk": "B1"}),
            event("a2", 2, "model.chunk", invocation="inv-a", parents=("a1",), payload={"chunk": "A2"}),
        ]
        records = reconstruct_streams(events)
        assert {record["invocation_id"]: record["text"] for record in records} == {
            "inv-a": "A1A2",
            "inv-b": "B1",
        }

    def test_chunk_without_a_request_still_reconstructs(self):
        events = [
            event("c0", 0, "model.chunk", invocation="inv-orphan", payload={"chunk": "x"}),
        ]
        [record] = reconstruct_streams(events)
        assert record["text"] == "x"
        assert record["truncated"] is True
        assert record["model"] is None

    def test_chunk_without_invocation_identity_fails_closed(self):
        events = [event("c0", 0, "model.chunk", payload={"chunk": "x"})]
        with pytest.raises(StreamingError, match="without an invocation_id"):
            reconstruct_streams(events)

    @pytest.mark.parametrize("delta", [None, 5, {"text": "x"}, ["x"], True])
    def test_non_string_chunk_payload_fails_closed(self, delta):
        events = [
            event("c0", 0, "model.chunk", invocation="inv-m", payload={"chunk": delta}),
        ]
        with pytest.raises(StreamingError, match="non-string chunk payload"):
            reconstruct_streams(events)

    def test_non_streaming_recording_yields_no_records(self):
        events = [event("e0", 0, "run.started"), event("e1", 1, "run.completed")]
        assert reconstruct_streams(events) == []

    def test_empty_delta_is_a_legitimate_keepalive_chunk(self):
        events = [
            event("m0", 0, "model.requested", invocation="inv-m", payload={"model": "m"}),
            event("c0", 1, "model.chunk", invocation="inv-m", parents=("m0",), payload={"chunk": ""}),
        ]
        [record] = reconstruct_streams(events)
        assert record["text"] == ""
        assert len(record["frames"]) == 1

    def test_reconstruction_does_not_mutate_input(self):
        events = streamed_run()
        snapshot = canonical_hash(events)
        reconstruct_streams(events)
        assert canonical_hash(events) == snapshot


class TestPlaybackClock:
    def frames(self) -> list[Frame]:
        return reconstruct_streams(streamed_run())[0]["frames"]

    @pytest.mark.parametrize("speed", [0.25, 0.5, 1.0, 2.0, 10.0, 1000.0])
    def test_speed_changes_timing_only_never_content(self, speed):
        """The decisive guarantee: identical frames at every speed."""
        baseline = PlaybackSession(self.frames(), clock=ManualClock()).drain()
        clock = ManualClock()
        session = PlaybackSession(self.frames(), clock=clock, speed=speed)
        assert session.drain() == baseline
        assert session.transcript() == "Hello, world"

    def test_speed_scales_simulated_delay_exactly(self):
        slow_clock, fast_clock = ManualClock(), ManualClock()
        PlaybackSession(self.frames(), clock=slow_clock, speed=1.0).drain()
        PlaybackSession(self.frames(), clock=fast_clock, speed=2.0).drain()
        assert sum(slow_clock.sleeps) == pytest.approx(2.0)
        assert sum(fast_clock.sleeps) == pytest.approx(1.0)

    def test_simulated_elapsed_is_not_measured_wall_time(self):
        clock = ManualClock()
        session = PlaybackSession(self.frames(), clock=clock, speed=1.0)
        session.drain()
        assert session.simulated_elapsed == pytest.approx(2.0)
        assert clock.now() == pytest.approx(2.0)

    def test_pause_stops_emission_without_losing_position(self):
        session = PlaybackSession(self.frames(), clock=ManualClock())
        assert session.step().delta == "Hel"
        session.pause()
        assert list(session.play()) == []
        assert session.position == 1
        assert session.transcript() == "Hel"
        session.resume()
        assert [frame.delta for frame in session.play()] == ["lo, ", "world"]

    def test_constructing_paused_emits_nothing_until_resumed(self):
        session = PlaybackSession(self.frames(), clock=ManualClock(), paused=True)
        assert list(session.play()) == []
        assert session.transcript() == ""
        assert len(session.drain()) == 3

    def test_step_advances_exactly_one_frame_even_while_paused(self):
        session = PlaybackSession(self.frames(), clock=ManualClock(), paused=True)
        frame = session.step()
        assert frame.index == 0
        assert session.position == 1
        assert session.paused is True

    def test_stepping_to_the_end_matches_draining(self):
        stepped = PlaybackSession(self.frames(), clock=ManualClock())
        collected = []
        while True:
            frame = stepped.step()
            if frame is None:
                break
            collected.append(frame)
        assert collected == PlaybackSession(self.frames(), clock=ManualClock()).drain()

    def test_step_past_the_end_returns_none_and_is_idempotent(self):
        session = PlaybackSession(self.frames(), clock=ManualClock())
        session.drain()
        assert session.step() is None
        assert session.step() is None
        assert session.finished is True

    def test_seek_skips_frames_without_emitting_them(self):
        clock = ManualClock()
        session = PlaybackSession(self.frames(), clock=clock)
        session.seek(2)
        assert session.transcript() == "Hello, "
        assert clock.sleeps == []
        assert [frame.delta for frame in session.play()] == ["world"]

    def test_seek_backwards_replays_identical_frames(self):
        session = PlaybackSession(self.frames(), clock=ManualClock())
        first = session.drain()
        session.seek(0)
        assert session.drain() == first

    def test_seek_out_of_range_is_rejected(self):
        session = PlaybackSession(self.frames(), clock=ManualClock())
        for position in (-1, 4):
            with pytest.raises(IndexError):
                session.seek(position)

    def test_close_stops_a_cancelled_presentation(self):
        session = PlaybackSession(self.frames(), clock=ManualClock())
        session.step()
        session.close()
        assert session.step() is None
        assert list(session.play()) == []
        assert session.finished is True

    def test_context_manager_closes_on_exit(self):
        with PlaybackSession(self.frames(), clock=ManualClock()) as session:
            session.step()
        assert session.finished is True
        assert session.step() is None

    @pytest.mark.parametrize("speed", [0, -1.0, 0.001, 1001.0])
    def test_out_of_range_speed_is_rejected(self, speed):
        with pytest.raises(ValueError, match="Speed must be between"):
            PlaybackSession(self.frames(), clock=ManualClock(), speed=speed)

    def test_speed_can_change_mid_presentation_without_altering_frames(self):
        session = PlaybackSession(self.frames(), clock=ManualClock(), speed=1.0)
        first = session.step()
        session.speed = 10.0
        rest = session.drain()
        assert [first.delta, *[frame.delta for frame in rest]] == ["Hel", "lo, ", "world"]

    def test_empty_frame_sequence_is_immediately_finished(self):
        session = PlaybackSession([], clock=ManualClock())
        assert session.finished is True
        assert session.drain() == []
        assert session.transcript() == ""

    def test_manual_clock_rejects_negative_sleep(self):
        with pytest.raises(ValueError, match="negative duration"):
            ManualClock().sleep(-1)
