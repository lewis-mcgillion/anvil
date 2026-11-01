"""Cross-platform golden projection hashes for the v1 conformance corpus.

ANVIL-036 requires that a recorded projection is reproducible across operating
systems, architectures, Python builds, locales and time zones. The corpus in
``fixtures/conformance/v1`` is language-neutral; this module pins the
*projection* of each recording so that any change to ordering, state reduction
or canonicalization is caught as an explicit golden mismatch rather than
silently altering published hashes.

Regenerate the goldens deliberately with::

    UPDATE_GOLDEN=1 python -m pytest server/tests/test_playback_conformance.py

Regeneration is a compatibility decision: ``projection_version`` must be
incremented whenever a golden changes for an existing fixture.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import sys

import pytest

from anvil_server.playback import (
    PROJECTION_VERSION,
    PlaybackError,
    ProjectionIndex,
    projection_hash,
)

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "conformance" / "v1"
GOLDEN = CORPUS / "projections.json"
MANIFEST = CORPUS / "manifest.json"


def _fixtures() -> list[dict]:
    return json.loads(MANIFEST.read_text())["fixtures"]


def _recording(entry: dict) -> dict:
    return json.loads((CORPUS / entry["file"]).read_text())


def _index(recording: dict) -> ProjectionIndex:
    return ProjectionIndex(
        recording["events"],
        version=PROJECTION_VERSION,
        run_id=recording["run"]["run_id"],
    )


def _describe(entry: dict) -> dict:
    """Return the golden description of one fixture, projected or rejected."""
    recording = _recording(entry)
    try:
        index = _index(recording)
    except PlaybackError as error:
        # A recording that cannot be projected must fail closed, and *which*
        # failure it produces is part of the published guarantee.
        return {
            "id": entry["id"],
            "projectable": False,
            "error": type(error).__name__,
        }
    return {
        "id": entry["id"],
        "projectable": True,
        "event_count": len(index),
        "checkpoint_positions": index.checkpoint_positions,
        "projection_hash": projection_hash(index.full()),
        "position_hashes": [
            projection_hash(index.at(position)) for position in range(len(index) + 1)
        ],
    }


def _generate() -> dict:
    return {
        "projection_manifest_version": "1",
        "projection_version": PROJECTION_VERSION,
        "canonicalization": "RFC8785",
        "hash": "SHA-256",
        "projections": [_describe(entry) for entry in _fixtures()],
    }


if os.environ.get("UPDATE_GOLDEN") == "1":  # pragma: no cover - maintenance path

    def test_regenerate_golden() -> None:
        GOLDEN.write_text(json.dumps(_generate(), indent=2, sort_keys=True) + "\n")
        pytest.skip("regenerated fixtures/conformance/v1/projections.json")


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def _golden_for(fixture_id: str) -> dict:
    for entry in _golden()["projections"]:
        if entry["id"] == fixture_id:
            return entry
    raise AssertionError(f"fixture {fixture_id!r} is missing from {GOLDEN}")


IDS = [entry["id"] for entry in _fixtures()]


def test_golden_covers_every_corpus_fixture() -> None:
    assert [entry["id"] for entry in _golden()["projections"]] == IDS


def test_golden_declares_the_current_projection_version() -> None:
    # A golden captured under a different projection version is not evidence
    # about this build.
    assert _golden()["projection_version"] == PROJECTION_VERSION


@pytest.mark.parametrize("fixture_id", IDS)
def test_projection_matches_published_golden(fixture_id: str) -> None:
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    assert _describe(entry) == _golden_for(fixture_id)


@pytest.mark.parametrize("fixture_id", IDS)
def test_projection_is_independent_of_input_order(fixture_id: str) -> None:
    """Ordering is derived from causality and sequence, never arrival order."""
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    recording = _recording(entry)
    golden = _golden_for(fixture_id)

    shuffled = list(recording["events"])
    random.Random(1979).shuffle(shuffled)
    recording["events"] = shuffled

    if not golden["projectable"]:
        with pytest.raises(PlaybackError):
            _index(recording)
        return
    assert projection_hash(_index(recording).full()) == golden["projection_hash"]


@pytest.mark.parametrize("fixture_id", IDS)
def test_projection_ignores_wall_clock_timestamps(fixture_id: str) -> None:
    """Reversing every timestamp must not move an event or change the hash."""
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    recording = _recording(entry)
    golden = _golden_for(fixture_id)

    stamps = [event.get("occurred_at") for event in recording["events"]]
    for event, stamp in zip(recording["events"], reversed(stamps)):
        if stamp is not None:
            event["occurred_at"] = stamp

    if not golden["projectable"]:
        with pytest.raises(PlaybackError):
            _index(recording)
        return
    assert projection_hash(_index(recording).full()) == golden["projection_hash"]


@pytest.mark.parametrize("fixture_id", IDS)
def test_projection_is_stable_under_locale_and_timezone(
    fixture_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different host time zone or locale must not change a published hash."""
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    golden = _golden_for(fixture_id)
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    monkeypatch.setenv("LC_ALL", "tr_TR.UTF-8")
    monkeypatch.setenv("LANG", "tr_TR.UTF-8")
    if hasattr(os, "tzset"):
        os.tzset()
    try:
        if not golden["projectable"]:
            with pytest.raises(PlaybackError):
                _index(_recording(entry))
            return
        assert projection_hash(_index(_recording(entry)).full()) == golden[
            "projection_hash"
        ]
    finally:
        monkeypatch.undo()
        if hasattr(os, "tzset"):
            os.tzset()


