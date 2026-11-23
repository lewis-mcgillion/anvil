import pytest

from anvil_server.attempt_provenance import InvalidAttempt, validate_attempt


def sample():
    return {"reexecution_id": "attempt", "recording_id": "recording",
            "agent_version_id": "agent", "image_digest": "anvil/runner@sha256:" + "a" * 64,
            "adapter": "google-adk", "adapter_version": "0.1.0",
            "model_mode": "recorded", "fixture_policy": "strict", "seed": 0,
            "budgets": {"wall_seconds": 120}}


def test_candidate_attempt_requires_explicit_provenance():
    validate_attempt(sample())


@pytest.mark.parametrize("key,value", [
    ("image_digest", "runner:latest"), ("model_mode", "implicit"),
    ("fixture_policy", "best_effort"), ("seed", -1),
    ("adapter", ""), ("budgets", {}),
])
def test_incomplete_or_unsafe_candidate_is_rejected(key, value):
    attempt = sample()
    attempt[key] = value
    with pytest.raises(InvalidAttempt):
        validate_attempt(attempt)
