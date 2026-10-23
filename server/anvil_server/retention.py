"""Project-scoped retention, quota and deletion graph rules."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Resource:
    id: str
    project_id: str
    kind: str
    bytes: int = 0
    parents: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    protected: bool = False


@dataclass(frozen=True)
class Backup:
    id: str
    project_id: str
    created_at: datetime
    retain_for: timedelta


class RetentionManager:
    def __init__(self, now=None):
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.resources: dict[str, Resource] = {}
        self.backups: dict[str, Backup] = {}
        self.quotas: dict[str, int] = {}
        self.policies: dict[str, timedelta] = {}
        self.audit: list[dict] = []

    def usage(self, project_id: str) -> int:
        return sum(item.bytes for item in self.resources.values() if item.project_id == project_id)

    def set_quota(self, project_id: str, max_bytes: int):
        if not project_id or max_bytes < 0 or self.usage(project_id) > max_bytes:
            raise ValueError("Invalid storage quota")
        self.quotas[project_id] = max_bytes

    def set_policy(self, project_id: str, retention: timedelta | None):
        if not project_id or retention is not None and retention.total_seconds() < 0:
            raise ValueError("Invalid retention policy")
        self.policies[project_id] = retention or timedelta(days=30)

    def put(self, resource: Resource):
        if not resource.id or not resource.project_id or not resource.kind or resource.bytes < 0:
            raise ValueError("Invalid resource")
        if resource.id in self.resources:
            raise ValueError("Resource already exists")
        if resource.kind != "run" and any(
            parent not in self.resources or self.resources[parent].project_id != resource.project_id
            for parent in resource.parents
        ):
            raise ValueError("Missing or cross-project parent")
        if self.usage(resource.project_id) + resource.bytes > self.quotas.get(resource.project_id, float("inf")):
            raise ValueError("Storage quota exceeded")
        self.resources[resource.id] = resource

    def delete(self, project_id: str, roots: list[str], reason: str) -> dict:
        if not project_id or not roots:
            raise ValueError("Invalid deletion")
        selected = set(roots)
        for root in roots:
            if root not in self.resources or self.resources[root].project_id != project_id:
                raise ValueError("Unknown or cross-project resource")
        changed = True
        while changed:
            previous = len(selected)
            selected.update(resource.id for resource in self.resources.values()
                            if resource.project_id == project_id and selected.intersection(resource.parents))
            changed = len(selected) != previous
        removed = sorted(selected)
        size = sum(self.resources[item].bytes for item in removed)
        for item in removed:
            del self.resources[item]
        audit = {"project_id": project_id, "kind": "delete", "root_ids": list(roots),
                 "resource_ids": removed, "bytes": size, "reason": reason, "created_at": self.now()}
        self.audit.append(audit)
        return audit

    def sweep(self, project_id: str) -> list[dict]:
        cutoff = self.now() - self.policies.get(project_id, timedelta(days=30))
        roots = sorted(item.id for item in self.resources.values() if item.project_id == project_id
                       and not item.protected and item.created_at <= cutoff)
        return [self.delete(project_id, roots, "retention")] if roots else []

    def put_backup(self, backup: Backup):
        if not backup.id or not backup.project_id or backup.retain_for.total_seconds() < 0:
            raise ValueError("Invalid backup")
        if backup.id in self.backups:
            raise ValueError("Duplicate backup")
        self.backups[backup.id] = backup

    def prune_backups(self, project_id: str) -> list[str]:
        expired = sorted(item.id for item in self.backups.values() if item.project_id == project_id
                         and self.now() >= item.created_at + item.retain_for)
        for item in expired:
            del self.backups[item]
        return expired
