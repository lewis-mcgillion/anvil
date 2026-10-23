"""Real PostgreSQL assertions activated by ANVIL_TEST_DATABASE_URL."""

import os
import base64
from datetime import datetime, timedelta, timezone
from uuid import UUID
import secrets
import time

import pytest
import pytest_asyncio
from sqlalchemy import text

from anvil_server.database import engine_for, project_connection
from anvil_server.events import Conflict, Event, accept_batch
from anvil_server.jobs import JobQueue, LeaseLost
from anvil_server.capture import create_run, ingest, seal_run
from anvil_server.capture import queue_seal
from anvil_server.worker import process_seal
from anvil_server.auth import issue_key, key_hash
from anvil_server.app import app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_agent_version_requires_pinned_image_and_project_scope(engine):
    token = issue_key()
    principal, key_id = new_uuid7(), new_uuid7()
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO principals(id, organization_id, subject, kind)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :subject, 'service')
        """), {"id": principal, "subject": str(principal)})
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            INSERT INTO api_keys(id, organization_id, project_id, principal_id,
              name, key_prefix, key_hash, scopes)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :project,
              :principal, :name, :prefix, :hash, ARRAY['replay:write'])
        """), {"id": key_id, "project": PROJECT, "principal": principal,
               "name": str(key_id), "prefix": token[:16], "hash": key_hash(token)})
    app.state.engine = engine
    payload = {"image": "registry.example/agent@sha256:" + "a" * 64,
               "entrypoint": "/app/agent", "adapter": "adk", "adapter_version": "1"}
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/v1/projects/{PROJECT}/agent-versions"
        invalid = await client.post(url, json={**payload, "image": "registry.example/agent:latest"}, headers=headers)
        assert invalid.status_code == 400
        created = await client.post(url, json=payload, headers=headers)
        assert created.status_code == 201, created.text
        repeated = await client.post(url, json=payload, headers=headers)
        assert repeated.status_code == 201, repeated.text
        assert created.json()["id"] == repeated.json()["id"]
        wrong_project = await client.post(
            "/v1/projects/0199b167-0000-7000-8000-000000000012/agent-versions",
            json=payload, headers=headers)
        assert wrong_project.status_code == 401
    async with project_connection(engine, PROJECT) as connection:
        assert (await connection.execute(text("""
            SELECT count(*) FROM agent_versions WHERE project_id = :project AND image = :image
        """), {"project": PROJECT, "image": payload["image"]})).scalar_one() == 1


PROJECT = UUID("0199b167-0000-7000-8000-000000000011")
RUN = UUID("0199b167-0000-7000-8000-000000000021")
STREAM = UUID("0199b167-0000-7000-8000-000000000031")


def new_uuid7():
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    return UUID(int=(timestamp << 80) | (7 << 76) | (secrets.randbits(12) << 64)
                | (2 << 62) | secrets.randbits(62))


@pytest_asyncio.fixture
async def engine():
    url = os.environ.get("ANVIL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ANVIL_TEST_DATABASE_URL not configured")
    engine = engine_for(url)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_event_retry_and_conflict(engine):
    async with project_connection(engine, PROJECT) as connection:
        next_sequence = (await connection.execute(text("SELECT next_sequence FROM run_streams WHERE id = :stream"), {"stream": STREAM})).scalar_one()
    event = Event(schema_version="1", event_id=new_uuid7(),
                  run_id=RUN, stream_id=STREAM, sequence=next_sequence, type="message.created",
                  occurred_at=datetime.now(timezone.utc), payload={"message": "safe"})
    assert await accept_batch(engine, PROJECT, RUN, STREAM, [event]) == {"accepted": 1, "duplicates": 0}
    assert await accept_batch(engine, PROJECT, RUN, STREAM, [event]) == {"accepted": 0, "duplicates": 1}
    with pytest.raises(Conflict):
        await accept_batch(engine, PROJECT, RUN, STREAM, [event.model_copy(update={"payload": {"message": "changed"}})])
    async with project_connection(engine, PROJECT) as connection:
        result = (await connection.execute(text("SELECT count(*) FROM events WHERE run_id = :run"), {"run": RUN})).scalar_one()
    assert result >= 1


