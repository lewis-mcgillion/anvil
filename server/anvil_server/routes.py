"""Project-authorized recording and administration routes."""

from __future__ import annotations

import json
import asyncio
import base64
import binascii
import hashlib
import re
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .capture import CaptureConflict, create_run, ingest, seal_run, queue_seal
from .auth import issue_key, key_hash, validate_scopes
from .bundles import MAX_ARTIFACT, MAX_BUNDLE, export_bundle, validate_recording_archive
from .identifiers import new_uuid7
from .database import project_connection
from .events import Conflict, Event, MAX_RUN_BYTES
from .jobs import JobQueue, LeaseLost
from .pagination import CursorCodec
from .security import AccessDenied, Unauthenticated, authorize

router = APIRouter(prefix="/v1")


@router.get("/projects")
async def list_projects(request: Request, project_id: UUID | None = None,
                        limit: int = 100, cursor: str | None = None):
    if project_id is None or limit < 1 or limit > 1000:
        return failure(request, 400, "invalid_request")
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    if cursor is not None:
        return failure(request, 400, "invalid_cursor")
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT id, name, retention_days, created_at FROM projects WHERE id = :project
        """), {"project": project_id})).mappings().first()
    if row is None:
        return failure(request, 404, "not_found")
    return {"items": [{"id": str(row["id"]), "name": row["name"],
                       "retention_days": row["retention_days"],
                       "created_at": row["created_at"].isoformat()}], "next_cursor": None}


@router.get("/runs")
async def list_runs(request: Request, project_id: UUID | None = None, limit: int = 100,
                    cursor: str | None = None, execution_state: str | None = None,
                    capture_state: str | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await list_project_runs(request, project_id, limit, cursor,
                                   execution_state, capture_state)


@router.get("/metrics")
async def project_metrics(request: Request, project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    engine, error = await scoped(request, project_id, "metrics:read")
    if error is not None:
        return error
    async with project_connection(engine, project_id) as connection:
        counts = {}
        for resource, table in (("runs", "runs"), ("recordings", "recordings"),
                                ("events", "events"), ("jobs", "jobs")):
            counts[resource] = (await connection.execute(text(
                f"SELECT count(*) FROM {table} WHERE project_id = :project"),
                {"project": project_id})).scalar_one()
    exposition = "# TYPE anvil_resources gauge\n"
    for resource, count in counts.items():
        exposition += f'anvil_resources{{resource="{resource}"}} {count}\n'
    return Response(content=exposition, media_type="text/plain; version=0.0.4")


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_name: str = Field(min_length=1, max_length=256)
    adapter: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    started_at: datetime | None = None


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[Event] = Field(min_length=1, max_length=500)


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)
    content_base64: str


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    retention_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class ArchiveImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive_base64: str


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_artifacts: bool = True
    redaction_profile: str | None = Field(default=None, min_length=1, max_length=128)


class DeletionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_kind: str = Field(pattern=r"^(run|recording|suite)$")
    target_id: UUID


class AgentVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(pattern=r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
    entrypoint: str = Field(min_length=1, max_length=512)
    adapter: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)


@router.post("/projects/{project_id}/agent-versions", status_code=201)
async def register_agent_version(request: Request, project_id: UUID, payload: AgentVersionCreate):
    engine, error = await scoped(request, project_id, "replay:write")
    if error is not None:
        return error
    key = request.headers.get("Idempotency-Key")
    if key is None or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key) is None:
        return failure(request, 400, "invalid_request")
    digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))
        """), {"lock": str(project_id) + ":" + key})
        previous = (await connection.execute(text("""
            SELECT request_hash, response FROM idempotency_keys
            WHERE project_id = :project AND key = :key
        """), {"project": project_id, "key": key})).mappings().first()
        if previous is not None:
            if previous["request_hash"] != digest:
                return failure(request, 409, "conflict")
            return previous["response"]
        row = (await connection.execute(text("""
            INSERT INTO agent_versions (id, project_id, image, entrypoint, adapter, adapter_version)
            VALUES (:id, :project, :image, :entrypoint, :adapter, :version)
            ON CONFLICT (project_id, image, entrypoint, adapter, adapter_version)
            DO UPDATE SET image = EXCLUDED.image
            RETURNING id, project_id, image, entrypoint, adapter, adapter_version, created_at
        """), {"id": new_uuid7(), "project": project_id, "image": payload.image,
               "entrypoint": payload.entrypoint, "adapter": payload.adapter,
               "version": payload.adapter_version})).mappings().one()
        result = {**{field: str(value) if isinstance(value, UUID) else value for field, value in row.items()
                     if field != "created_at"}, "created_at": row["created_at"].isoformat()}
        await connection.execute(text("""
            INSERT INTO idempotency_keys(project_id, key, request_hash, status_code, response)
            VALUES (:project, :key, :hash, 201, CAST(:response AS jsonb))
        """), {"project": project_id, "key": key, "hash": digest, "response": json.dumps(result)})
    return result


