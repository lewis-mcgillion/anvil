"""Project-scoped PostgreSQL connections with transaction-local RLS context."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncConnection, create_async_engine


def engine_for(database_url: str) -> AsyncEngine:
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("Anvil requires an asyncpg PostgreSQL URL")
    return create_async_engine(database_url, pool_pre_ping=True)


@asynccontextmanager
async def project_connection(engine: AsyncEngine, project_id: UUID) -> AsyncIterator[AsyncConnection]:
    if not isinstance(project_id, UUID):
        raise ValueError("A valid project UUID is required")
    async with engine.begin() as connection:
        await connection.execute(text("SELECT set_config('anvil.project_id', :project_id, true)"),
                                 {"project_id": str(project_id)})
        yield connection