@pytest.mark.asyncio
async def test_durable_lease_is_exclusive(engine):
    queue = JobQueue(engine, timedelta(seconds=30))
    job = new_uuid7()
    first_token = new_uuid7()
    other_token = new_uuid7()
    await queue.create(PROJECT, job, "export")
    assert (await queue.acquire_job(PROJECT, job, first_token))["id"] == job
    assert await queue.acquire_job(PROJECT, job, other_token) is None
    with pytest.raises(LeaseLost):
        await queue.complete(PROJECT, job, other_token, "succeeded")
    await queue.heartbeat(PROJECT, job, first_token)
    await queue.complete(PROJECT, job, first_token, "succeeded")
    with pytest.raises(LeaseLost):
        await queue.cancel(PROJECT, job)


@pytest.mark.asyncio
async def test_kind_specific_job_claim(engine):
    queue = JobQueue(engine, timedelta(seconds=30))
    job_id, token = new_uuid7(), new_uuid7()
    await queue.create(PROJECT, job_id, "deletion")
    claimed = await queue.acquire_job(PROJECT, job_id, token)
    assert claimed["id"] == job_id
    await queue.complete(PROJECT, job_id, token, "cancelled")


@pytest.mark.asyncio
async def test_expired_cancelled_lease_never_requeues(engine):
    queue = JobQueue(engine, timedelta(seconds=30))
    job_id, token = new_uuid7(), new_uuid7()
    await queue.create(PROJECT, job_id, "seal")
    await queue.acquire_job(PROJECT, job_id, token)
    assert (await queue.cancel(PROJECT, job_id))["status"] == "cancelling"
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE project_id = :project AND id = :job
        """), {"project": PROJECT, "job": job_id})
    assert await queue.recover_expired(PROJECT) >= 1
    assert (await queue.get(PROJECT, job_id))["status"] == "cancelled"
    assert await queue.acquire_job(PROJECT, job_id, new_uuid7()) is None


@pytest.mark.asyncio
async def test_exhausted_lease_is_terminal_and_fenced(engine):
    queue = JobQueue(engine, timedelta(seconds=30))
    job_id, token = new_uuid7(), new_uuid7()
    await queue.create(PROJECT, job_id, "seal")
    await queue.acquire_job(PROJECT, job_id, token)
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            UPDATE jobs SET max_attempts = 1,
                lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE project_id = :project AND id = :job
        """), {"project": PROJECT, "job": job_id})
    await queue.recover_expired(PROJECT)
    assert (await queue.get(PROJECT, job_id))["status"] == "failed"
    with pytest.raises(LeaseLost):
        await queue.complete(PROJECT, job_id, token, "succeeded")
    assert await queue.acquire_job(PROJECT, job_id, new_uuid7()) is None


@pytest.mark.asyncio
async def test_recorded_capture_to_immutable_seal(engine):
    run_id, stream_id = await create_run(engine, PROJECT)
    event = Event(schema_version="1", event_id=new_uuid7(),
                  run_id=run_id, stream_id=stream_id, sequence=0,
                  type="run.completed", occurred_at=datetime.now(timezone.utc),
                  payload={"outcome": "succeeded"}, replay_safe=True)
    assert (await ingest(engine, PROJECT, run_id, stream_id, [event]))["accepted"] == 1
    recording = await seal_run(engine, PROJECT, run_id)
    assert recording["manifest"]["capture_completeness"] == "sealed_complete"
    assert recording["manifest"]["replay_eligibility"] == "eligible"
    with pytest.raises(Exception):
        await ingest(engine, PROJECT, run_id, stream_id, [event])


