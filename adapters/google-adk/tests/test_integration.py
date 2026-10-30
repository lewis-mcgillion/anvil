"""Offline conformance against the actual pinned ADK Runner (Apache-2.0)."""

import asyncio
import json
from pathlib import Path
from typing import ClassVar

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
import pytest

from anvil_adk.plugin import CapturePlugin, Ineligible, export_state, import_state
from anvil_adk.replay import ReplayModel, ReplayTool


class SampleModel(BaseLlm):
    model: str = "offline"
    calls: ClassVar[int] = 0

    async def generate_content_async(self, llm_request, stream=False):
        type(self).calls += 1
        has_result = any(
            part.function_response
            for content in llm_request.contents for part in content.parts or []
        )
        if has_result:
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text="done")],
            ))
        else:
            yield LlmResponse(content=types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(
                    id="call-1", name="lookup", args={"sku": "A"},
                ))],
            ))


async def execute(agent, plugins=(), config=None, state=None):
    service = InMemorySessionService()
    session = await service.create_session(app_name="sample", user_id="user", state=state or {})
    runner = Runner(app=App(name="sample", root_agent=agent, plugins=list(plugins)), session_service=service)
    try:
        events = [event async for event in runner.run_async(
            user_id="user", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="find A")]),
            run_config=config,
        )]
        return events
    finally:
        await runner.close()


def sample(tool):
    return LlmAgent(name="sample", model=SampleModel(), tools=[tool])


async def test_real_capture_preserves_outcome_and_payload_schema():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    calls = []

    def lookup(sku: str) -> dict:
        calls.append(sku)
        return {"available": True}

    baseline = await execute(sample(lookup))
    captured = []
    plugin = CapturePlugin(lambda *event: captured.append(event))
    instrumented = await execute(sample(lookup), [plugin])
    assert baseline[-1].content == instrumented[-1].content
    assert calls == ["A", "A"]
    kinds = [event[0] for event in captured]
    assert kinds.count("model.requested") == 2
    assert kinds.count("model.completed") == 2
    assert kinds.count("tool.requested") == kinds.count("tool.completed") == 1
    assert "message.created" in kinds
    assert not plugin._models and not plugin._chunks
    schema_dir = Path(__file__).resolve().parents[3] / "schemas" / "v1"
    resources = [Resource.from_contents(json.loads(path.read_text())) for path in schema_dir.glob("*.json")]
    registry = Registry().with_resources((resource.id(), resource) for resource in resources)
    schema = json.loads((schema_dir / "event.schema.json").read_text())
    validator = Draft202012Validator(schema, registry=registry)
    for kind, payload, correlation in captured:
        envelope = {
            "schema_version": "1", "event_id": "01900000-0000-7000-8000-000000000001",
            "run_id": "01900000-0000-7000-8000-000000000002",
            "stream_id": "01900000-0000-7000-8000-000000000003",
            "invocation_id": "01900000-0000-7000-8000-000000000004",
            "sequence": 0, "type": kind, "occurred_at": "2026-01-01T00:00:00.000Z",
            "trace_id": "1" * 32, "span_id": "1" * 16, "parent_event_ids": [],
            "payload": payload,
        }
        validator.validate(envelope)
        assert correlation["adk_version"] == "2.13.0"
        assert payload["replay_safe"] is False


async def test_real_hook_interception_fatal_side_effect_canary(tmp_path):
    marker = tmp_path / "production-side-effect"

    def lookup(sku: str) -> dict:
        marker.write_text("production tool executed")
        raise AssertionError("fatal production tool canary")

    class Intercept(BasePlugin):
        def __init__(self):
            super().__init__("spike")
            self.models = 0
            self.tools = 0

        async def before_model_callback(self, *, callback_context, llm_request):
            self.models += 1
            # Use real model input processing but never invoke a provider.
            return await anext(SampleModel().generate_content_async(llm_request))

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            self.tools += 1
            assert tool.name == "lookup" and tool_args == {"sku": "A"}
            return {"available": True}

    plugin = Intercept()
    before = SampleModel.calls
    events = await execute(sample(lookup), [plugin])
    assert plugin.models == 2 and plugin.tools == 1
    assert SampleModel.calls == before + 2
    assert events[-1].content.parts[0].text == "done"
    assert not marker.exists()


class FixtureResolver:
    def __init__(self):
        self.requests = []
        self.tools = []

    async def model(self, request):
        self.requests.append(request)
        response = await anext(SampleModel().generate_content_async(
            LlmRequest.model_validate(request),
        ))
        return response.model_dump(mode="json", exclude_none=True)

    async def tool(self, name, declaration, arguments, correlation):
        self.tools.append((name, declaration, arguments, correlation))
        return {"available": True}


