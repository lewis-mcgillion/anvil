import pytest

from anvil_server.auth import allows, bearer_token, issue_key, key_hash, validate_scopes
from anvil_server.pagination import CursorCodec


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
