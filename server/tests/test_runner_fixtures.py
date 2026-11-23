import json

import pytest

from anvil_server.replay.fixtures import Fixture, FixtureSession
from anvil_server.runner_fixtures import RecordedToolResolver
from anvil_server.runner_pb2 import RunnerMessage
from anvil_server.runner_policy import IsolationUnavailable


def request(arguments):
    return RunnerMessage(tool_request={
        "request_id": "call-1", "invocation_id": "root", "tool_name": "inventory.lookup",
        "schema_digest": "sha256:" + "a" * 64, "arguments": json.dumps(arguments).encode(),
    }).tool_request


@pytest.mark.asyncio
async def test_recorded_tool_resolver_returns_only_fixture_result():
    fixture = Fixture.create(kind="tool", path=("root",), ordinal=0,
                             request={"tool_name": "inventory.lookup", "schema_digest": "sha256:" + "a" * 64,
                                      "arguments": {"sku": "1"}},
                             response={"status": "completed", "result": {"available": True}})
    session = FixtureSession([fixture])
    response = await RecordedToolResolver(session)(request({"sku": "1"}))
    assert json.loads(response.tool_response.result_json) == {"available": True}
    assert session.finish()["provider_calls"] == 0


@pytest.mark.asyncio
async def test_recorded_tool_resolver_rejects_changed_arguments():
    fixture = Fixture.create(kind="tool", path=("root",), ordinal=0,
                             request={"tool_name": "inventory.lookup", "schema_digest": "sha256:" + "a" * 64,
                                      "arguments": {"sku": "1"}},
                             response={"status": "completed", "result": {"available": True}})
    session = FixtureSession([fixture])
    with pytest.raises(IsolationUnavailable, match="did not match"):
        await RecordedToolResolver(session)(request({"sku": "other"}))
