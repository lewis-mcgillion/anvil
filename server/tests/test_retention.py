from datetime import datetime, timedelta, timezone

import pytest

from anvil_server.retention import Backup, Resource, RetentionManager


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def test_project_graph_deletion_and_quota():
    manager = RetentionManager(lambda: NOW)
    manager.set_quota("one", 100)
    manager.put(Resource("run", "one", "run", 10))
    manager.put(Resource("artifact", "one", "artifact", 20, ("run",)))
    manager.put(Resource("derived", "one", "derived", 5, ("artifact",)))
    manager.put(Resource("other", "two", "run", 10))
    with pytest.raises(ValueError):
        manager.put(Resource("invalid", "one", "artifact", 5, ("other",)))
    with pytest.raises(ValueError):
        manager.set_quota("one", 10)
    with pytest.raises(ValueError):
        manager.delete("one", ["other"], "bad")
    assert manager.delete("one", ["run"], "requested")["resource_ids"] == ["artifact", "derived", "run"]
    assert manager.usage("one") == 0
    assert manager.usage("two") == 10


def test_retention_and_backups_are_separate():
    manager = RetentionManager(lambda: NOW)
    old = NOW - timedelta(days=40)
    manager.put(Resource("old", "one", "run", created_at=old))
    manager.put(Resource("protected", "one", "run", created_at=old, protected=True))
    assert manager.sweep("one")[0]["root_ids"] == ["old"]
    assert "protected" in manager.resources
    manager.put_backup(Backup("backup", "one", old, timedelta(days=30)))
    manager.put_backup(Backup("different", "two", old, timedelta(days=30)))
    assert manager.prune_backups("one") == ["backup"]
    assert "different" in manager.backups
