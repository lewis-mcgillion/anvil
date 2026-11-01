"""ANVIL-067: the server's mandatory capture policy.

The canary is a single recognisable string. Every test asserts it is absent
from whatever surface is under examination, so a regression shows up as the
secret itself appearing somewhere, which is the failure that actually matters.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import pytest

from anvil_server.events import Event, apply_policy
from anvil_server.policy import MARKER, PolicyError, enforce

CANARY = "sk-canary0123456789abcdefghij"
RUN = UUID("0199b167-0000-7000-8000-000000000021")
STREAM = UUID("0199b167-0000-7000-8000-000000000031")


def event(payload, *, event_type="tool.requested", replay_safe=True):
    return Event(schema_version="1", event_id=UUID("0199b167-0000-7000-8000-00000000e001"),
                 run_id=RUN, stream_id=STREAM, sequence=0, type=event_type,
                 occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                 payload=payload, replay_safe=replay_safe)


class TestCredentialKeys:
    """A credential-named key is removed whatever its value looks like."""

    @pytest.mark.parametrize("key", [
        "authorization", "Authorization", "api_key", "apiKey", "API-KEY",
        "access_token", "refresh_token", "id_token", "client_secret", "secret",
        "password", "passwd", "passphrase", "private_key", "session_key",
        "cookie", "Set-Cookie", "credential", "bearer",
    ])
    def test_a_credential_named_field_never_survives(self, key):
        result, redacted = enforce("tool.requested", {"arguments": {key: CANARY}})
        assert CANARY not in json.dumps(result)
        assert result["arguments"][key] == MARKER
        assert redacted == [f"/arguments/{key}"]

    def test_a_redacted_field_is_replaced_not_dropped(self):
        # Dropping it would make a removed credential indistinguishable from
        # one that was never sent, which hides a real policy violation.
        result, _ = enforce("tool.requested", {"arguments": {"api_key": CANARY}})
        assert "api_key" in result["arguments"]

    def test_an_innocent_field_is_untouched(self):
        result, redacted = enforce("tool.requested",
                                   {"tool_name": "lookup", "arguments": {"sku": "ABC"}})
        assert result["arguments"]["sku"] == "ABC"
        assert redacted == []

    @pytest.mark.parametrize("key", ["tokenizer", "sort_key", "keyboard",
                                     "secretariat", "cookbook", "authority"])
    def test_a_key_that_merely_contains_a_credential_word_is_kept(self, key):
        # A false positive silently destroys real evidence, and nobody finds
        # out until they need the data, so matching is word-aware.
        result, redacted = enforce("tool.requested", {"arguments": {key: "value"}})
        assert result["arguments"][key] == "value"
        assert redacted == []


class TestCredentialValues:
    """A credential-shaped value is removed even under an innocent key."""

    @pytest.mark.parametrize("value", [
        "Bearer abcdefghijklmnop",
        "sk-abcdefghijklmnopqrstuvwx",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "xoxb-1234567890-abcdefghij",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
        "api_key=supersecretvalue",
    ])
    def test_a_credential_shaped_value_is_removed(self, value):
        result, redacted = enforce("tool.requested", {"arguments": {"note": value}})
        assert value not in json.dumps(result)
        assert redacted == ["/arguments/note"]

    def test_a_credential_inside_prose_is_removed_without_losing_the_prose(self):
        text = f"retrying with Bearer {'a' * 20} after a timeout"
        result, _ = enforce("tool.requested", {"arguments": {"note": text}})
        assert "a" * 20 not in result["arguments"]["note"]
        assert result["arguments"]["note"].startswith("retrying with ")
        assert result["arguments"]["note"].endswith(" after a timeout")

    def test_ordinary_text_is_not_mangled(self):
        prose = "looked up SKU ABC and returned 3 results"
        result, redacted = enforce("tool.requested", {"arguments": {"note": prose}})
        assert result["arguments"]["note"] == prose and redacted == []


class TestStructures:
    def test_credentials_are_found_at_any_depth(self):
        payload = {"arguments": {"a": [{"b": {"headers": {"authorization": CANARY}}}]}}
        result, redacted = enforce("tool.requested", payload)
        assert CANARY not in json.dumps(result)
        assert redacted == ["/arguments/a/0/b/headers/authorization"]

    def test_pointers_escape_reserved_characters(self):
        result, redacted = enforce("tool.requested", {"arguments": {"a/b~c": {"token": CANARY}}})
        assert redacted == ["/arguments/a~1b~0c/token"]
        assert CANARY not in json.dumps(result)

    def test_excessive_nesting_is_refused(self):
        payload: dict = {"arguments": {}}
        node = payload["arguments"]
        for _ in range(80):
            node["next"] = {}
            node = node["next"]
        with pytest.raises(PolicyError):
            enforce("tool.requested", payload)

    def test_the_callers_payload_is_never_mutated(self):
        # A rejected batch must not leave a half-scrubbed object behind.
        payload = {"arguments": {"api_key": CANARY}}
        enforce("tool.requested", payload)
        assert payload["arguments"]["api_key"] == CANARY

    def test_non_string_keys_are_refused(self):
        with pytest.raises(PolicyError):
            enforce("tool.requested", {"arguments": {1: "value"}})


class TestReplayEligibility:
    """Redacting removes replay eligibility, because the evidence is gone."""

    def test_server_redaction_marks_the_payload_unsafe(self):
        result, _ = enforce("tool.requested",
                            {"arguments": {"api_key": CANARY}, "replay_safe": True})
        assert result["replay_safe"] is False

    def test_an_untouched_payload_keeps_its_declared_eligibility(self):
        result, _ = enforce("tool.requested",
                            {"arguments": {"sku": "ABC"}, "replay_safe": True})
        assert result["replay_safe"] is True

    def test_the_policy_can_never_grant_eligibility(self):
        result, _ = enforce("tool.requested",
                            {"arguments": {"sku": "ABC"}, "replay_safe": False})
        assert result["replay_safe"] is False

    def test_client_declared_redactions_are_preserved(self):
        # The server cannot observe what was removed before export, and must
        # not drop that fact from the record.
        result, _ = enforce("tool.requested",
                            {"arguments": {"api_key": CANARY}, "redactions": ["/result"]})
        assert result["redactions"] == ["/arguments/api_key", "/result"]

    def test_a_malformed_redaction_list_is_refused(self):
        with pytest.raises(PolicyError):
            enforce("tool.requested", {"arguments": {}, "redactions": "/result"})


class TestBatchEnforcement:
    """No write path reaches storage around the policy."""

    def test_every_event_in_a_batch_is_enforced(self):
        events = [event({"tool_name": "t", "arguments": {"api_key": CANARY}})]
        enforced = apply_policy(events)
        assert CANARY not in json.dumps(enforced[0].payload)
        assert enforced[0].replay_safe is False

    def test_enforcement_does_not_mutate_the_submitted_events(self):
        events = [event({"tool_name": "t", "arguments": {"api_key": CANARY}})]
        apply_policy(events)
        assert events[0].payload["arguments"]["api_key"] == CANARY

    def test_the_content_hash_commits_to_what_was_stored(self):
        from anvil_server.events import event_hash

        submitted = event({"tool_name": "t", "arguments": {"api_key": CANARY}})
        stored = apply_policy([submitted])[0]
        # Hashing the submitted payload would let a recording's identity
        # attest to bytes the server deliberately never kept.
        assert event_hash(stored) != event_hash(submitted)
        assert event_hash(stored) == event_hash(apply_policy([submitted])[0])

    def test_an_event_the_policy_cannot_make_safe_is_rejected(self):
        payload: dict = {"tool_name": "t", "arguments": {}}
        node = payload["arguments"]
        for _ in range(80):
            node["next"] = {}
            node = node["next"]
        with pytest.raises(PolicyError):
            apply_policy([event(payload)])


class TestErrorsDoNotLeak:
    def test_a_policy_error_never_quotes_the_payload(self):
        # An error that quotes what it rejected leaks the exact secret the
        # policy exists to remove, into logs and into the client's response.
        with pytest.raises(PolicyError) as raised:
            enforce("tool.requested", {"arguments": {"api_key": CANARY}, "redactions": CANARY})
        assert CANARY not in str(raised.value)
