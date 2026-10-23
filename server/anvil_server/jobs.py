"""Durable PostgreSQL queue with bounded leases and fail-closed transitions."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection


class LeaseLost(ValueError):
    pass


class JobQueue:
    def __init__(self, engine: AsyncEngine, lease_duration: timedelta):
        if lease_duration.total_seconds() <= 0:
            raise ValueError("Lease duration must be positive")
        self.engine = engine
        self.lease_duration = lease_duration

    async def create(self, project_id: UUID, job_id: UUID, kind: str, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("Invalid retry limit")
        async with project_connection(self.engine, project_id) as connection:
            await connection.execute(text("""
                INSERT INTO jobs (id, project_id, kind, max_attempts)
                VALUES (:id, :project, :kind, :attempts)
            """), {"id": job_id, "project": project_id, "kind": kind, "attempts": max_attempts})

    async def acquire(self, project_id: UUID, lease_token: UUID):
        async with project_connection(self.engine, project_id) as connection:
            row = (await connection.execute(text("""
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                  lease_token = :token, leased_at = clock_timestamp(),
                  heartbeat_at = clock_timestamp(),
                  lease_expires_at = clock_timestamp() + (:seconds * interval '1 second'),
                  error = NULL, finished_at = NULL
                WHERE id = (
                  SELECT id FROM jobs WHERE project_id = :project AND attempts < max_attempts
                  AND (status = 'queued' OR (status IN ('running','cancelling')
                    AND lease_expires_at < clock_timestamp()))
                  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
                ) RETURNING id, status, attempts
            """), {"project": project_id, "token": lease_token,
                   "seconds": self.lease_duration.total_seconds()})).mappings().first()
            return dict(row) if row else None

    async def acquire_kind(self, project_id: UUID, lease_token: UUID, kind: str):
        async with project_connection(self.engine, project_id) as connection:
            row = (await connection.execute(text("""
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                  lease_token = :token, leased_at = clock_timestamp(),
                  heartbeat_at = clock_timestamp(),
                  lease_expires_at = clock_timestamp() + (:seconds * interval '1 second'),
                  error = NULL, finished_at = NULL
                WHERE id = (
                  SELECT id FROM jobs WHERE project_id = :project AND kind = :kind
                  AND attempts < max_attempts AND status = 'queued'
                  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
                ) RETURNING id, status, attempts
            """), {"project": project_id, "token": lease_token, "kind": kind,
                   "seconds": self.lease_duration.total_seconds()})).mappings().first()
            return dict(row) if row else None

    async def acquire_job(self, project_id: UUID, job_id: UUID, lease_token: UUID):
        async with project_connection(self.engine, project_id) as connection:
            row = (await connection.execute(text("""
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                  lease_token = :token, leased_at = clock_timestamp(),
                  heartbeat_at = clock_timestamp(),
                  lease_expires_at = clock_timestamp() + (:seconds * interval '1 second'),
                  error = NULL, finished_at = NULL
                WHERE id = :job AND project_id = :project
                  AND (status = 'queued' OR (status = 'running'
                    AND lease_expires_at < clock_timestamp()))
                  AND attempts < max_attempts
                RETURNING id, status, attempts
            """), {"project": project_id, "job": job_id, "token": lease_token,
                   "seconds": self.lease_duration.total_seconds()})).mappings().first()
            return dict(row) if row else None

    async def heartbeat(self, project_id: UUID, job_id: UUID, lease_token: UUID) -> None:
        await self._leased_update(project_id, job_id, lease_token, """
            heartbeat_at = clock_timestamp(),
            lease_expires_at = clock_timestamp() + (:seconds * interval '1 second')
        """, {"seconds": self.lease_duration.total_seconds()})

    async def complete(self, project_id: UUID, job_id: UUID, lease_token: UUID, status: str) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Invalid terminal status")
        await self._leased_update(project_id, job_id, lease_token, """
            status = :status, outcome = :status, finished_at = clock_timestamp(),
            lease_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL
        """, {"status": status})

    async def _leased_update(self, project_id, job_id, lease_token, updates, extra):
        async with project_connection(self.engine, project_id) as connection:
            result = await connection.execute(text(f"""
                UPDATE jobs SET {updates}
                WHERE project_id = :project AND id = :id AND lease_token = :token
                  AND status IN ('running', 'cancelling')
                  AND lease_expires_at > clock_timestamp()
            """), {"project": project_id, "id": job_id, "token": lease_token, **extra})
            if result.rowcount != 1:
                raise LeaseLost("Lease expired or does not belong to this worker")

    async def recover_expired(self, project_id: UUID) -> int:
        async with project_connection(self.engine, project_id) as connection:
            result = await connection.execute(text("""
                UPDATE jobs
                SET status = CASE WHEN status = 'cancelling' THEN 'cancelled'
                                  WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                    outcome = CASE WHEN status = 'cancelling' THEN 'cancelled'
                                   WHEN attempts < max_attempts THEN 'interrupted' ELSE 'failed' END,
                    error = 'worker lease expired',
                    finished_at = CASE WHEN status <> 'cancelling' AND attempts < max_attempts
                                       THEN NULL ELSE clock_timestamp() END,
                    lease_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                WHERE project_id = :project AND status IN ('running', 'cancelling')
                  AND lease_expires_at < clock_timestamp()
            """), {"project": project_id})
            return result.rowcount

    async def get(self, project_id: UUID, job_id: UUID) -> dict | None:
        async with project_connection(self.engine, project_id) as connection:
            row = (await connection.execute(text("""
                SELECT id, project_id, kind, status, outcome, attempts,
                       max_attempts, lease_token, lease_expires_at, result_url,
                       error, created_at, finished_at
                FROM jobs WHERE project_id = :project AND id = :job
            """), {"project": project_id, "job": job_id})).mappings().first()
            return dict(row) if row else None

    async def cancel(self, project_id: UUID, job_id: UUID) -> dict | None:
        async with project_connection(self.engine, project_id) as connection:
            existing = (await connection.execute(text("""
                SELECT status FROM jobs WHERE project_id = :project AND id = :job FOR UPDATE
            """), {"project": project_id, "job": job_id})).scalar_one_or_none()
            if existing in {"succeeded", "failed"}:
                raise LeaseLost("Finished jobs cannot be cancelled")
            if existing is None:
                return None
            row = (await connection.execute(text("""
                UPDATE jobs SET
                  status = CASE WHEN status = 'queued' THEN 'cancelled'
                                WHEN status = 'cancelled' THEN 'cancelled' ELSE 'cancelling' END,
                  outcome = CASE WHEN status = 'queued' THEN 'cancelled' ELSE outcome END,
                  finished_at = CASE WHEN status = 'queued' THEN clock_timestamp() ELSE finished_at END
                WHERE project_id = :project AND id = :job
                  AND status IN ('queued', 'running', 'cancelling', 'cancelled')
                RETURNING id, status, outcome
            """), {"project": project_id, "job": job_id})).mappings().first()
            return dict(row) if row else None
