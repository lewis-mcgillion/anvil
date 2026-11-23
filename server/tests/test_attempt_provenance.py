import pytest

from anvil_server.attempt_provenance import InvalidAttempt, validate_attempt, execute_attempt


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
    ("reexecution_id", ""), ("budgets", {"wall_seconds": 0}),
    ("budgets", {"wall_seconds": True}),
    ("budgets", {"unexpected": 10}),
])
def test_incomplete_or_unsafe_candidate_is_rejected(key, value):
    attempt = sample()
    attempt[key] = value
    with pytest.raises(InvalidAttempt):
        validate_attempt(attempt)


@pytest.mark.asyncio
async def test_attempt_seals_success_and_runner_failure():
    sealed = []

    async def seal(evidence):
        sealed.append(evidence)

    async def success():
        return None

    async def failure():
        raise RuntimeError("sensitive internals")

    assert (await execute_attempt(sample(), success, seal))["outcome"] == "succeeded"
    assert (await execute_attempt(sample(), failure, seal))["failure_code"] == "runner_error"
    assert len(sealed) == 2
    assert "sensitive internals" not in str(sealed)


@pytest.mark.asyncio
async def test_attempt_seals_cancellation():
    import asyncio

    started = asyncio.Event()
    sealed = []

    async def run():
        started.set()
        await asyncio.Future()

    async def seal(evidence):
        sealed.append(evidence)

    task = asyncio.create_task(execute_attempt(sample(), run, seal))
    await started.wait()
    task.cancel()
    evidence = await asyncio.wait_for(task, 1)
    assert evidence["outcome"] == "cancelled"
    assert sealed == [evidence]


@pytest.mark.asyncio
async def test_attempt_cancellation_during_seal_does_not_discard_evidence():
    import asyncio

    sealing_started = asyncio.Event()
    release_seal = asyncio.Event()
    sealed = []

    async def run():
        return None

    async def seal(evidence):
        sealing_started.set()
        await release_seal.wait()
        sealed.append(evidence)

    task = asyncio.create_task(execute_attempt(sample(), run, seal))
    await sealing_started.wait()
    task.cancel()
    release_seal.set()
    evidence = await asyncio.wait_for(task, 1)
    assert evidence["outcome"] == "succeeded"
    assert sealed == [evidence]
