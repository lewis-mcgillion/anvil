"""Translate supervisor requests into strict recorded fixture lookups."""

import json
from collections import defaultdict

from .replay.fixtures import FixtureSession, ReplayError
from .runner_pb2 import SupervisorMessage
from .runner_policy import IsolationUnavailable


class RecordedToolResolver:
    def __init__(self, fixtures: FixtureSession, ordinals: dict[str, int] | None = None):
        self.fixtures = fixtures
        self.ordinals = ordinals if ordinals is not None else defaultdict(int)

    async def __call__(self, request) -> SupervisorMessage:
        if not request.invocation_id or not request.request_id:
            raise IsolationUnavailable("Recorded tool request lacks causal identity")
        try:
            arguments = json.loads(request.arguments)
            ordinal = self.ordinals[request.invocation_id]
            response = self.fixtures.resolve(
                request_id=request.request_id, kind="tool", path=(request.invocation_id,),
                ordinal=ordinal, request={"tool_name": request.tool_name,
                                          "schema_digest": request.schema_digest,
                                          "arguments": arguments},
            )
            self.ordinals[request.invocation_id] += 1
        except (ReplayError, ValueError, UnicodeError) as error:
            raise IsolationUnavailable("Recorded tool request did not match fixtures") from error
        if response["status"] != "completed":
            raise IsolationUnavailable("Recorded tool fault requires explicit mediation")
        return SupervisorMessage(tool_response={
            "request_id": request.request_id,
            "result_json": json.dumps(response["result"], sort_keys=True, separators=(",", ":")).encode(),
        })


class RecordedModelResolver:
    def __init__(self, fixtures: FixtureSession, ordinals: dict[str, int] | None = None):
        self.fixtures = fixtures
        self.ordinals = ordinals if ordinals is not None else defaultdict(int)

    async def __call__(self, request) -> SupervisorMessage:
        if not request.invocation_id or not request.request_id:
            raise IsolationUnavailable("Recorded model request lacks causal identity")
        try:
            canonical_request = {
                "model": request.model,
                "messages": [{"role": message.role, "content": json.loads(message.content)}
                             for message in request.messages],
                "tools": [{"name": tool.name, "schema_digest": tool.schema_digest}
                          for tool in request.tools],
                "system_instruction": request.system_instruction,
                "generation": {
                    "temperature": request.generation.temperature,
                    "top_p": request.generation.top_p,
                    "max_output_tokens": request.generation.max_output_tokens,
                },
            }
            ordinal = self.ordinals[request.invocation_id]
            response = self.fixtures.resolve(
                request_id=request.request_id, kind="model", path=(request.invocation_id,),
                ordinal=ordinal, request=canonical_request,
            )
            self.ordinals[request.invocation_id] += 1
        except (ReplayError, ValueError, UnicodeError) as error:
            raise IsolationUnavailable("Recorded model request did not match fixtures") from error
        if response["status"] == "completed":
            return SupervisorMessage(model_response={
                "request_id": request.request_id,
                "completion": {"messages": [{"role": "assistant", "content": json.dumps(response["response"]).encode()}],
                               "input_tokens": response["input_tokens"],
                               "output_tokens": response["output_tokens"]},
            })
        if response["status"] == "stream":
            return SupervisorMessage(model_response={
                "request_id": request.request_id,
                "stream": {"chunks": [{"text": str(chunk["chunk"])} for chunk in response["chunks"]],
                           "input_tokens": response["input_tokens"],
                           "output_tokens": response["output_tokens"]},
            })
        raise IsolationUnavailable("Recorded model error requires explicit mediation")


class RecordedRunnerFixtures:
    def __init__(self, fixtures: FixtureSession):
        self.fixtures = fixtures
        ordinals: dict[str, int] = defaultdict(int)
        self.resolve_model = RecordedModelResolver(fixtures, ordinals)
        self.resolve_tool = RecordedToolResolver(fixtures, ordinals)

    async def finalize(self) -> None:
        try:
            self.fixtures.finish()
        except ReplayError as error:
            raise IsolationUnavailable("Runner left recorded fixtures unconsumed") from error
