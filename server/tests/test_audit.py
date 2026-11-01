"""ANVIL-071: what an audit row is allowed to contain."""

import json
from uuid import UUID

import pytest

from anvil_server import audit
from anvil_server.audit import AuditError

PROJECT = UUID("0199b167-0000-7000-8000-000000000011")
TARGET = UUID("0199b167-0000-7000-8000-000000000021")
OTHER = UUID("0199b167-0000-7000-8000-000000000022")
PROMPT = "Summarise the patient's record. Authorization: Bearer sk-canary0123456789"


class StubConnection:
    """Records the parameters an audit write would send to PostgreSQL."""

    def __init__(self):
        self.calls = []

    async def execute(self, statement, parameters=None):
        self.calls.append((str(statement), parameters or {}))
        return None


async def write(**kwargs):
    connection = StubConnection()
    audit_id = await audit.record(connection, PROJECT, **kwargs)
    assert len(connection.calls) == 1
    return audit_id, connection.calls[0][1]


class TestAcceptedRows:
    @pytest.mark.asyncio
    async def test_a_valid_row_is_written_with_its_identifiers(self):
        audit_id, parameters = await write(kind="delete", reason="user_request",
                                           root_ids=[TARGET], resource_ids=[TARGET, OTHER],
                                           size_bytes=4096)
        assert audit_id.version == 7
        assert parameters["project"] == PROJECT
        assert parameters["kind"] == "delete"
        assert parameters["reason"] == "user_request"
        assert json.loads(parameters["roots"]) == [str(TARGET)]
        assert json.loads(parameters["resources"]) == [str(TARGET), str(OTHER)]
        assert parameters["bytes"] == 4096

    @pytest.mark.asyncio
    async def test_identifiers_may_be_given_as_strings(self):
        _, parameters = await write(kind="read", reason="recording_read",
                                    root_ids=[str(TARGET)])
        assert json.loads(parameters["roots"]) == [str(TARGET)]

    @pytest.mark.asyncio
    async def test_an_action_touching_nothing_still_records_the_attempt(self):
        _, parameters = await write(kind="read", reason="comparison_read")
        assert json.loads(parameters["roots"]) == []
        assert parameters["bytes"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,reason", sorted(
        (kind, reason) for kind, reasons in audit.REASONS.items() for reason in reasons))
    async def test_every_published_kind_and_reason_pairing_is_writable(self, kind, reason):
        _, parameters = await write(kind=kind, reason=reason)
        assert (parameters["kind"], parameters["reason"]) == (kind, reason)

    def test_every_kind_has_at_least_one_reason(self):
        assert set(audit.REASONS) == audit.KINDS
        assert all(audit.REASONS[kind] for kind in audit.KINDS)


class TestRejectedRows:
    """The audit trail must not become a second copy of the evidence."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["root_ids", "resource_ids"])
    async def test_a_prompt_cannot_be_smuggled_in_as_an_identifier(self, field):
        with pytest.raises(AuditError):
            await write(kind="read", reason="recording_read", **{field: [PROMPT]})

    @pytest.mark.asyncio
    async def test_an_unknown_kind_is_refused(self):
        with pytest.raises(AuditError):
            await write(kind="execute", reason="user_request")

    @pytest.mark.asyncio
    async def test_a_reason_belonging_to_another_kind_is_refused(self):
        # 'user_request' is a deletion reason; a read must not borrow it.
        with pytest.raises(AuditError):
            await write(kind="read", reason="user_request")

    @pytest.mark.asyncio
    async def test_a_free_text_reason_is_refused(self):
        with pytest.raises(AuditError):
            await write(kind="read", reason=PROMPT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [-1, 1.5, "4096", True, None])
    async def test_an_invalid_size_is_refused(self, value):
        with pytest.raises(AuditError):
            await write(kind="export", reason="archive_download", size_bytes=value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["not-a-list", 7, {"id": str(TARGET)}])
    async def test_identifiers_must_be_a_list(self, value):
        with pytest.raises(AuditError):
            await write(kind="read", reason="recording_read", root_ids=value)

    @pytest.mark.asyncio
    async def test_an_unbounded_identifier_list_is_refused(self):
        with pytest.raises(AuditError):
            await write(kind="delete", reason="retention",
                        resource_ids=[TARGET] * (audit.MAX_IDS + 1))

    @pytest.mark.asyncio
    async def test_a_rejected_row_writes_nothing(self):
        connection = StubConnection()
        with pytest.raises(AuditError):
            await audit.record(connection, PROJECT, kind="read", reason="recording_read",
                               root_ids=[PROMPT])
        assert connection.calls == []


class TestErrorsDoNotLeak:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [
        {"kind": "read", "reason": "recording_read", "root_ids": [PROMPT]},
        {"kind": "read", "reason": PROMPT},
        {"kind": PROMPT, "reason": "recording_read"},
    ])
    async def test_a_refusal_never_quotes_what_it_refused(self, kwargs):
        # An audit error that echoes its input writes the payload into the
        # logs, which is the outcome the audit rules exist to prevent.
        with pytest.raises(AuditError) as raised:
            await write(**kwargs)
        assert PROMPT not in str(raised.value)
        assert "Bearer" not in str(raised.value)