@router.get("/projects/{project_id}/runs")
async def list_project_runs(request: Request, project_id: UUID, limit: int = 100,
                            cursor: str | None = None, execution_state: str | None = None,
                            capture_state: str | None = None):
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    if limit < 1 or limit > 500 or (execution_state is not None and execution_state not in {
        "running", "succeeded", "failed", "cancelled", "unknown"}) or (
            capture_state is not None and capture_state not in {
                "open", "sealing", "sealed_complete", "sealed_incomplete"}):
        return failure(request, 400, "invalid_request")
    secret = getattr(request.app.state, "cursor_secret", None)
    if not secret:
        return failure(request, 503, "not_ready")
    codec = CursorCodec(secret)
    filters = {"execution_state": execution_state or "", "capture_state": capture_state or ""}
    try:
        after = UUID(codec.decode(cursor, "runs", str(project_id), filters)) if cursor else None
    except ValueError:
        return failure(request, 400, "invalid_request")
    async with project_connection(engine, project_id) as connection:
        rows = (await connection.execute(text("""
            SELECT id, project_id, capture_status, execution_outcome, agent_name,
                   adapter, adapter_version, trace_id, started_at, finished_at, created_at
            FROM runs WHERE project_id = :project AND (CAST(:after AS uuid) IS NULL OR id < CAST(:after AS uuid))
              AND (CAST(:execution AS text) IS NULL OR execution_outcome = :execution)
              AND (CAST(:capture AS text) IS NULL OR capture_status = :capture)
            ORDER BY id DESC LIMIT :limit
        """), {"project": project_id, "after": str(after) if after else None,
               "execution": execution_state, "capture": capture_state, "limit": limit + 1})).mappings().all()
    items = [{"id": str(row["id"]), "project_id": str(row["project_id"]),
              "capture_state": row["capture_status"], "execution_state": row["execution_outcome"],
              "agent_name": row["agent_name"], "adapter": row["adapter"],
              "adapter_version": row["adapter_version"], "trace_id": row["trace_id"],
              "started_at": (row["started_at"] or row["created_at"]).isoformat(),
              "ended_at": row["finished_at"].isoformat() if row["finished_at"] else None}
             for row in rows[:limit]]
    next_cursor = codec.encode("runs", str(project_id), str(rows[limit - 1]["id"]), filters) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


def failure(request: Request, status: int, code: str):
    from .app import problem
    return problem(request, status, code, code.replace("_", " "))


async def scoped(request: Request, project_id: UUID, permission: str):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return None, failure(request, 503, "not_ready")
    try:
        await authorize(engine, request, project_id, permission)
    except Unauthenticated:
        response = failure(request, 401, "unauthenticated")
        response.headers["WWW-Authenticate"] = "Bearer"
        return None, response
    except AccessDenied:
        return None, failure(request, 403, "forbidden")
    return engine, None


