"""Lease-bound background seal worker; no replay or external tool execution."""

import asyncio
import os
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .capture import seal_run
from .database import engine_for, project_connection
from .identifiers import new_uuid7
from .jobs import JobQueue


async def process_seal(engine: AsyncEngine, project_id: UUID, job_id: UUID) -> bool:
    queue = JobQueue(engine, timedelta(seconds=30))
    token = new_uuid7()
    if await queue.acquire_job(project_id, job_id, token) is None:
        return False
    async with project_connection(engine, project_id) as connection:
        entry = (await connection.execute(text("""
            SELECT run_id, acknowledge_incomplete FROM seal_requests
            WHERE project_id = :project AND job_id = :job
        """), {"project": project_id, "job": job_id})).mappings().first()
    if entry is None:
        await queue.complete(project_id, job_id, token, "failed")
        return False
    try:
        await seal_run(engine, project_id, entry["run_id"],
                       entry["acknowledge_incomplete"], from_worker=True,
                       job_id=job_id, lease_token=token)
    except ValueError:
        async with project_connection(engine, project_id) as connection:
            await connection.execute(text("""
                UPDATE jobs SET error = 'sealing validation failed'
                WHERE id = :job AND project_id = :project AND lease_token = :token
            """), {"job": job_id, "project": project_id, "token": token})
        try:
            await queue.complete(project_id, job_id, token, "failed")
        except ValueError:
            return False
        return False
    return True


async def serve_seals(engine: AsyncEngine, project_id: UUID, interval: float = 1.0):
    queue = JobQueue(engine, timedelta(seconds=30))
    while True:
        await queue.recover_expired(project_id)
        async with project_connection(engine, project_id) as connection:
            queued = (await connection.execute(text("""
                SELECT id FROM jobs WHERE project_id = :project AND kind = 'seal'
                  AND status = 'queued' ORDER BY created_at, id LIMIT 1
            """), {"project": project_id})).scalar_one_or_none()
        if queued is None:
            await asyncio.sleep(interval)
            continue
        await process_seal(engine, project_id, queued)


def main():
    database_url = os.environ["ANVIL_DATABASE_URL"]
    project_id = UUID(os.environ["ANVIL_WORKER_PROJECT_ID"])

    async def run():
        engine = engine_for(database_url)
        try:
            await serve_seals(engine, project_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
