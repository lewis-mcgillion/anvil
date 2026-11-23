import json

import pytest

from anvil_server.replay.fixtures import Fixture, FixtureSession
from anvil_server.runner_fixtures import RecordedToolResolver, RecordedModelResolver, RecordedRunnerFixtures
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


def model_request(prompt):
    return RunnerMessage(model_request={
        "request_id": "model-1", "invocation_id": "root", "model": "recorded",
        "messages": [{"role": "user", "content": json.dumps(prompt).encode()}],
        "system_instruction": "", "generation": {},
    }).model_request


def model_fixture():
    return Fixture.create(kind="model", path=("root",), ordinal=0,
                          request={"model": "recorded", "messages": [{"role": "user", "content": "hello"}],
                                   "tools": [], "system_instruction": "",
                                   "generation": {"temperature": 0.0, "top_p": 0.0, "max_output_tokens": 0}},
                          response={"status": "completed", "response": "answer",
                                    "input_tokens": 2, "output_tokens": 1})


@pytest.mark.asyncio
async def test_recorded_model_resolver_returns_fixture_completion():
    session = FixtureSession([model_fixture()])
    response = await RecordedModelResolver(session)(model_request("hello"))
    assert response.model_response.completion.messages[0].role == "assistant"
    assert json.loads(response.model_response.completion.messages[0].content) == "answer"
    assert session.finish()["provider_calls"] == 0


@pytest.mark.asyncio
async def test_recorded_model_resolver_rejects_changed_prompt():
    session = FixtureSession([model_fixture()])
    with pytest.raises(IsolationUnavailable, match="did not match"):
        await RecordedModelResolver(session)(model_request("changed"))


@pytest.mark.asyncio
async def test_recorded_runner_fixtures_share_model_and_tool_ordinals():
    fixture = model_fixture()
    tool_fixture = Fixture.create(kind="tool", path=("root",), ordinal=1,
                                  request={"tool_name": "inventory.lookup", "schema_digest": "sha256:" + "a" * 64,
                                           "arguments": {"sku": "1"}},
                                  response={"status": "completed", "result": {"available": True}})
    session = RecordedRunnerFixtures(FixtureSession([fixture, tool_fixture]))
    await session.resolve_model(model_request("hello"))
    await session.resolve_tool(request({"sku": "1"}))
    await session.finalize()


@pytest.mark.asyncio
async def test_recorded_runner_rejects_incomplete_fixture_consumption():
    session = RecordedRunnerFixtures(FixtureSession([model_fixture()]))
    with pytest.raises(IsolationUnavailable, match="unconsumed"):
        await session.finalize()
