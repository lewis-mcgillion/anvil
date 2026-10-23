from uuid import UUID

from anvil_server.repositories import RunRepository


def test_run_repository_is_bound_to_database():
    repository = RunRepository(None)
    assert repository.engine is None
    assert UUID("0199b167-0000-7000-8000-000000000011").version == 7
