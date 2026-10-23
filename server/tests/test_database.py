from uuid import UUID

import pytest

from anvil_server.database import engine_for, project_connection


def test_only_postgres_urls_accepted():
    with pytest.raises(ValueError):
        engine_for("sqlite+aiosqlite:///anvil.db")


@pytest.mark.asyncio
async def test_project_scope_rejects_non_uuid():
    with pytest.raises(ValueError):
        async with project_connection(None, "wrong"):
            pass


def test_project_id_type():
    assert isinstance(UUID("0199b167-0000-7000-8000-000000000011"), UUID)
