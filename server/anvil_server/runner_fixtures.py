"""Translate supervisor tool requests into strict recorded fixture lookups."""

import json
from collections import defaultdict

from .replay.fixtures import FixtureSession, ReplayError
from .runner_pb2 import SupervisorMessage
from .runner_policy import IsolationUnavailable


class RecordedToolResolver:
    def __init__(self, fixtures: FixtureSession):
        self.fixtures = fixtures
        self.ordinals: dict[str, int] = defaultdict(int)

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