@router.patch("/projects/{project_id}")
async def update_project(request: Request, project_id: UUID, payload: ProjectUpdate):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    if not payload.model_fields_set or any(getattr(payload, field) is None for field in payload.model_fields_set):
        return failure(request, 400, "invalid_request")
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            UPDATE projects SET name = COALESCE(:name, name), retention_days = COALESCE(:retention, retention_days)
            WHERE id = :project RETURNING id, name, retention_days, created_at
        """), {"name": payload.name, "retention": payload.retention_days,
               "project": project_id})).mappings().first()
    if row is None:
        return failure(request, 404, "not_found")
    return {"id": str(row["id"]), "name": row["name"], "retention_days": row["retention_days"],
            "created_at": row["created_at"].isoformat()}


@router.post("/projects/{project_id}/keys", status_code=201)
async def create_api_key(request: Request, project_id: UUID, payload: ApiKeyCreate):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    try:
        validate_scopes(payload.scopes)
    except ValueError:
        return failure(request, 400, "invalid_request")
    if payload.expires_at is not None and (payload.expires_at.tzinfo is None or
                                           payload.expires_at <= datetime.now(payload.expires_at.tzinfo)):
        return failure(request, 400, "invalid_request")
    secret = issue_key()
    key_id = new_uuid7()
    async with project_connection(engine, project_id) as connection:
        principal = (await connection.execute(text("""
            SELECT principal_id, organization_id FROM api_keys
            WHERE project_id = :project AND key_hash = :hash AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > clock_timestamp())
        """), {"project": project_id,
               "hash": key_hash(request.headers["authorization"].split()[1])})).mappings().first()
        if principal is None:
            return failure(request, 403, "forbidden")
        existing = (await connection.execute(text("""
            SELECT 1 FROM api_keys WHERE project_id = :project AND name = :name
        """), {"project": project_id, "name": payload.name})).first()
        if existing:
            return failure(request, 409, "conflict")
        row = (await connection.execute(text("""
            INSERT INTO api_keys (id, organization_id, project_id, principal_id, name,
                                  key_prefix, key_hash, scopes, expires_at)
            VALUES (:id, :organization, :project, :principal, :name,
                    :prefix, :hash, :scopes, :expires)
            RETURNING created_at
        """), {"id": key_id, "organization": principal["organization_id"],
               "project": project_id, "principal": principal["principal_id"],
               "name": payload.name, "prefix": secret[:16], "hash": key_hash(secret),
               "scopes": payload.scopes, "expires": payload.expires_at})).scalar_one()
    return {"id": str(key_id), "name": payload.name, "scopes": payload.scopes,
            "secret": secret, "created_at": row.isoformat()}


@router.delete("/projects/{project_id}/keys/{key_id}", status_code=204)
async def revoke_api_key(request: Request, project_id: UUID, key_id: UUID):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    async with project_connection(engine, project_id) as connection:
        result = await connection.execute(text("""
            UPDATE api_keys SET revoked_at = COALESCE(revoked_at, clock_timestamp())
            WHERE project_id = :project AND id = :key
        """), {"project": project_id, "key": key_id})
    if result.rowcount == 0:
        return failure(request, 404, "not_found")
    return Response(status_code=204)


@router.post("/projects/{project_id}/runs", status_code=201)
async def open_run(request: Request, project_id: UUID, payload: RunCreate):
    engine, error = await scoped(request, project_id, "capture:write")
    if error is not None:
        return error
    key = request.headers.get("Idempotency-Key")
    if key is None or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key) is None:
        return failure(request, 400, "invalid_request")
    digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    try:
        run_id, stream_id = await create_run(engine, project_id, idempotency_key=key, request_hash=digest,
                                             **payload.model_dump())
    except CaptureConflict:
        return failure(request, 409, "conflict")
    return {"id": str(run_id), "project_id": str(project_id), "stream_id": str(stream_id),
            "capture_state": "open", "execution_state": "running", **payload.model_dump()}


@router.post("/projects/{project_id}/runs/{run_id}/events:batch")
async def ingest_events(request: Request, project_id: UUID, run_id: UUID, payload: EventBatch):
    engine, error = await scoped(request, project_id, "capture:write")
    if error is not None:
        return error
    streams = {event.stream_id for event in payload.events}
    if len(streams) != 1:
        return failure(request, 400, "invalid_request")
    try:
        return await ingest(engine, project_id, run_id, next(iter(streams)), payload.events)
    except (Conflict, CaptureConflict):
        return failure(request, 409, "conflict")
    except ValueError:
        return failure(request, 400, "invalid_request")


@router.post("/runs/{run_id}/events:batch")
async def ingest_run_events(request: Request, run_id: UUID, payload: EventBatch,
                            project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await ingest_events(request, project_id, run_id, payload)


@router.post("/projects/{project_id}/runs/{run_id}:seal", status_code=202)
async def finish_run(request: Request, project_id: UUID, run_id: UUID, acknowledge_incomplete: bool = False):
    engine, error = await scoped(request, project_id, "capture:write")
    if error is not None:
        return error
    try:
        job_id = await queue_seal(engine, project_id, run_id, acknowledge_incomplete)
    except CaptureConflict:
        return failure(request, 409, "conflict")
    job_url = f"/v1/projects/{project_id}/jobs/{job_id}"
    return Response(content=json.dumps({"job_id": str(job_id), "job_url": job_url, "status": "queued"}),
                    media_type="application/json", status_code=202, headers={"Location": job_url})


@router.post("/runs/{run_id}:seal", status_code=202)
async def finish_run_by_id(request: Request, run_id: UUID, project_id: UUID | None = None,
                           acknowledge_incomplete: bool = False):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await finish_run(request, project_id, run_id, acknowledge_incomplete)


@router.post("/projects/{project_id}/runs/{run_id}/artifacts", status_code=201)
async def upload_artifact(request: Request, project_id: UUID, run_id: UUID, payload: ArtifactCreate):
    engine, error = await scoped(request, project_id, "capture:write")
    if error is not None:
        return error
    if len(payload.content_base64) > (MAX_ARTIFACT + 2) // 3 * 4:
        return failure(request, 413, "payload_too_large")
    try:
        contents = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError):
        return failure(request, 400, "invalid_request")
    if len(contents) > MAX_ARTIFACT:
        return failure(request, 413, "payload_too_large")
    digest = hashlib.sha256(contents).hexdigest()
    async with project_connection(engine, project_id) as connection:
        status = (await connection.execute(text("""
            SELECT capture_status FROM runs WHERE id = :run AND project_id = :project FOR UPDATE
        """), {"run": run_id, "project": project_id})).scalar_one_or_none()
        if status is None:
            return failure(request, 404, "not_found")
        if status != "open":
            return failure(request, 409, "conflict")
        current_bytes = (await connection.execute(text("""
            SELECT COALESCE((SELECT sum(octet_length(payload::text)) FROM events
                     WHERE project_id = :project AND run_id = :run), 0)
                 + COALESCE((SELECT sum(size_bytes) FROM artifacts
                     WHERE project_id = :project AND run_id = :run), 0)
        """), {"project": project_id, "run": run_id})).scalar_one()
        if current_bytes + len(contents) > MAX_RUN_BYTES:
            return failure(request, 413, "payload_too_large")
        existing = (await connection.execute(text("""
            SELECT id, run_id, name, media_type, size_bytes, created_at
            FROM artifacts WHERE project_id = :project AND sha256_hash = :digest
        """), {"project": project_id, "digest": digest})).mappings().first()
        if existing:
            if existing["run_id"] != run_id or existing["name"] != payload.name or existing["media_type"] != payload.content_type:
                return failure(request, 409, "conflict")
            row = existing
        else:
            row = (await connection.execute(text("""
                INSERT INTO artifacts(id, project_id, run_id, name, media_type, sha256_hash, size_bytes, content)
                VALUES (:id, :project, :run, :name, :media_type, :digest, :size, :content)
                RETURNING id, run_id, name, media_type, size_bytes, created_at
            """), {"id": new_uuid7(), "project": project_id, "run": run_id, "name": payload.name,
                   "media_type": payload.content_type, "digest": digest, "size": len(contents),
                   "content": contents})).mappings().one()
    return {"id": str(row["id"]), "run_id": str(row["run_id"]), "name": row["name"],
            "content_type": row["media_type"], "size_bytes": row["size_bytes"],
            "sha256": "sha256:" + digest, "created_at": row["created_at"].isoformat()}


@router.post("/runs/{run_id}/artifacts", status_code=201)
async def upload_run_artifact(request: Request, run_id: UUID, payload: ArtifactCreate,
                              project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await upload_artifact(request, project_id, run_id, payload)


@router.get("/projects/{project_id}/recordings/{recording_id}")
async def get_recording(request: Request, project_id: UUID, recording_id: UUID):
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT recordings.id, recordings.run_id, recordings.project_id, recordings.sha256_hash,
                   recordings.manifest, recordings.created_at, runs.capture_status
            FROM recordings JOIN runs ON runs.id = recordings.run_id
            WHERE recordings.id = :recording AND recordings.project_id = :project
        """), {"recording": recording_id, "project": project_id})).mappings().first()
    if row is None:
        return failure(request, 404, "not_found")
    return {"id": str(row["id"]), "run_id": str(row["run_id"]), "project_id": str(row["project_id"]),
            "capture_state": row["capture_status"], "manifest_hash": "sha256:" + row["sha256_hash"],
            "event_count": row["manifest"]["event_count"], "sealed_at": row["created_at"].isoformat()}