@pytest.mark.parametrize("fixture_id", IDS)
def test_repeated_projection_is_byte_identical(fixture_id: str) -> None:
    """Repeated runs in one process must not drift (no set/dict iteration leak)."""
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    golden = _golden_for(fixture_id)
    if not golden["projectable"]:
        pytest.skip("fixture is a published rejection")
    hashes = {projection_hash(_index(_recording(entry)).full()) for _ in range(5)}
    assert hashes == {golden["projection_hash"]}


@pytest.mark.parametrize("fixture_id", IDS)
def test_seeking_to_a_cursor_reproduces_the_published_position_hash(
    fixture_id: str,
) -> None:
    """Cursor seeking is pinned, so partial playback is reproducible too."""
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    golden = _golden_for(fixture_id)
    if not golden["projectable"]:
        pytest.skip("fixture is a published rejection")
    index = _index(_recording(entry))
    for position, expected in enumerate(golden["position_hashes"]):
        assert projection_hash(index.at(position)) == expected
        cursor = index.cursor(position)
        if cursor is not None:
            assert index.position_of(cursor) == position


@pytest.mark.parametrize("fixture_id", IDS)
def test_final_position_hash_equals_full_projection_hash(fixture_id: str) -> None:
    golden = _golden_for(fixture_id)
    if not golden["projectable"]:
        pytest.skip("fixture is a published rejection")
    assert golden["position_hashes"][-1] == golden["projection_hash"]
    assert len(golden["position_hashes"]) == golden["event_count"] + 1


def test_a_recording_with_a_capture_gap_is_published_as_a_rejection() -> None:
    """The corpus must keep proving that an incomplete capture is unprojectable."""
    rejected = [
        entry for entry in _golden()["projections"] if not entry["projectable"]
    ]
    assert {entry["id"] for entry in rejected} == {"missing-events"}
    assert rejected[0]["error"] == "SequenceError"


def test_golden_hashes_are_lowercase_sha256() -> None:
    for entry in _golden()["projections"]:
        if not entry["projectable"]:
            continue
        for value in [entry["projection_hash"], *entry["position_hashes"]]:
            assert len(value) == 64
            assert value == value.lower()
            int(value, 16)


def test_mutating_any_payload_changes_the_published_hash() -> None:
    """Goldens must actually be sensitive to recording content."""
    entry = next(item for item in _fixtures() if item["id"] == "parallel-calls")
    recording = _recording(entry)
    for event in recording["events"]:
        if event["type"] == "tool.requested":
            event["payload"]["arguments"]["sku"] += "!"
            break
    else:  # pragma: no cover - corpus guarantees a tool request
        pytest.fail("parallel-calls fixture has no tool request to mutate")
    assert (
        projection_hash(_index(recording).full())
        != _golden_for("parallel-calls")["projection_hash"]
    )


@pytest.mark.parametrize("fixture_id", IDS)
def test_dropping_the_last_event_changes_the_published_hash(fixture_id: str) -> None:
    """Every projectable fixture's golden must depend on its full event list."""
    entry = next(item for item in _fixtures() if item["id"] == fixture_id)
    golden = _golden_for(fixture_id)
    if not golden["projectable"] or golden["event_count"] < 2:
        pytest.skip("fixture is a published rejection or too short to truncate")
    recording = _recording(entry)
    recording["events"] = recording["events"][:-1]
    try:
        truncated = projection_hash(_index(recording).full())
    except PlaybackError:
        return  # Fails closed instead, which is an equally strong guarantee.
    assert truncated != golden["projection_hash"]


def test_an_unrequested_chunk_is_recorded_as_a_replay_blocker() -> None:
    """The streaming fixture's chunks have no model.requested parent.

    That is why its projection is insensitive to chunk text: an unrequested
    chunk cannot be attributed to an invocation, so playback records a replay
    blocker rather than inventing one. Pin that behaviour explicitly.
    """
    entry = next(item for item in _fixtures() if item["id"] == "streaming")
    projection = _index(_recording(entry)).full()
    reasons = [blocker["reason"] for blocker in projection["replay_blockers"]]
    assert reasons.count("model.chunk without a matching request") == 2


