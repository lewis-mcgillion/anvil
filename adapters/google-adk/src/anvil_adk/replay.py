"""Typed substitution boundaries. No boundary holds an original tool/provider."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import PrivateAttr

from .plugin import Ineligible, check_version, json_value, request_body


class Resolver(Protocol):
    """Implement using checked, consumption-aware fixtures or runner IPC.

    Implementations must reject missing/ambiguous requests, never execute tools,
    and match the complete model request, not just prompt text.
    """

    async def model(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def tool(
        self, name: str, declaration: dict[str, Any], arguments: dict[str, Any],
        correlation: dict[str, Any],
    ) -> dict[str, Any]: ...


class ReplayModel(BaseLlm):
    _resolver: Resolver = PrivateAttr()

    def __init__(self, *, model: str, resolver: Resolver):
        check_version()
        super().__init__(model=model)
        self._resolver = resolver

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False,
    ) -> AsyncIterator[LlmResponse]:
        if stream:
            raise Ineligible("streaming model replay is not supported")
        response = json_value(await self._resolver.model(request_body(llm_request)))
        if type(response) is not dict:
            raise Ineligible("model resolver must return an explicit response object")
        parsed = LlmResponse.model_validate(response)
        if parsed.content is None and not parsed.error_code:
            raise Ineligible("model resolver returned neither content nor an error")
        if parsed.partial:
            raise Ineligible("partial model response cannot satisfy non-streaming replay")
        yield parsed


class ReplayTool(BaseTool):
    def __init__(self, *, declaration: dict[str, Any], resolver: Resolver):
        check_version()
        declaration = json_value(declaration)
        self._declaration = types.FunctionDeclaration.model_validate(declaration)
        if not self._declaration.name:
            raise Ineligible("tool declaration must have a name")
        super().__init__(
            name=self._declaration.name, description=self._declaration.description or "",
        )
        self._resolver = resolver

    def _get_declaration(self) -> types.FunctionDeclaration:
        return self._declaration.model_copy(deep=True)

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> dict[str, Any]:
        result = json_value(await self._resolver.tool(
            self.name, self._declaration.model_dump(mode="json", exclude_none=True),
            json_value(args),
            {
                "adk_invocation_id": tool_context.invocation_id,
                "branch": tool_context.branch or "",
                "agent_name": tool_context.agent_name,
                "function_call_id": tool_context.function_call_id,
            },
        ))
        if type(result) is not dict:
            raise Ineligible("tool resolver must return an explicit result object")
        return result
