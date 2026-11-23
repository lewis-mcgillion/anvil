import pytest

from anvil_server.attempt_provenance import InvalidAttempt, validate_attempt, execute_attempt, verify_evidence, persist_evidence, load_evidence, run_leased_attempt, run_persisted_attempt, validate_registered_agent


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
async def test_attempt_does_not_mark_failed_runner_outcome_successful():
    sealed = []

    async def failed_runner():
        return 2

    async def seal(evidence):
        sealed.append(evidence)

    evidence = await execute_attempt(sample(), failed_runner, seal)
    assert evidence["outcome"] == "failed"
    assert evidence["failure_code"] == "runner_outcome"
    assert verify_evidence(evidence)
    assert sealed == [evidence]


@pytest.mark.asyncio
async def test_attempt_rejects_boolean_false_result():
    sealed = []

    async def failed_runner():
        return False

    async def seal(evidence):
        sealed.append(evidence)

    evidence = await execute_attempt(sample(), failed_runner, seal)
    assert evidence["outcome"] == "failed"
    assert evidence["failure_code"] == "runner_outcome"
    assert sealed == [evidence]


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


@pytest.mark.asyncio
async def test_load_evidence_returns_verified_record(monkeypatch):
    from contextlib import asynccontextmanager
    from uuid import UUID
    import hashlib
    import json

    evidence = {"outcome": "cancelled", "duration_ms": 12}
    evidence["sha256"] = hashlib.sha256(json.dumps(evidence, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"evidence": evidence, "digest": evidence["sha256"]}

    class Connection:
        async def execute(self, statement, params):
            return Result()

    @asynccontextmanager
    async def connection(engine, project_id):
        yield Connection()

    monkeypatch.setattr("anvil_server.attempt_provenance.project_connection", connection)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    assert await load_evidence(None, identifier, identifier, identifier) == evidence


@pytest.mark.asyncio
async def test_leased_attempt_seals_before_completing_job():
    from uuid import UUID

    events = []

    class Queue:
        async def acquire_job(self, *args):
            events.append("acquire")
            return {"id": args[1]}

        async def complete(self, *args):
            events.append(("complete", args[-1]))

    async def run():
        events.append("run")

    async def seal(evidence):
        events.append("seal")

    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    result = await run_leased_attempt(Queue(), identifier, identifier, identifier, sample(), run, seal)
    assert result["outcome"] == "succeeded"
    assert events == ["acquire", "run", "seal", ("complete", "succeeded")]


@pytest.mark.asyncio
async def test_lease_renewal_failure_cancels_execution_before_completion(monkeypatch):
    import asyncio
    from uuid import UUID

    cancelled = asyncio.Event()
    completed = []

    class Queue:
        async def acquire_job(self, *args):
            return {"id": args[1]}

        async def heartbeat(self, *args):
            raise RuntimeError("lease lost")

        async def complete(self, *args):
            completed.append(args)

    async def run():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def seal(evidence):
        pass

    original_sleep = asyncio.sleep

    async def immediate_sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr("anvil_server.attempt_provenance.asyncio.sleep", immediate_sleep)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(RuntimeError, match="lease lost"):
        await asyncio.wait_for(run_leased_attempt(
            Queue(), identifier, identifier, identifier, sample(), run, seal,
        ), 1)
    assert cancelled.is_set()
    assert completed == []


@pytest.mark.asyncio
async def test_heartbeat_interval_scales_with_short_lease(monkeypatch):
    import asyncio
    from datetime import timedelta
    from uuid import UUID

    intervals = []

    class Queue:
        lease_duration = timedelta(seconds=3)

        async def acquire_job(self, *args):
            return {"id": args[1]}

        async def heartbeat(self, *args):
            raise RuntimeError("renewed")

    async def run():
        await asyncio.Future()

    async def seal(evidence):
        pass

    original_sleep = asyncio.sleep

    async def observe_sleep(seconds):
        intervals.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr("anvil_server.attempt_provenance.asyncio.sleep", observe_sleep)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(RuntimeError, match="renewed"):
        await run_leased_attempt(Queue(), identifier, identifier, identifier, sample(), run, seal)
    assert intervals == [1.0]


@pytest.mark.asyncio
async def test_heartbeat_continues_while_job_completion_is_pending(monkeypatch):
    import asyncio
    from datetime import timedelta
    from uuid import UUID

    completing = asyncio.Event()
    renewed = asyncio.Event()

    class Queue:
        lease_duration = timedelta(seconds=3)

        async def acquire_job(self, *args):
            return {"id": args[1]}

        async def heartbeat(self, *args):
            renewed.set()

        async def complete(self, *args):
            completing.set()
            await renewed.wait()

    async def run():
        return None

    async def seal(evidence):
        pass

    original_sleep = asyncio.sleep

    async def immediate_sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr("anvil_server.attempt_provenance.asyncio.sleep", immediate_sleep)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    result = await asyncio.wait_for(run_leased_attempt(
        Queue(), identifier, identifier, identifier, sample(), run, seal,
    ), 1)
    assert result["outcome"] == "succeeded"
    assert completing.is_set() and renewed.is_set()


@pytest.mark.asyncio
async def test_lease_loss_during_completion_cancels_terminal_update(monkeypatch):
    import asyncio
    from datetime import timedelta
    from uuid import UUID

    completing = asyncio.Event()
    cancelled = asyncio.Event()

    class Queue:
        lease_duration = timedelta(seconds=3)

        async def acquire_job(self, *args):
            return {"id": args[1]}

        async def heartbeat(self, *args):
            await completing.wait()
            raise RuntimeError("lease expired")

        async def complete(self, *args):
            completing.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    async def run():
        return None

    async def seal(evidence):
        pass

    original_sleep = asyncio.sleep

    async def immediate_sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr("anvil_server.attempt_provenance.asyncio.sleep", immediate_sleep)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(RuntimeError, match="lease expired"):
        await asyncio.wait_for(run_leased_attempt(
            Queue(), identifier, identifier, identifier, sample(), run, seal,
        ), 1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_unavailable_lease_never_runs_candidate():
    from uuid import UUID

    class Queue:
        async def acquire_job(self, *args):
            return None

    async def forbidden():
        raise AssertionError("Runner must not execute without a lease")

    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    assert await run_leased_attempt(Queue(), identifier, identifier, identifier,
                                    sample(), forbidden, forbidden) is None


@pytest.mark.asyncio
async def test_invalid_attempt_cannot_consume_job_lease():
    from uuid import UUID

    class Queue:
        async def acquire_job(self, *args):
            raise AssertionError("Invalid attempt must not acquire lease")

    async def forbidden():
        raise AssertionError("Invalid attempt must not run")

    details = sample()
    details["budgets"] = {"model_calls": 2}
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(InvalidAttempt, match="wall-time"):
        await run_leased_attempt(Queue(), identifier, identifier, identifier,
                                 details, forbidden, forbidden)


@pytest.mark.asyncio
async def test_persisted_attempt_writes_evidence_before_job_completion(monkeypatch):
    from uuid import UUID

    events = []

    class Queue:
        async def acquire_job(self, *args):
            events.append("acquire")
            return {"id": args[1]}

        async def complete(self, *args):
            events.append("complete")

    async def persist(*args):
        events.append("persist")
        assert verify_evidence(args[-1])

    async def run():
        events.append("run")

    async def registered(*args):
        events.append("registered")

    monkeypatch.setattr("anvil_server.attempt_provenance.persist_evidence", persist)
    monkeypatch.setattr("anvil_server.attempt_provenance.validate_registered_agent", registered)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    details = sample()
    details["agent_version_id"] = str(identifier)
    result = await run_persisted_attempt(None, Queue(), identifier, identifier,
                                         identifier, details, run)
    assert result["outcome"] == "succeeded"
    assert events == ["registered", "acquire", "run", "persist", "complete"]


@pytest.mark.asyncio
async def test_persisted_attempt_does_not_complete_job_when_seal_fails(monkeypatch):
    from uuid import UUID

    completed = []

    class Queue:
        async def acquire_job(self, *args):
            return {"id": args[1]}

        async def complete(self, *args):
            completed.append(args)

    async def persist(*args):
        raise RuntimeError("database unavailable")

    async def run():
        pass

    monkeypatch.setattr("anvil_server.attempt_provenance.persist_evidence", persist)
    async def registered(*args):
        return None

    monkeypatch.setattr("anvil_server.attempt_provenance.validate_registered_agent", registered)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    details = sample()
    details["agent_version_id"] = str(identifier)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await run_persisted_attempt(None, Queue(), identifier, identifier,
                                    identifier, details, run)
    assert completed == []


@pytest.mark.asyncio
async def test_registered_agent_must_match_candidate_manifest(monkeypatch):
    from contextlib import asynccontextmanager
    from uuid import UUID

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"image": "unexpected", "adapter": "google-adk", "adapter_version": "0.1.0"}

    class Connection:
        async def execute(self, statement, params):
            assert "project_id = :project" in str(statement)
            return Result()

    @asynccontextmanager
    async def connection(engine, project_id):
        yield Connection()

    monkeypatch.setattr("anvil_server.attempt_provenance.project_connection", connection)
    identifier = UUID("0199b167-0000-7000-8000-000000000011")
    with pytest.raises(InvalidAttempt, match="registered"):
        await validate_registered_agent(None, identifier, identifier, sample())