@router.get("/recordings/{recording_id}")
async def get_recording_by_id(request: Request, recording_id: UUID, project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await get_recording(request, project_id, recording_id)


@router.get("/projects/{project_id}/recordings/{recording_id}/projection")
async def project_recording(request: Request, project_id: UUID, recording_id: UUID,
                            projection_version: str = "1", before_event_id: UUID | None = None):
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    if projection_version != "1":
        return failure(request, 400, "invalid_request")
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT run_id, manifest FROM recordings WHERE project_id = :project AND id = :recording
        """), {"project": project_id, "recording": recording_id})).mappings().first()
        if row is None:
            return failure(request, 404, "not_found")
        events = (await connection.execute(text("""
            SELECT event_id, stream_id, sequence, event_type, payload FROM events
            WHERE project_id = :project AND run_id = :run
            ORDER BY stream_id, sequence, event_id
        """), {"project": project_id, "run": row["run_id"]})).mappings().all()
    selected = []
    found = before_event_id is None
    for event in events:
        if event["event_id"] == before_event_id:
            found = True
            break
        selected.append({"event_id": str(event["event_id"]), "stream_id": str(event["stream_id"]),
                         "sequence": event["sequence"], "type": event["event_type"],
                         "payload": event["payload"]})
    if not found:
        return failure(request, 400, "invalid_request")
    state = {"events": selected, "capture_completeness": row["manifest"]["capture_completeness"]}
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"recording_id": str(recording_id), "projection_version": projection_version,
            "canonical_hash": "sha256:" + hashlib.sha256(canonical).hexdigest(), "state": state}


@router.get("/recordings/{recording_id}/projection")
async def project_recording_by_id(request: Request, recording_id: UUID,
                                  project_id: UUID | None = None, projection_version: str = "1",
                                  before_event_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await project_recording(request, project_id, recording_id,
                                   projection_version, before_event_id)


@router.post("/recordings/{recording_id}/exports", status_code=202)
async def request_recording_export(request: Request, recording_id: UUID,
                                   payload: ExportCreate | None = None,
                                   project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    options = payload or ExportCreate()
    if options.redaction_profile is not None:
        return failure(request, 400, "invalid_request")
    key = request.headers.get("Idempotency-Key")
    if key is None or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key) is None:
        return failure(request, 400, "invalid_request")
    digest = hashlib.sha256(json.dumps({"recording": str(recording_id),
                                       "include_artifacts": options.include_artifacts},
                                      sort_keys=True).encode()).hexdigest()
    job_id = new_uuid7()
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))
        """), {"lock": str(project_id) + ":" + key})
        previous = (await connection.execute(text("""
            SELECT request_hash, response FROM idempotency_keys
            WHERE project_id = :project AND key = :key
        """), {"project": project_id, "key": key})).mappings().first()
        if previous:
            if previous["request_hash"] != digest:
                return failure(request, 409, "conflict")
            job_id = UUID(previous["response"]["job_id"])
        else:
            found = (await connection.execute(text("""
                SELECT 1 FROM recordings WHERE project_id = :project AND id = :recording
            """), {"project": project_id, "recording": recording_id})).scalar_one_or_none()
            if found is None:
                return failure(request, 404, "not_found")
            await connection.execute(text("""
                INSERT INTO jobs(id, project_id, kind) VALUES (:job, :project, 'export')
            """), {"job": job_id, "project": project_id})
            await connection.execute(text("""
                INSERT INTO archive_exports(job_id, project_id, recording_id, include_artifacts)
                VALUES (:job, :project, :recording, :include)
            """), {"job": job_id, "project": project_id, "recording": recording_id,
                   "include": options.include_artifacts})
            await connection.execute(text("""
                INSERT INTO idempotency_keys(project_id, key, request_hash, status_code, response)
                VALUES (:project, :key, :hash, 202, CAST(:response AS jsonb))
            """), {"project": project_id, "key": key, "hash": digest,
                   "response": json.dumps({"job_id": str(job_id)})})
    job_url = f"/v1/projects/{project_id}/jobs/{job_id}"
    return Response(content=json.dumps({"job_id": str(job_id), "job_url": job_url, "status": "queued"}),
                    media_type="application/json", status_code=202, headers={"Location": job_url})