@pytest.mark.asyncio
async def test_queued_seal_rejects_new_events_and_completed_retries(engine):
    run_id, stream_id = await create_run(engine, PROJECT)
    event = Event(schema_version="1", event_id=new_uuid7(), run_id=run_id,
                  stream_id=stream_id, sequence=0, type="run.completed",
                  occurred_at=datetime.now(timezone.utc), payload={}, replay_safe=True)
    await ingest(engine, PROJECT, run_id, stream_id, [event])
    job_id = await queue_seal(engine, PROJECT, run_id)
    with pytest.raises(Conflict):
        await ingest(engine, PROJECT, run_id, stream_id, [event])
    with pytest.raises(Exception):
        await queue_seal(engine, PROJECT, run_id)
    assert await process_seal(engine, PROJECT, job_id)
    assert not await process_seal(engine, PROJECT, job_id)
    assert (await JobQueue(engine, timedelta(seconds=30)).get(PROJECT, job_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_incomplete_seal_job_fails_without_recording(engine):
    run_id, _ = await create_run(engine, PROJECT)
    job_id = await queue_seal(engine, PROJECT, run_id)
    assert not await process_seal(engine, PROJECT, job_id)
    job = await JobQueue(engine, timedelta(seconds=30)).get(PROJECT, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "sealing validation failed"
    async with project_connection(engine, PROJECT) as connection:
        assert (await connection.execute(text("""
            SELECT count(*) FROM recordings WHERE run_id = :run
        """), {"run": run_id})).scalar_one() == 0


@pytest.mark.asyncio
async def test_cancelled_seal_does_not_create_recording(engine):
    run_id, _ = await create_run(engine, PROJECT)
    job_id = await queue_seal(engine, PROJECT, run_id)
    queue = JobQueue(engine, timedelta(seconds=30))
    assert (await queue.cancel(PROJECT, job_id))["status"] == "cancelled"
    assert not await process_seal(engine, PROJECT, job_id)
    async with project_connection(engine, PROJECT) as connection:
        assert (await connection.execute(text("""
            SELECT count(*) FROM recordings WHERE run_id = :run
        """), {"run": run_id})).scalar_one() == 0


@pytest.mark.asyncio
async def test_http_capture_and_tenant_authorization(engine):
    token = issue_key()
    principal, key_id = new_uuid7(), new_uuid7()
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO principals(id, organization_id, subject, kind)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :subject, 'service')
        """), {"id": principal, "subject": str(principal)})
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            INSERT INTO api_keys(id, organization_id, project_id, principal_id,
              name, key_prefix, key_hash, scopes)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :project,
              :principal, :name, :prefix, :hash, ARRAY['capture:write'])
        """), {"id": key_id, "project": PROJECT, "principal": principal,
               "name": str(key_id), "prefix": token[:16], "hash": key_hash(token)})
    app.state.engine = engine
    app.state.cursor_secret = b"0123456789abcdef0123456789abcdef"
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/projects/{PROJECT}/runs", json={"agent_name": "test", "adapter": "adk", "adapter_version": "1"}, headers=headers)
        assert response.status_code == 201, response.text
        run_id, stream_id = response.json()["id"], response.json()["stream_id"]
        async with project_connection(engine, PROJECT) as connection:
            metadata = (await connection.execute(text("""
                SELECT agent_name, adapter, adapter_version FROM runs WHERE id = :run
            """), {"run": UUID(run_id)})).one()
        assert tuple(metadata) == ("test", "adk", "1")
        event = Event(schema_version="1", event_id=new_uuid7(), run_id=UUID(run_id),
                      stream_id=UUID(stream_id), sequence=0, type="run.completed",
                      occurred_at=datetime.now(timezone.utc), payload={"ok": True}, replay_safe=True)
        response = await client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/events:batch",
                                     json={"events": [event.model_dump(mode="json")]}, headers=headers)
        assert response.status_code == 200, response.text
        artifact_url = f"/v1/projects/{PROJECT}/runs/{run_id}/artifacts"
        artifact_bytes = f"hello {run_id}".encode()
        artifact = {"name": "capture.txt", "content_type": "text/plain",
                    "content_base64": base64.b64encode(artifact_bytes).decode()}
        uploaded = await client.post(artifact_url, json=artifact, headers=headers)
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["size_bytes"] == len(artifact_bytes)
        duplicate = await client.post(artifact_url, json=artifact, headers=headers)
        assert duplicate.status_code == 201
        assert duplicate.json()["id"] == uploaded.json()["id"]
        response = await client.post(f"/v1/projects/{PROJECT}/runs/{run_id}:seal", headers=headers)
        assert response.status_code == 202, response.text
        assert await process_seal(engine, PROJECT, UUID(response.json()["job_id"]))
        async with project_connection(engine, PROJECT) as connection:
            manifest = (await connection.execute(text("""
                SELECT manifest FROM recordings WHERE run_id = :run
            """), {"run": UUID(run_id)})).scalar_one()
        assert manifest["replay_eligibility"] == "eligible"
        assert manifest["artifacts"][0]["name"] == "capture.txt"
        assert (await client.post(artifact_url, json=artifact, headers=headers)).status_code == 409
        other_project = UUID("0199b167-0000-7000-8000-000000000012")
        response = await client.post(f"/v1/projects/{other_project}/runs", json={"agent_name": "test", "adapter": "adk", "adapter_version": "1"}, headers=headers)
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_multi_stream_pagination_and_seal_guard(engine):
    run_id, first_stream = await create_run(engine, PROJECT)
    second_stream = new_uuid7()
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            INSERT INTO run_streams(id, run_id, project_id, name)
            VALUES (:stream, :run, :project, 'secondary')
        """), {"stream": second_stream, "run": run_id, "project": PROJECT})
    for stream_id in (first_stream, second_stream):
        event = Event(schema_version="1", event_id=new_uuid7(), run_id=run_id,
                      stream_id=stream_id, sequence=0, type="run.completed",
                      occurred_at=datetime.now(timezone.utc), payload={"ok": True}, replay_safe=True)
        await ingest(engine, PROJECT, run_id, stream_id, [event])
    token = issue_key()
    principal, key_id = new_uuid7(), new_uuid7()
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO principals(id, organization_id, subject, kind)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :subject, 'service')
        """), {"id": principal, "subject": str(principal)})
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            INSERT INTO api_keys(id, organization_id, project_id, principal_id,
              name, key_prefix, key_hash, scopes)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :project,
              :principal, :name, :prefix, :hash, ARRAY['runs:read'])
        """), {"id": key_id, "project": PROJECT, "principal": principal,
               "name": str(key_id), "prefix": token[:16], "hash": key_hash(token)})
    app.state.engine = engine
    app.state.cursor_secret = b"0123456789abcdef0123456789abcdef"
    url = f"/v1/projects/{PROJECT}/runs/{run_id}/events"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        first = await client.get(url, params={"limit": 1}, headers=headers)
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor
        second = await client.get(url, params={"limit": 1, "cursor": cursor}, headers=headers)
        assert second.status_code == 200, second.text
        assert second.json()["next_cursor"] is None
        assert first.json()["items"][0]["event_id"] != second.json()["items"][0]["event_id"]
    await seal_run(engine, PROJECT, run_id)
    with pytest.raises(Conflict):
        await ingest(engine, PROJECT, run_id, first_stream, [event])


@pytest.mark.asyncio
async def test_admin_settings_key_lifecycle_and_recording_read(engine):
    token = issue_key()
    principal, key_id = new_uuid7(), new_uuid7()
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO principals(id, organization_id, subject, kind)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :subject, 'service')
        """), {"id": principal, "subject": str(principal)})
    async with project_connection(engine, PROJECT) as connection:
        await connection.execute(text("""
            INSERT INTO api_keys(id, organization_id, project_id, principal_id,
              name, key_prefix, key_hash, scopes)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :project,
              :principal, :name, :prefix, :hash, ARRAY['admin', 'runs:read'])
        """), {"id": key_id, "project": PROJECT, "principal": principal,
               "name": str(key_id), "prefix": token[:16], "hash": key_hash(token)})
    app.state.engine = engine
    app.state.cursor_secret = b"0123456789abcdef0123456789abcdef"
    headers = {"Authorization": f"Bearer {token}"}
    run_id, stream_id = await create_run(engine, PROJECT)
    await ingest(engine, PROJECT, run_id, stream_id, [Event(
        schema_version="1", event_id=new_uuid7(), run_id=run_id, stream_id=stream_id,
        sequence=0, type="run.completed", occurred_at=datetime.now(timezone.utc),
        payload={}, replay_safe=True)])
    sealed = await seal_run(engine, PROJECT, run_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        runs = await client.get("/v1/runs", params={"project_id": str(PROJECT), "limit": 1}, headers=headers)
        assert runs.status_code == 200, runs.text
        assert all(item["project_id"] == str(PROJECT) for item in runs.json()["items"])
        assert (await client.get("/v1/runs", params={"project_id": str(
            UUID("0199b167-0000-7000-8000-000000000012"))}, headers=headers)).status_code == 401
        assert (await client.get("/v1/runs", headers=headers)).status_code == 400
        recording = await client.get(f"/v1/projects/{PROJECT}/recordings/{sealed['recording_id']}", headers=headers)
        assert recording.status_code == 200, recording.text
        assert recording.json()["event_count"] == 1
        from anvil_server.bundles import import_bundle
        archive_response = await client.get(
            f"/v1/projects/{PROJECT}/recordings/{sealed['recording_id']}/archive", headers=headers)
        assert archive_response.status_code == 200, archive_response.text
        archive_manifest, archive_artifacts = import_bundle(archive_response.content)
        assert archive_manifest["recording"]["run_id"] == str(run_id)
        assert len(archive_manifest["recording"]["events"]) == 1
        assert archive_artifacts == {}
        projection_url = f"/v1/projects/{PROJECT}/recordings/{sealed['recording_id']}/projection"
        projection = await client.get(projection_url, headers=headers)
        assert projection.status_code == 200, projection.text
        assert len(projection.json()["state"]["events"]) == 1
        assert (await client.get(projection_url, headers=headers)).json()["canonical_hash"] == projection.json()["canonical_hash"]
        before = await client.get(projection_url, params={"before_event_id": str(
            projection.json()["state"]["events"][0]["event_id"])}, headers=headers)
        assert before.json()["state"]["events"] == []
        settings = await client.patch(f"/v1/projects/{PROJECT}", json={"retention_days": 31}, headers=headers)
        assert settings.status_code == 200, settings.text
        assert settings.json()["retention_days"] == 31
        await client.patch(f"/v1/projects/{PROJECT}", json={"retention_days": 30}, headers=headers)
        created = await client.post(f"/v1/projects/{PROJECT}/keys", json={
            "name": str(new_uuid7()), "scopes": ["capture:write"]}, headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["secret"].startswith("anv_")
        revoked = await client.delete(f"/v1/projects/{PROJECT}/keys/{created.json()['id']}", headers=headers)
        assert revoked.status_code == 204
        assert (await client.delete(f"/v1/projects/{PROJECT}/keys/{created.json()['id']}", headers=headers)).status_code == 204
        deletion_url = f"/v1/projects/{PROJECT}/deletions"
        deletion_payload = {"target_kind": "recording", "target_id": sealed["recording_id"]}
        deletion = await client.post(deletion_url, json=deletion_payload, headers=headers)
        assert deletion.status_code == 202, deletion.text
        duplicate = await client.post(deletion_url, json=deletion_payload, headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == deletion.json()["job_id"]
        status = await client.get(f"{deletion_url}/{deletion.json()['deletion_id']}", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "queued"
        assert (await client.post(deletion_url, json={"target_kind": "suite", "target_id": str(new_uuid7())},
                                  headers=headers)).status_code == 400


@pytest.mark.asyncio
async def test_verified_archive_import_is_scoped_and_immutable(engine):
    import json
    from anvil_server.bundles import export_bundle, validate_recording_archive
    source_run, source_stream = await create_run(engine, PROJECT)
    event = Event(schema_version="1", event_id=new_uuid7(), run_id=source_run,
                  stream_id=source_stream, sequence=0, type="run.completed",
                  occurred_at=datetime.now(timezone.utc), payload={"message": "safe"}, replay_safe=True)
    await ingest(engine, PROJECT, source_run, source_stream, [event])
    sealed = await seal_run(engine, PROJECT, source_run)
    recording = {"schema_version": "1", "run_id": str(source_run), "manifest": sealed["manifest"],
                 "manifest_sha256": __import__("hashlib").sha256(json.dumps(
                     sealed["manifest"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                 "events": [event.model_dump(mode="json")]}
    recording["events_sha256"] = __import__("hashlib").sha256(json.dumps(
        recording["events"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    archive = export_bundle(sealed["recording_id"], {}, recording)
    assert len(validate_recording_archive(archive)[1]) == 1
    with pytest.raises(ValueError, match="integrity"):
        falsified = {**recording, "events": [{**recording["events"][0], "payload": {"changed": True}}]}
        validate_recording_archive(export_bundle(sealed["recording_id"], {}, falsified))
    import_project = UUID("0199b167-0000-7000-8000-000000000012")
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO projects(id, organization_id, slug, name)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', 'import-target', 'Import target')
            ON CONFLICT (id) DO NOTHING
        """), {"id": import_project})
    token = issue_key()
    principal, key_id = new_uuid7(), new_uuid7()
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO principals(id, organization_id, subject, kind)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :subject, 'service')
        """), {"id": principal, "subject": str(principal)})
    async with project_connection(engine, import_project) as connection:
        await connection.execute(text("""
            INSERT INTO api_keys(id, organization_id, project_id, principal_id,
              name, key_prefix, key_hash, scopes)
            VALUES (:id, '0199b167-0000-7000-8000-000000000001', :project,
              :principal, :name, :prefix, :hash, ARRAY['replay:write'])
        """), {"id": key_id, "project": import_project, "principal": principal,
               "name": str(key_id), "prefix": token[:16], "hash": key_hash(token)})
    app.state.engine = engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/projects/{import_project}/imports:verified",
                                     json={"archive_base64": base64.b64encode(archive).decode()},
                                     headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403, response.text
    async with project_connection(engine, import_project) as connection:
        count = (await connection.execute(text("""
            SELECT count(*) FROM recordings WHERE project_id = :project AND run_id = :run
        """), {"project": import_project, "run": source_run})).scalar_one()
        assert count == 0
    from anvil_server.sealing import seal
    imported_run, imported_stream, imported_event = new_uuid7(), new_uuid7(), new_uuid7()
    imported = Event(schema_version="1", event_id=imported_event, run_id=imported_run,
                     stream_id=imported_stream, sequence=0, type="run.completed",
                     occurred_at=datetime.now(timezone.utc), payload={"safe": True}, replay_safe=True)
    summary = [{"event_id": str(imported_event), "stream_id": str(imported_stream),
                "sequence": 0, "type": "run.completed", "parent_event_ids": [],
                "invocation_id": None, "replay_safe": True}]
    imported_manifest = seal(str(imported_run), str(import_project), summary,
                             [{"stream_id": str(imported_stream), "terminal": True}], [])
    imported_manifest["artifacts"] = []
    prepared = {"schema_version": "1", "run_id": str(imported_run),
                "manifest": imported_manifest, "events": [imported.model_dump(mode="json")]}
    prepared["manifest_sha256"] = __import__("hashlib").sha256(json.dumps(
        imported_manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prepared["events_sha256"] = __import__("hashlib").sha256(json.dumps(
        prepared["events"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    archive = export_bundle(str(new_uuid7()), {}, prepared)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/projects/{import_project}/imports:verified",
                                     json={"archive_base64": base64.b64encode(archive).decode()},
                                     headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 201, response.text
        assert response.json()["run_id"] == str(imported_run)
        retry = await client.post(f"/v1/projects/{import_project}/imports:verified",
                                  json={"archive_base64": base64.b64encode(archive).decode()},
                                  headers={"Authorization": f"Bearer {token}"})
        assert retry.status_code == 409
    async with project_connection(engine, import_project) as connection:
        assert (await connection.execute(text("SELECT count(*) FROM events WHERE run_id = :run"),
                                         {"run": imported_run})).scalar_one() == 1


@pytest.mark.asyncio
async def test_run_creation_idempotency(engine):
    from anvil_server.capture import CaptureConflict
    key = str(new_uuid7())
    first = await create_run(engine, PROJECT, idempotency_key=key, request_hash="a" * 64)
    assert await create_run(engine, PROJECT, idempotency_key=key, request_hash="a" * 64) == first
    with pytest.raises(CaptureConflict):
        await create_run(engine, PROJECT, idempotency_key=key, request_hash="b" * 64)