async def test_replay_boundaries_use_real_runner_without_original_objects():
    resolver = FixtureResolver()
    agent = LlmAgent(
        name="sample", model=ReplayModel(model="offline", resolver=resolver),
        tools=[ReplayTool(declaration={
            "name": "lookup", "description": "Look up a SKU",
            "parameters": {"type": "OBJECT", "properties": {"sku": {"type": "STRING"}}},
        }, resolver=resolver)],
    )
    events = await execute(agent)
    assert events[-1].content.parts[0].text == "done"
    assert len(resolver.requests) == 2 and len(resolver.tools) == 1
    name, declaration, arguments, correlation = resolver.tools[0]
    assert name == "lookup" and arguments == {"sku": "A"}
    assert declaration["parameters"] and correlation["function_call_id"] == "call-1"
    assert "tools_dict" not in resolver.requests[0]
    assert resolver.requests[0]["config"]["tools"]


@pytest.mark.parametrize("failure", [KeyError("missing fixture"), Ineligible("ambiguous fixture")])
async def test_replay_miss_propagates_without_fallback(failure):
    class Reject(FixtureResolver):
        async def tool(self, *args):
            raise failure

    resolver = Reject()
    agent = LlmAgent(name="sample", model=ReplayModel(model="offline", resolver=resolver), tools=[
        ReplayTool(declaration={"name": "lookup"}, resolver=resolver),
    ])
    with pytest.raises(type(failure), match=str(failure).strip("'")):
        await execute(agent)


async def test_parallel_nested_agents_have_distinct_causal_branches():
    def lookup(sku: str) -> dict:
        return {"available": True}

    events = []
    agent = SequentialAgent(name="root", sub_agents=[
        ParallelAgent(name="parallel", sub_agents=[
            LlmAgent(name="left", model=SampleModel(), tools=[lookup]),
            LlmAgent(name="right", model=SampleModel(), tools=[lookup]),
        ]),
    ])
    await execute(agent, [CapturePlugin(lambda *event: events.append(event))])
    tools = [metadata for kind, _, metadata in events if kind == "tool.requested"]
    assert len(tools) == 2
    assert len({(entry["branch"], entry["agent_name"]) for entry in tools}) == 2
    for kind, payload, metadata in events:
        if kind == "message.created" and "author" in payload["content"]:
            assert metadata["agent_name"] == payload["content"]["author"]
            assert metadata["branch"] == payload["content"].get("branch", "")


async def test_tool_error_is_captured_without_recovery():
    def lookup(sku: str) -> dict:
        raise RuntimeError("offline tool failed")

    captured = []
    with pytest.raises(RuntimeError, match="offline tool failed"):
        await execute(sample(lookup), [CapturePlugin(lambda *event: captured.append(event))])
    failed = [payload for kind, payload, _ in captured if kind == "tool.failed"]
    assert len(failed) == 1 and failed[0]["error"]["type"] == "RuntimeError"


async def test_model_error_and_sink_failure_are_explicit():
    class Broken(BaseLlm):
        model: str = "offline"

        async def generate_content_async(self, llm_request, stream=False):
            raise RuntimeError("offline model failed")
            yield

    captured = []
    with pytest.raises(RuntimeError, match="offline model failed"):
        await execute(LlmAgent(name="broken", model=Broken()), [
            CapturePlugin(lambda *event: captured.append(event)),
        ])
    assert any(kind == "model.failed" for kind, _, _ in captured)

    def broken_sink(*args):
        raise RuntimeError("sink unavailable")

    with pytest.raises(Exception, match="sink unavailable"):
        await execute(LlmAgent(name="broken", model=Broken()), [CapturePlugin(broken_sink)])


async def test_cancellation_releases_capture_bookkeeping():
    entered = asyncio.Event()

    async def lookup(sku: str) -> dict:
        entered.set()
        await asyncio.Event().wait()

    captured = []
    plugin = CapturePlugin(lambda *event: captured.append(event))
    task = asyncio.create_task(execute(sample(lookup), [plugin]))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(kind == "tool.failed" and payload["error"]["type"] == "CancelledError"
               for kind, payload, _ in captured)
    assert not plugin._models and not plugin._chunks


async def test_streaming_chunks_preserve_sequence():
    class Stream(BaseLlm):
        model: str = "offline"

        async def generate_content_async(self, llm_request, stream=False):
            assert stream
            for text in ["a", "b"]:
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text=text)]), partial=True,
                )
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ab")]))

    captured = []
    await execute(LlmAgent(name="stream", model=Stream()), [
        CapturePlugin(lambda *event: captured.append(event)),
    ], RunConfig(streaming_mode=StreamingMode.SSE))
    assert [payload["chunk_index"] for kind, payload, _ in captured if kind == "model.chunk"] == [0, 1]
    assert sum(kind == "model.completed" for kind, _, _ in captured) == 1


@pytest.mark.parametrize("bad", [
    object(), b"pickle", (1, 2), {1: "not a string key"}, {"x": float("nan")},
    {"x": float("inf")}, {"x": object()},
])
def test_state_rejects_non_json(bad):
    with pytest.raises(Ineligible):
        export_state(bad)


