"""Durable audit trail for authenticated access, export, replay and deletion.

The audit trail is the record of who touched what. It is therefore the last
place that should hold the data it describes: an audit row that quoted a
prompt would turn the safety mechanism into a second, longer-lived copy of the
evidence it exists to protect.

So a row carries identifiers, counts and a reason drawn from a fixed
vocabulary, and nothing else. There is no free-text field, because a free-text
field is eventually given a payload.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text

from .identifiers import new_uuid7

# The audited action classes. ``read`` covers authenticated access to stored
# evidence, ``export`` covers anything that produces bytes a third party can
# keep, ``replay`` covers re-derivation from a recording, and ``delete``
# covers destruction.
KINDS = frozenset({"read", "export", "replay", "delete"})

# Reasons are a closed vocabulary per kind rather than a string the caller
# chooses. A caller that can write arbitrary text into an audit row will
# eventually write something sensitive into it.
REASONS: dict[str, frozenset[str]] = {
    "read": frozenset({"recording_read", "projection_read", "comparison_read",
                       "divergence_read"}),
    "export": frozenset({"archive_download", "export_queued"}),
    "replay": frozenset({"comparison_queued", "projection_built"}),
    "delete": frozenset({"user_request", "retention"}),
}

MAX_IDS = 1000


class AuditError(ValueError):
    """An audit row was refused. The message never quotes the input."""


def _identifiers(values, field: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise AuditError(f"{field} must be a list of identifiers")
    if len(values) > MAX_IDS:
        raise AuditError(f"{field} exceeds {MAX_IDS} identifiers")
    checked = []
    for value in values:
        if isinstance(value, UUID):
            checked.append(str(value))
            continue
        # Parsing rather than pattern-matching: an identifier that is not a
        # UUID is the shape a leaked prompt or token would arrive in.
        try:
            checked.append(str(UUID(str(value))))
        except (ValueError, AttributeError, TypeError):
            raise AuditError(f"{field} must contain only UUID identifiers") from None
    return checked


async def record(connection, project_id: UUID, *, kind: str, reason: str,
                 root_ids=(), resource_ids=(), size_bytes: int = 0) -> UUID:
    """Write one audit row and return its identifier.

    Takes a connection rather than an engine so the caller can record the
    action in the same transaction as the action itself. An audit row written
    in a separate transaction is a row that survives a rolled-back deletion,
    or is lost by a crash between the two commits.
    """
    if kind not in KINDS:
        raise AuditError("Unknown audit kind")
    if reason not in REASONS[kind]:
        raise AuditError("Unknown audit reason for this kind")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise AuditError("size_bytes must be a non-negative integer")
    roots = _identifiers(root_ids, "root_ids")
    resources = _identifiers(resource_ids, "resource_ids")
    audit_id = new_uuid7()
    await connection.execute(text("""
        INSERT INTO audit_events(id, project_id, kind, root_ids, resource_ids, bytes, reason)
        VALUES (:id, :project, :kind, CAST(:roots AS jsonb), CAST(:resources AS jsonb),
                :bytes, :reason)
    """), {"id": audit_id, "project": project_id, "kind": kind,
           "roots": json.dumps(roots), "resources": json.dumps(resources),
           "bytes": size_bytes, "reason": reason})
    return audit_id
