import pytest

from anvil_server.attempt_provenance import InvalidAttempt, validate_attempt, execute_attempt, verify_evidence, persist_evidence, load_evidence


def sample():
    return {"reexecution_id": "attempt", "recording_id": "recording",
            "agent_version_id": "agent", "image_digest": "anvil/runner@sha256:" + "a" * 64,
            "adapter": "google-adk", "adapter_version": "0.1.0",
            "model_mode": "recorded", "fixture_policy": "strict", "seed": 0,
            "budgets": {"wall_seconds": 120}}


def test_candidate_attempt_requires_explicit_provenance():
    validate_attempt(sample())


def test_attempt_evidence_migration_enforces_project_isolation():
    from pathlib import Path

    migration = (Path(__file__).parents[2] / "migrations/0008_attempt_evidence.sql").read_text()
    assert "FOREIGN KEY (job_id, project_id) REFERENCES jobs (id, project_id)" in migration
    assert "ALTER TABLE attempt_evidence FORCE ROW LEVEL SECURITY" in migration
    assert "CREATE POLICY attempt_evidence_project_isolation" in migration
    assert "INSERT INTO schema_migrations (version) VALUES ('0008_attempt_evidence')" in migration


@pytest.mark.asyncio
async def test_persistence_insert_is_idempotent_and_project_scoped(monkeypatch):
    from contextlib import asynccontextmanager
    from uuid import UUID

    captured = []

    class Connection:
        async def execute(self, statement, parameters):
            captured.append((str(statement), parameters))

    @asynccontextmanager
    async def connection(engine, project_id):
        captured.append(project_id)
        yield Connection()

    monkeypatch.setattr("anvil_server.attempt_provenance.project_connection", connection)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    evidence = {"outcome": "succeeded"}
    import hashlib
    import json

    evidence["sha256"] = hashlib.sha256(json.dumps(evidence, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    await persist_evidence(None, identifier, identifier, identifier, evidence)
    assert captured[0] == identifier
    assert "ON CONFLICT (job_id, digest) DO NOTHING" in captured[1][0]
    assert captured[1][1]["digest"] == evidence["sha256"]


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
    assert len(sealed[0]["sha256"]) == 64
    assert verify_evidence(sealed[0])
    changed = {**sealed[0], "outcome": "failed"}
    assert not verify_evidence(changed)


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


@pytest.mark.asyncio
async def test_attempt_timeout_seals_failure_evidence(monkeypatch):
    import asyncio

    sealed = []

    async def run():
        await asyncio.Future()

    async def seal(evidence):
        sealed.append(evidence)

    attempt = sample()
    attempt["budgets"] = {"wall_seconds": 1}
    evidence = await asyncio.wait_for(execute_attempt(attempt, run, seal), 2)
    assert evidence["outcome"] == "timed_out"
    assert sealed == [evidence]


@pytest.mark.asyncio
async def test_persistence_rejects_corrupt_evidence_before_database_access():
    from uuid import UUID

    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(InvalidAttempt, match="checksum"):
        await persist_evidence(None, identifier, identifier, identifier,
                               {"outcome": "succeeded", "sha256": "0" * 64})


@pytest.mark.asyncio
async def test_load_evidence_rejects_stored_checksum_mismatch(monkeypatch):
    from contextlib import asynccontextmanager
    from uuid import UUID

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"evidence": {"outcome": "succeeded", "sha256": "0" * 64}, "digest": "0" * 64}

    class Connection:
        async def execute(self, statement, params):
            assert "project_id = :project" in str(statement)
            return Result()

    @asynccontextmanager
    async def connection(engine, project_id):
        yield Connection()

    monkeypatch.setattr("anvil_server.attempt_provenance.project_connection", connection)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(InvalidAttempt, match="checksum mismatch"):
        await load_evidence(None, identifier, identifier, identifier)