@pytest.mark.parametrize("bad", [
    {}, {"schema_version": "2", "adapter": "google-adk", "state": {}},
    {"schema_version": "1", "adapter": "google-adk"},
    {"schema_version": "1", "adapter": "google-adk", "state": None},
])
def test_missing_state_rejected(bad):
    with pytest.raises(Ineligible):
        import_state(bad)


def test_state_roundtrip_is_detached_and_cycles_rejected():
    state = {"counter": 1, "nested": [None, True, 2.5]}
    restored = import_state(export_state(state))
    restored["nested"].append("changed")
    assert state["nested"] == [None, True, 2.5]
    state["cycle"] = state
    with pytest.raises(Ineligible):
        export_state(state)


def test_unsupported_version_rejected(monkeypatch):
    monkeypatch.setattr("anvil_adk.version", lambda _: "2.12.0")
    with pytest.raises(Ineligible, match="unsupported google-adk"):
        CapturePlugin(lambda *event: None)
    with pytest.raises(Ineligible, match="unsupported google-adk"):
        ReplayModel(model="offline", resolver=FixtureResolver())


async def test_real_session_reconstructed_from_explicit_snapshot():
    from google.adk.tools.tool_context import ToolContext

    snapshot = json.loads(
        (Path(__file__).resolve().parents[3] / "fixtures/adk/state-v1.json").read_text(),
    )
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="sample", user_id="user", state=import_state(snapshot),
    )

    def lookup(sku: str, tool_context: ToolContext) -> dict:
        tool_context.state["counter"] += 1
        return {"available": True}

    runner = Runner(
        app=App(name="sample", root_agent=sample(lookup)), session_service=service,
    )
    try:
        async for _ in runner.run_async(
            user_id="user", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="find A")]),
        ):
            pass
        restored = await service.get_session(app_name="sample", user_id="user", session_id=session.id)
        assert export_state(restored.state)["state"]["counter"] == 5
        assert snapshot["state"]["counter"] == 4
    finally:
        await runner.close()


@pytest.mark.parametrize("response", [None, [], {}, {"partial": True, "content": {"parts": [{"text": "a"}]}}])
async def test_invalid_model_resolver_response_is_rejected(response):
    class Invalid(FixtureResolver):
        async def model(self, request):
            return response

    with pytest.raises(Ineligible):
        await execute(LlmAgent(name="sample", model=ReplayModel(model="offline", resolver=Invalid())))


@pytest.mark.parametrize("result", [None, [], "not an object"])
async def test_invalid_tool_resolver_result_is_rejected(result):
    class Invalid(FixtureResolver):
        async def tool(self, *args):
            return result

    resolver = Invalid()
    with pytest.raises(Ineligible):
        await execute(LlmAgent(name="sample", model=ReplayModel(model="offline", resolver=resolver), tools=[
            ReplayTool(declaration={"name": "lookup"}, resolver=resolver),
        ]))


async def test_streaming_replay_is_explicitly_rejected():
    with pytest.raises(Ineligible, match="streaming"):
        await execute(
            LlmAgent(name="sample", model=ReplayModel(model="offline", resolver=FixtureResolver())),
            config=RunConfig(streaming_mode=StreamingMode.SSE),
        )


async def test_real_repeated_tool_calls_preserve_occurrences():
    class Retry(BaseLlm):
        model: str = "offline"

        async def generate_content_async(self, llm_request, stream=False):
            results = sum(
                bool(part.function_response)
                for content in llm_request.contents for part in content.parts or []
            )
            yield LlmResponse(content=types.Content(
                role="model",
                parts=(
                    [types.Part(function_call=types.FunctionCall(
                        id=f"attempt-{results}", name="lookup", args={"sku": "A"},
                    ))]
                    if results < 2 else [types.Part(text="done")]
                ),
            ))

    def lookup(sku: str) -> dict:
        return {"available": True}

    captured = []
    await execute(
        LlmAgent(name="retry", model=Retry(), tools=[lookup]),
        [CapturePlugin(lambda *event: captured.append(event))],
    )
    tools = [metadata for kind, _, metadata in captured if kind == "tool.requested"]
    assert [entry["function_call_id"] for entry in tools] == ["attempt-0", "attempt-1"]
    assert sum(kind == "model.requested" for kind, _, _ in captured) == 3


async def test_early_stream_close_releases_pending_model():
    class Stream(BaseLlm):
        model: str = "offline"

        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="a")]), partial=True,
            )
            await asyncio.Event().wait()

    plugin = CapturePlugin(lambda *event: None)
    service = InMemorySessionService()
    session = await service.create_session(app_name="sample", user_id="user")
    runner = Runner(
        app=App(name="sample", root_agent=LlmAgent(name="stream", model=Stream()), plugins=[plugin]),
        session_service=service,
    )
    stream = runner.run_async(
        user_id="user", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="stream")]),
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    )
    try:
        await asyncio.wait_for(anext(stream), 5)
        assert plugin._models
    finally:
        await stream.aclose()
        await runner.close()
    assert not plugin._models and not plugin._chunks
