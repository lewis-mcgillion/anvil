from datetime import datetime, timezone
from uuid import UUID

import pytest

from anvil_server.events import (Conflict, Event, MAX_BATCH_EVENTS, MAX_EVENT_BYTES,
                                 event_hash, validate_batch)


RUN = UUID("0199b167-0000-7000-8000-000000000021")
STREAM = UUID("0199b167-0000-7000-8000-000000000031")


def sample(sequence=0):
    return Event(schema_version="1", event_id=UUID("0199b167-0000-7000-8000-000000000041"),
                 run_id=RUN, stream_id=STREAM, sequence=sequence, type="message.created",
                 occurred_at=datetime.now(timezone.utc), payload={"a": 1, "b": 2})


def test_event_hash_canonicalizes_dict_keys():
    first = sample()
    second = first.model_copy(update={"payload": {"b": 2, "a": 1}})
    assert event_hash(first) == event_hash(second)


def test_batch_duplicate_rejected():
    event = sample()
    with pytest.raises(Conflict):
        validate_batch(RUN, STREAM, [event, event])


def test_batch_wrong_stream_rejected():
    with pytest.raises(Conflict):
        validate_batch(RUN, UUID("0199b167-0000-7000-8000-000000000032"), [sample()])


def test_batch_size_limits_reject_before_database_access():
    with pytest.raises(ValueError, match="batch size"):
        validate_batch(RUN, STREAM, [])
    with pytest.raises(ValueError, match="batch size"):
        validate_batch(RUN, STREAM, [sample()] * (MAX_BATCH_EVENTS + 1))


def test_single_oversized_event_rejected_before_database_access():
    oversized = sample().model_copy(update={"payload": {"text": "x" * MAX_EVENT_BYTES}})
    with pytest.raises(ValueError, match="ingest limit"):
        validate_batch(RUN, STREAM, [oversized])
