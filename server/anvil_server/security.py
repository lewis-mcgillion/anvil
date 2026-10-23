"""Database-backed project authorization using one-way API key hashes."""

from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .auth import bearer_token, key_hash
from .database import project_connection


class AccessDenied(ValueError):
    pass


async def authorize(engine: AsyncEngine, request: Request, project_id: UUID, required: str) -> None:
    try:
        token = bearer_token(request.headers.get("authorization"))
    except ValueError as error:
        raise AccessDenied("Missing or invalid credentials") from error
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT scopes FROM api_keys WHERE project_id = :project
              AND key_hash = :hash AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > clock_timestamp())
        """), {"project": project_id, "hash": key_hash(token)})).first()
    if row is None or required not in row.scopes:
        raise AccessDenied("Permission denied")
