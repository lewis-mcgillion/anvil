import pytest

from anvil_server.auth import allows, bearer_token, issue_key, key_hash, validate_scopes
from anvil_server.pagination import CursorCodec
from anvil_server.routes import event_cursor_position


def test_secret_issuance_and_scopes():
    first, second = issue_key(), issue_key()
    assert first != second and key_hash(first) != key_hash(second)
    assert bearer_token("Bearer " + first) == first
    assert allows(["capture:write"], "capture:write")
    assert not allows(["capture:write"], "runs:read")
    with pytest.raises(ValueError):
        validate_scopes(["capture:write", "replay:write"])
    with pytest.raises(ValueError):
        validate_scopes(["admin", "admin"])


def test_signed_cursor_is_scoped_and_tamper_evident():
    codec = CursorCodec(b"0123456789abcdef")
    value = codec.encode("runs", "project-1", "last-id", {"state": "failed"})
    assert codec.decode(value, "runs", "project-1", {"state": "failed"}) == "last-id"
    for resource, scope, filters in [("jobs", "project-1", {"state": "failed"}),
                                     ("runs", "project-2", {"state": "failed"}),
                                     ("runs", "project-1", {})]:
        with pytest.raises(ValueError):
            codec.decode(value, resource, scope, filters)
    with pytest.raises(ValueError):
        codec.decode(value[:-3] + "abc", "runs", "project-1", {"state": "failed"})


def test_event_cursor_position_rejects_invalid_sequence_types():
    identifier = "0199b167-0000-7000-8000-000000000031"
    assert event_cursor_position([identifier, 0, identifier]) == [identifier, 0, identifier]
    for sequence in (-1, 2**63, True, "1", 1.5, None):
        with pytest.raises(ValueError):
            event_cursor_position([identifier, sequence, identifier])
