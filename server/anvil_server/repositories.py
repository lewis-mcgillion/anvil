"""Project-scoped persistence with no ambient privileged query methods."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection


class RunRepository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def create(self, project_id: UUID, run_id: UUID, status: str = "running") -> None:
        async with project_connection(self.engine, project_id) as connection:
            await connection.execute(text("""
                INSERT INTO runs (id, project_id, execution_outcome)
                VALUES (:run, :project, :status)
            """), {"run": run_id, "project": project_id, "status": status})

    async def get(self, project_id: UUID, run_id: UUID) -> dict | None:
        async with project_connection(self.engine, project_id) as connection:
            row = (await connection.execute(text("""
                SELECT id, project_id, execution_outcome, capture_status FROM runs
                WHERE project_id = :project AND id = :run
            """), {"project": project_id, "run": run_id})).mappings().first()
            return dict(row) if row else None

    async def update_outcome(self, project_id: UUID, run_id: UUID, status: str) -> bool:
        async with project_connection(self.engine, project_id) as connection:
            result = await connection.execute(text("""
                UPDATE runs SET execution_outcome = :status
                WHERE project_id = :project AND id = :run
            """), {"project": project_id, "run": run_id, "status": status})
            return result.rowcount == 1

    async def delete(self, project_id: UUID, run_id: UUID) -> bool:
        async with project_connection(self.engine, project_id) as connection:
            result = await connection.execute(text("""
                DELETE FROM runs WHERE project_id = :project AND id = :run
            """), {"project": project_id, "run": run_id})
            return result.rowcount == 1
