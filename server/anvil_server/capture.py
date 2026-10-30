"""Durable capture and immutable recording workflows."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection
from .events import Event, accept_batch
from .identifiers import new_uuid7
from .sealing import IncompleteCapture, seal


class CaptureConflict(ValueError):
    pass


async def queue_seal(engine: AsyncEngine, project_id: UUID, run_id: UUID,
                     acknowledge_incomplete: bool = False) -> UUID:
    job_id = new_uuid7()
    async with project_connection(engine, project_id) as connection:
        state = (await connection.execute(text("""
            SELECT capture_status FROM runs WHERE project_id = :project AND id = :run FOR UPDATE
        """), {"project": project_id, "run": run_id})).scalar_one_or_none()
        if state != "open":
            raise CaptureConflict("Run is already sealing or unavailable")
        await connection.execute(text("""
            INSERT INTO jobs(id, project_id, kind) VALUES (:job, :project, 'seal')
        """), {"job": job_id, "project": project_id})
        await connection.execute(text("""
            INSERT INTO seal_requests(job_id, project_id, run_id, acknowledge_incomplete)
            VALUES (:job, :project, :run, :acknowledge)
        """), {"job": job_id, "project": project_id, "run": run_id,
               "acknowledge": acknowledge_incomplete})
        await connection.execute(text("""
            UPDATE runs SET capture_status = 'sealing' WHERE id = :run AND project_id = :project
        """), {"run": run_id, "project": project_id})
    return job_id


async def create_run(engine: AsyncEngine, project_id: UUID, name: str = "main",
                     idempotency_key: str | None = None, request_hash: str | None = None,
                     agent_name: str | None = None, adapter: str | None = None,
                     adapter_version: str | None = None, trace_id: str | None = None,
                     started_at=None) -> tuple[UUID, UUID]:
    run_id, stream_id = new_uuid7(), new_uuid7()
    async with project_connection(engine, project_id) as connection:
        if idempotency_key is not None:
            await connection.execute(text("""
                SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))
            """), {"lock": str(project_id) + ":" + idempotency_key})
            existing = (await connection.execute(text("""
                SELECT request_hash, response FROM idempotency_keys
                WHERE project_id = :project AND key = :key
            """), {"project": project_id, "key": idempotency_key})).mappings().first()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise CaptureConflict("Idempotency key reused with different request")
                return UUID(existing["response"]["id"]), UUID(existing["response"]["stream_id"])
        await connection.execute(text("""
            INSERT INTO runs(id, project_id, execution_outcome, agent_name,
                             adapter, adapter_version, trace_id, started_at)
            VALUES (:run, :project, 'running', :name, :adapter, :version, :trace, :started)
        """), {"run": run_id, "project": project_id, "name": agent_name,
               "adapter": adapter, "version": adapter_version, "trace": trace_id,
               "started": started_at})
        await connection.execute(text("""
            INSERT INTO run_streams(id, run_id, project_id, name)
            VALUES (:stream, :run, :project, :name)
        """), {"stream": stream_id, "run": run_id, "project": project_id, "name": name})
        if idempotency_key is not None:
            await connection.execute(text("""
                INSERT INTO idempotency_keys(project_id, key, request_hash, status_code, response)
                VALUES (:project, :key, :hash, 201, CAST(:response AS jsonb))
            """), {"project": project_id, "key": idempotency_key, "hash": request_hash,
                   "response": json.dumps({"id": str(run_id), "stream_id": str(stream_id)})})
    return run_id, stream_id


async def ingest(engine: AsyncEngine, project_id: UUID, run_id: UUID,
                 stream_id: UUID, events: list[Event]):
    return await accept_batch(engine, project_id, run_id, stream_id, events)


async def seal_run(engine: AsyncEngine, project_id: UUID, run_id: UUID,
                   acknowledge_incomplete: bool = False, from_worker: bool = False,
                   job_id: UUID | None = None, lease_token: UUID | None = None) -> dict:
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT capture_status FROM runs
            WHERE id = :run AND project_id = :project FOR UPDATE
        """), {"run": run_id, "project": project_id})).scalar_one_or_none()
        if row != ("sealing" if from_worker else "open"):
            raise CaptureConflict("Run is sealed or unavailable")
        streams = (await connection.execute(text("""
            SELECT id FROM run_streams WHERE run_id = :run AND project_id = :project
        """), {"run": run_id, "project": project_id})).mappings().all()
        rows = (await connection.execute(text("""
            SELECT event_id, stream_id, sequence, event_type, parent_event_ids,
                   invocation_id, replay_safe
            FROM events WHERE run_id = :run AND project_id = :project
            ORDER BY stream_id, sequence
        """), {"run": run_id, "project": project_id})).mappings().all()
        events = [{"event_id": str(item["event_id"]), "stream_id": str(item["stream_id"]),
                   "sequence": item["sequence"], "type": item["event_type"],
                   "parent_event_ids": item["parent_event_ids"],
                   "invocation_id": str(item["invocation_id"]) if item["invocation_id"] else None,
                   "replay_safe": item["replay_safe"]} for item in rows]
        terminal_types = {"run.completed", "run.failed"}
        stream_details = [{"stream_id": str(item["id"]),
                           "terminal": any(event["stream_id"] == str(item["id"]) and
                                           event["type"] in terminal_types for event in events)} for item in streams]
        artifacts = (await connection.execute(text("""
            SELECT id, name, media_type, sha256_hash, size_bytes FROM artifacts
            WHERE project_id = :project AND run_id = :run ORDER BY id
        """), {"project": project_id, "run": run_id})).mappings().all()
        manifest = seal(str(run_id), str(project_id), events, stream_details, [], acknowledge_incomplete)
        manifest["artifacts"] = [{"id": str(item["id"]), "name": item["name"],
                                  "content_type": item["media_type"], "sha256": item["sha256_hash"],
                                  "size_bytes": item["size_bytes"]} for item in artifacts]
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        recording_id = new_uuid7()
        await connection.execute(text("""
            INSERT INTO recordings (id, project_id, run_id, schema_version, manifest, sha256_hash)
            VALUES (:id, :project, :run, '1', CAST(:manifest AS jsonb), :hash)
        """), {"id": recording_id, "project": project_id, "run": run_id,
               "manifest": manifest_bytes.decode(), "hash": hashlib.sha256(manifest_bytes).hexdigest()})
        await connection.execute(text("""
            UPDATE runs SET capture_status = :capture, execution_outcome = :outcome,
              finished_at = clock_timestamp()
            WHERE id = :run AND project_id = :project
        """), {"capture": manifest["capture_completeness"], "outcome": manifest["execution_outcome"],
               "run": run_id, "project": project_id})
        if from_worker:
            if job_id is None or lease_token is None:
                raise CaptureConflict("Worker lease is required")
            result = await connection.execute(text("""
                UPDATE jobs SET status = 'succeeded', outcome = 'succeeded',
                    finished_at = clock_timestamp(), lease_token = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL, result_url = :url
                WHERE id = :job AND project_id = :project AND lease_token = :token
                  AND status = 'running' AND lease_expires_at > clock_timestamp()
            """), {"job": job_id, "project": project_id, "token": lease_token,
                   "url": f"/v1/projects/{project_id}/recordings/{recording_id}"})
            if result.rowcount != 1:
                raise CaptureConflict("Worker lease expired")
    return {"recording_id": str(recording_id), "manifest": manifest}