def test_projection_does_not_depend_on_hash_randomization() -> None:
    """Re-run projection in a fresh interpreter with a different PYTHONHASHSEED."""
    import subprocess

    entry = next(item for item in _fixtures() if item["id"] == "parallel-calls")
    script = (
        "import json,pathlib,sys;"
        f"sys.path.insert(0,{str(pathlib.Path(__file__).resolve().parents[1])!r});"
        "from anvil_server.playback import ProjectionIndex, projection_hash,"
        " PROJECTION_VERSION;"
        f"rec=json.loads(pathlib.Path({str(CORPUS / entry['file'])!r}).read_text());"
        "idx=ProjectionIndex(rec['events'],version=PROJECTION_VERSION,"
        "run_id=rec['run']['run_id']);"
        "print(projection_hash(idx.full()))"
    )
    seen = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        seen.add(result.stdout.strip())
    assert seen == {_golden_for("parallel-calls")["projection_hash"]}


def test_playback_reads_the_chunk_fields_that_producers_actually_emit() -> None:
    """Regression guard for a producer/consumer field-name split.

    The capture SDK and the ADK adapter both emit the ``chunk``/``chunk_index``
    payload published in ``schemas/v1``. Playback once read a ``delta`` field
    that no producer emitted, so every real recording's streamed content was
    silently dropped from the projection and rejected by stream
    reconstruction. Assert the published field names survive end to end.
    """
    from anvil_server.streaming import reconstruct_streams

    run_id = "00000000-0000-7000-8000-0000000009a0"
    stream_id = "00000000-0000-7000-8000-0000000009b0"
    invocation = "00000000-0000-7000-8000-0000000009c0"

    def event(suffix: str, sequence: int, kind: str, parents, payload):
        return {
            "schema_version": "1",
            "event_id": f"00000000-0000-7000-8000-0000000009{suffix}",
            "run_id": run_id,
            "stream_id": stream_id,
            "sequence": sequence,
            "type": kind,
            "occurred_at": f"2026-01-01T00:00:0{sequence}.000Z",
            "parent_event_ids": list(parents),
            "invocation_id": invocation,
            "payload": payload,
        }

    events = [
        event("d0", 1, "run.started", (), {}),
        event("d1", 2, "model.requested", ("00000000-0000-7000-8000-0000000009d0",),
              {"model": "m", "request": {}}),
        event("d2", 3, "model.chunk", ("00000000-0000-7000-8000-0000000009d1",),
              {"chunk_index": 0, "chunk": "Hel"}),
        event("d3", 4, "model.chunk", ("00000000-0000-7000-8000-0000000009d2",),
              {"chunk_index": 1, "chunk": "lo"}),
        event("d4", 5, "model.completed", ("00000000-0000-7000-8000-0000000009d3",),
              {"response": {}}),
        event("d5", 6, "run.completed", ("00000000-0000-7000-8000-0000000009d4",), {}),
    ]

    index = ProjectionIndex(events, version=PROJECTION_VERSION, run_id=run_id)
    projection = index.full()
    model = [
        record
        for record in projection["invocations"]
        if record["invocation_id"] == invocation and record["kind"] == "model"
    ]
    assert len(model) == 1
    chunks = model[0]["chunks"]
    assert [chunk["chunk"] for chunk in chunks] == ["Hel", "lo"]
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1]
    # No content may be dropped: the projection must not be identical to one
    # built from chunks carrying no recognised payload at all.
    blank = [dict(item) for item in events]
    for item in blank:
        if item["type"] == "model.chunk":
            item["payload"] = {"chunk_index": item["payload"]["chunk_index"]}
    assert projection_hash(
        ProjectionIndex(blank, version=PROJECTION_VERSION, run_id=run_id).full()
    ) != projection_hash(projection)

    streams = reconstruct_streams(events)
    assert [record["text"] for record in streams] == ["Hello"]


def test_sdk_and_adapter_emit_the_schema_chunk_payload() -> None:
    """The published schema is the single source of truth for the field names."""
    schemas = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "v1"
    event_schema = json.loads((schemas / "event.schema.json").read_text())
    payload = event_schema["$defs"]["modelChunkPayload"]
    assert set(payload["required"]) >= {"chunk", "chunk_index"}

    sdk = (
        pathlib.Path(__file__).resolve().parents[2]
        / "sdk" / "python" / "anvil" / "_client.py"
    ).read_text()
    assert '"chunk_index": index, "chunk": chunk' in sdk

    adapter = (
        pathlib.Path(__file__).resolve().parents[2]
        / "adapters" / "google-adk" / "src" / "anvil_adk" / "plugin.py"
    ).read_text()
    assert '"chunk_index": index, "chunk": response' in adapter