@router.get("/projects/{project_id}/recordings/{recording_id}/archive")
async def download_recording_archive(request: Request, project_id: UUID, recording_id: UUID):
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    async with project_connection(engine, project_id) as connection:
        recording = (await connection.execute(text("""
            SELECT run_id, manifest, sha256_hash FROM recordings
            WHERE id = :recording AND project_id = :project
        """), {"recording": recording_id, "project": project_id})).mappings().first()
        if recording is None:
            return failure(request, 404, "not_found")
        events = (await connection.execute(text("""
            SELECT event_id, run_id, stream_id, sequence, schema_version, event_type,
                   occurred_at, trace_id, span_id, parent_event_ids, invocation_id, payload, replay_safe
            FROM events WHERE project_id = :project AND run_id = :run
            ORDER BY stream_id, sequence
        """), {"project": project_id, "run": recording["run_id"]})).mappings().all()
        artifacts = (await connection.execute(text("""
            SELECT name, content FROM artifacts WHERE project_id = :project AND run_id = :run
            ORDER BY id
        """), {"project": project_id, "run": recording["run_id"]})).mappings().all()
    payload = {"schema_version": "1", "run_id": str(recording["run_id"]),
               "manifest": recording["manifest"], "manifest_sha256": recording["sha256_hash"],
               "events": [{"event_id": str(event["event_id"]), "run_id": str(event["run_id"]),
                           "stream_id": str(event["stream_id"]), "sequence": event["sequence"],
                           "schema_version": event["schema_version"], "type": event["event_type"],
                           "occurred_at": event["occurred_at"].isoformat(),
                           "trace_id": event["trace_id"], "span_id": event["span_id"],
                           "parent_event_ids": event["parent_event_ids"],
                           "invocation_id": str(event["invocation_id"]) if event["invocation_id"] else None,
                           "payload": event["payload"], "replay_safe": event["replay_safe"]}
                          for event in events]}
    payload["events_sha256"] = hashlib.sha256(json.dumps(payload["events"], sort_keys=True,
                                                  separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    try:
        archive = export_bundle(str(recording_id), {artifact["name"]: bytes(artifact["content"])
                                                    for artifact in artifacts}, payload)
    except ValueError:
        return failure(request, 413, "payload_too_large")
    return Response(content=archive, media_type="application/vnd.anvil.archive+zip",
                    headers={"Content-Disposition": f'attachment; filename="{recording_id}.anvil"'})


@router.post("/projects/{project_id}/imports:verified", status_code=201)
async def import_verified_recording(request: Request, project_id: UUID, payload: ArchiveImport):
    engine, error = await scoped(request, project_id, "replay:write")
    if error is not None:
        return error
    if len(payload.archive_base64) > (MAX_BUNDLE + 2) // 3 * 4:
        return failure(request, 413, "payload_too_large")
    try:
        archive = base64.b64decode(payload.archive_base64, validate=True)
        recording, events, artifacts = validate_recording_archive(archive)
    except (ValueError, binascii.Error, OSError, KeyError, TypeError):
        return failure(request, 400, "invalid_request")
    if recording["manifest"]["project_id"] != str(project_id):
        return failure(request, 403, "forbidden")
    run_id = UUID(recording["run_id"])
    recording_id = new_uuid7()
    if (recording["manifest"]["capture_completeness"] != "sealed_complete" or
            recording["manifest"]["replay_eligibility"] != "eligible"):
        return failure(request, 409, "conflict")
    async with project_connection(engine, project_id) as connection:
        existing = (await connection.execute(text("""
            SELECT id FROM runs WHERE id = :run AND project_id = :project
        """), {"run": run_id, "project": project_id})).scalar_one_or_none()
        if existing is not None:
            return failure(request, 409, "conflict")
        for name, contents in artifacts.items():
            existing_artifact = (await connection.execute(text("""
                SELECT 1 FROM artifacts WHERE project_id = :project AND sha256_hash = :hash
            """), {"project": project_id,
                   "hash": hashlib.sha256(contents).hexdigest()})).first()
            if existing_artifact is not None:
                return failure(request, 409, "conflict")
        await connection.execute(text("""
            INSERT INTO runs (id, project_id, capture_status, execution_outcome)
            VALUES (:run, :project, :capture, :outcome)
        """), {"run": run_id, "project": project_id,
               "capture": recording["manifest"]["capture_completeness"],
               "outcome": recording["manifest"]["execution_outcome"]})
        streams = sorted({event.stream_id for event in events})
        for stream_id in streams:
            sequence = max(event.sequence for event in events if event.stream_id == stream_id) + 1
            await connection.execute(text("""
                INSERT INTO run_streams(id, run_id, project_id, name, next_sequence)
                VALUES (:stream, :run, :project, :name, :sequence)
            """), {"stream": stream_id, "run": run_id, "project": project_id,
                   "name": str(stream_id), "sequence": sequence})
        from .events import event_hash
        for event in events:
            await connection.execute(text("""
                INSERT INTO events(event_id, project_id, run_id, stream_id, sequence,
                    schema_version, event_type, occurred_at, trace_id, span_id,
                    parent_event_ids, invocation_id, payload, content_hash, replay_safe)
                VALUES (:event, :project, :run, :stream, :sequence,
                    '1', :type, :occurred_at, :trace_id, :span_id,
                    CAST(:parents AS jsonb), :invocation, CAST(:payload AS jsonb), :hash, :safe)
            """), {"event": event.event_id, "project": project_id, "run": run_id,
                   "stream": event.stream_id, "sequence": event.sequence, "type": event.type,
                   "occurred_at": event.occurred_at, "trace_id": event.trace_id,
                   "span_id": event.span_id, "parents": json.dumps([str(p) for p in event.parent_event_ids]),
                   "invocation": event.invocation_id, "payload": json.dumps(event.payload),
                   "hash": event_hash(event), "safe": event.replay_safe})
        for name, contents in artifacts.items():
            await connection.execute(text("""
                INSERT INTO artifacts(id, project_id, run_id, name, media_type,
                                      sha256_hash, size_bytes, content)
                VALUES (:id, :project, :run, :name, 'application/octet-stream',
                        :hash, :size, :content)
            """), {"id": new_uuid7(), "project": project_id, "run": run_id,
                   "name": name, "hash": hashlib.sha256(contents).hexdigest(),
                   "size": len(contents), "content": contents})
        await connection.execute(text("""
            INSERT INTO recordings(id, project_id, run_id, schema_version, manifest, sha256_hash)
            VALUES (:id, :project, :run, '1', CAST(:manifest AS jsonb), :hash)
        """), {"id": recording_id, "project": project_id, "run": run_id,
               "manifest": json.dumps(recording["manifest"]), "hash": recording["manifest_sha256"]})
    return {"recording_id": str(recording_id), "run_id": str(run_id), "status": "imported"}


@router.get("/projects/{project_id}/runs/{run_id}/events")
async def list_events(request: Request, project_id: UUID, run_id: UUID, limit: int = 100,
                      cursor: str | None = None, stream_id: UUID | None = None,
                      type: str | None = None):
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    if limit < 1 or limit > 500:
        return failure(request, 400, "invalid_request")
    secret = getattr(request.app.state, "cursor_secret", None)
    if secret is None:
        return failure(request, 503, "not_ready")
    codec = CursorCodec(secret)
    filters = {"run": str(run_id), "stream": str(stream_id) if stream_id else "", "type": type or ""}
    try:
        after = json.loads(codec.decode(cursor, "events", str(project_id), filters)) if cursor else None
        if after is not None and (not isinstance(after, list) or len(after) != 3):
            raise ValueError("Invalid cursor position")
        position = [str(UUID(after[0])), int(after[1]), str(UUID(after[2]))] if after else None
    except (ValueError, TypeError):
        return failure(request, 400, "invalid_cursor")
    async with project_connection(engine, project_id) as connection:
        run_exists = (await connection.execute(text("""
            SELECT 1 FROM runs WHERE project_id = :project AND id = :run
        """), {"project": project_id, "run": run_id})).scalar_one_or_none()
        if run_exists is None:
            return failure(request, 404, "not_found")
        rows = (await connection.execute(text("""
            SELECT event_id, run_id, stream_id, sequence, schema_version, event_type,
                   occurred_at, trace_id, span_id, parent_event_ids, invocation_id, payload, replay_safe
            FROM events
            WHERE project_id = :project AND run_id = :run
              AND (CAST(:filter_stream AS uuid) IS NULL OR stream_id = CAST(:filter_stream AS uuid))
              AND (CAST(:filter_type AS text) IS NULL OR event_type = :filter_type)
              AND (:first OR (stream_id, sequence, event_id) >
                   (CAST(:stream AS uuid), :sequence, CAST(:event AS uuid)))
            ORDER BY stream_id, sequence, event_id LIMIT :limit
        """), {"project": project_id, "run": run_id, "first": position is None,
               "stream": position[0] if position else None,
               "sequence": position[1] if position else -1,
               "event": position[2] if position else None, "limit": limit + 1,
               "filter_stream": str(stream_id) if stream_id else None,
               "filter_type": type})).mappings().all()
    items = [{"event_id": str(row["event_id"]), "run_id": str(row["run_id"]),
              "stream_id": str(row["stream_id"]), "sequence": row["sequence"],
              "schema_version": row["schema_version"], "type": row["event_type"],
              "occurred_at": row["occurred_at"].isoformat(), "trace_id": row["trace_id"],
              "span_id": row["span_id"], "parent_event_ids": row["parent_event_ids"],
              "invocation_id": str(row["invocation_id"]) if row["invocation_id"] else None,
              "payload": row["payload"], "replay_safe": row["replay_safe"]} for row in rows[:limit]]
    next_cursor = codec.encode("events", str(project_id), json.dumps([
        str(rows[limit - 1]["stream_id"]), rows[limit - 1]["sequence"], str(rows[limit - 1]["event_id"])]),
                               filters) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/runs/{run_id}/events")
async def list_run_events(request: Request, run_id: UUID, project_id: UUID | None = None,
                          limit: int = 100, cursor: str | None = None,
                          stream_id: UUID | None = None, type: str | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await list_events(request, project_id, run_id, limit, cursor, stream_id, type)


@router.get("/projects/{project_id}/runs/{run_id}/watch")
async def watch_run(request: Request, project_id: UUID, run_id: UUID):
    engine, error = await scoped(request, project_id, "runs:read")
    if error is not None:
        return error
    resume_id = request.headers.get("Last-Event-ID")
    try:
        resume_uuid = UUID(resume_id) if resume_id else None
    except ValueError:
        return failure(request, 400, "invalid_request")
    async with project_connection(engine, project_id) as connection:
        exists = (await connection.execute(text("""
            SELECT 1 FROM runs WHERE id = :run AND project_id = :project
        """), {"run": run_id, "project": project_id})).scalar_one_or_none()
        if exists is None:
            return failure(request, 404, "not_found")
        position = None
        if resume_uuid:
            position = (await connection.execute(text("""
                SELECT created_at, event_id FROM events
                WHERE project_id = :project AND run_id = :run AND event_id = :event
            """), {"project": project_id, "run": run_id, "event": resume_uuid})).mappings().first()
            if position is None:
                return failure(request, 409, "resume_unavailable")

    async def frames():
        current = (position["created_at"], position["event_id"]) if position else None
        while not await request.is_disconnected():
            async with project_connection(engine, project_id) as connection:
                rows = (await connection.execute(text("""
                    SELECT event_id, created_at, event_type, payload FROM events
                    WHERE project_id = :project AND run_id = :run
                      AND (:first OR (created_at, event_id) >
                           (CAST(:created AS timestamptz), CAST(:event AS uuid)))
                    ORDER BY created_at, event_id LIMIT 500
                """), {"project": project_id, "run": run_id, "first": current is None,
                       "created": current[0] if current else None,
                       "event": current[1] if current else None})).mappings().all()
                sealed = (await connection.execute(text("""
                    SELECT capture_status != 'open' FROM runs
                    WHERE project_id = :project AND id = :run
                """), {"project": project_id, "run": run_id})).scalar_one()
            for row in rows:
                current = (row["created_at"], row["event_id"])
                yield f'id: {row["event_id"]}\nevent: {row["event_type"]}\ndata: {json.dumps(row["payload"])}\n\n'
            if sealed and not rows:
                break
            if not rows:
                yield ': keepalive\n\n'
                await asyncio.sleep(2)

    return StreamingResponse(frames(), media_type="text/event-stream")


@router.get("/runs/{run_id}/watch")
async def watch_run_by_id(request: Request, run_id: UUID, project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await watch_run(request, project_id, run_id)


@router.get("/projects/{project_id}/jobs/{job_id}")
async def get_job(request: Request, project_id: UUID, job_id: UUID):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    result = await JobQueue(engine, timedelta(seconds=30)).get(project_id, job_id)
    return json.loads(json.dumps(result, default=str)) if result else failure(request, 404, "not_found")


@router.get("/jobs/{job_id}")
async def get_job_by_id(request: Request, job_id: UUID, project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await get_job(request, project_id, job_id)


@router.post("/projects/{project_id}/jobs/{job_id}:cancel")
async def cancel_job(request: Request, project_id: UUID, job_id: UUID):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    try:
        result = await JobQueue(engine, timedelta(seconds=30)).cancel(project_id, job_id)
    except LeaseLost:
        return failure(request, 409, "conflict")
    return json.loads(json.dumps(result, default=str)) if result else failure(request, 404, "not_found")


@router.post("/jobs/{job_id}:cancel")
async def cancel_job_by_id(request: Request, job_id: UUID, project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await cancel_job(request, project_id, job_id)


@router.post("/projects/{project_id}/deletions", status_code=202)
async def request_deletion(request: Request, project_id: UUID, payload: DeletionCreate):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    if payload.target_kind == "suite":
        return failure(request, 400, "invalid_request")
    table = "runs" if payload.target_kind == "run" else "recordings"
    deletion_id, job_id = new_uuid7(), new_uuid7()
    async with project_connection(engine, project_id) as connection:
        exists = (await connection.execute(text(f"""
            SELECT id FROM {table} WHERE project_id = :project AND id = :target
        """), {"project": project_id, "target": payload.target_id})).scalar_one_or_none()
        if exists is None:
            return failure(request, 404, "not_found")
        existing = (await connection.execute(text("""
            SELECT id, job_id FROM deletion_requests
            WHERE project_id = :project AND target_kind = :kind AND target_id = :target
        """), {"project": project_id, "kind": payload.target_kind,
               "target": payload.target_id})).mappings().first()
        if existing:
            deletion_id, job_id = existing["id"], existing["job_id"]
        else:
            await connection.execute(text("""
                INSERT INTO jobs(id, project_id, kind) VALUES (:job, :project, 'deletion')
            """), {"job": job_id, "project": project_id})
            await connection.execute(text("""
                INSERT INTO deletion_requests(id, project_id, target_kind, target_id, job_id)
                VALUES (:id, :project, :kind, :target, :job)
            """), {"id": deletion_id, "project": project_id, "kind": payload.target_kind,
                   "target": payload.target_id, "job": job_id})
    return {"job_id": str(job_id), "job_url": f"/v1/projects/{project_id}/jobs/{job_id}",
            "status": "queued", "deletion_id": str(deletion_id)}


@router.get("/projects/{project_id}/deletions/{deletion_id}")
async def get_deletion(request: Request, project_id: UUID, deletion_id: UUID):
    engine, error = await scoped(request, project_id, "admin")
    if error is not None:
        return error
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT deletion_requests.id, deletion_requests.project_id, target_kind, target_id,
                   audit_event_id, deletion_requests.created_at, jobs.status
            FROM deletion_requests JOIN jobs ON jobs.id = deletion_requests.job_id
            WHERE deletion_requests.id = :id AND deletion_requests.project_id = :project
        """), {"id": deletion_id, "project": project_id})).mappings().first()
    if row is None:
        return failure(request, 404, "not_found")
    return {"id": str(row["id"]), "project_id": str(row["project_id"]),
            "target_kind": row["target_kind"], "target_id": str(row["target_id"]),
            "status": row["status"], "audit_event_id": str(row["audit_event_id"]) if row["audit_event_id"] else None,
            "created_at": row["created_at"].isoformat()}


@router.get("/deletions/{deletion_id}")
async def get_deletion_by_id(request: Request, deletion_id: UUID, project_id: UUID | None = None):
    if project_id is None:
        return failure(request, 400, "invalid_request")
    return await get_deletion(request, project_id, deletion_id)
