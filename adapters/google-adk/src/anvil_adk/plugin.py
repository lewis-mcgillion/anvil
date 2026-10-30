"""Passive ADK interception; the caller owns redaction, envelopes and transport."""

from collections.abc import Callable
import json
import math
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from . import SUPPORTED_ADK, Ineligible, check_version

Sink = Callable[[str, dict[str, Any], dict[str, Any]], None]


def json_value(value: Any) -> Any:
    """Copy JSON primitives without coercing Python objects or executing hooks."""
    def validate(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise Ineligible(f"non-JSON state or payload: {type(item).__name__}")

    try:
        validate(value)
        return json.loads(json.dumps(value, allow_nan=False))
    except RecursionError as exc:
        raise Ineligible("cyclic or excessively nested JSON") from exc


def export_state(state: dict[str, Any]) -> dict[str, Any]:
    if type(state) is not dict:
        raise Ineligible("state must be an explicit JSON object")
    return {"schema_version": "1", "adapter": "google-adk", "state": json_value(state)}


def import_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot = json_value(snapshot)
    if (
        type(snapshot) is not dict
        or set(snapshot) != {"schema_version", "adapter", "state"}
        or snapshot["schema_version"] != "1"
        or snapshot["adapter"] != "google-adk"
        or type(snapshot["state"]) is not dict
    ):
        raise Ineligible("missing state or unsupported state snapshot")
    return snapshot["state"]


def request_body(request: LlmRequest) -> dict[str, Any]:
    # tools_dict contains executable objects; only their model-visible declarations
    # in config belong in a recording.
    return json_value(request.model_dump(mode="json", exclude={"tools_dict"}, exclude_none=True))


class CapturePlugin(BasePlugin):
    """Emit v1 payloads without replacing any framework result.

    Install last, with no agent callbacks that mutate requests/responses. A sink
    must redact before retention and build schema-valid event envelopes.
    Exceptions from serialization or the sink are deliberately not suppressed.
    """

    def __init__(self, sink: Sink):
        check_version()
        super().__init__(name="anvil_capture")
        self._sink = sink
        self._models: dict[tuple[str, str, str], str] = {}
        self._chunks: dict[tuple[str, str, str], int] = {}

    @staticmethod
    def _key(context: CallbackContext) -> tuple[str, str, str]:
        return context.invocation_id, context.branch or "", context.agent_name

    def _emit(
        self, kind: str, payload: dict[str, Any], context: CallbackContext,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        correlation = {
            "adk_version": SUPPORTED_ADK,
            "adk_invocation_id": context.invocation_id,
            "branch": context.branch or "",
            "agent_name": context.agent_name,
            "function_call_id": context.function_call_id,
            **(metadata or {}),
        }
        # Hook-only capture cannot establish that callbacks/toolsets had no hidden
        # effects, so it must not claim replay eligibility.
        self._sink(kind, json_value({**payload, "redactions": [], "replay_safe": False}), correlation)

    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content,
    ) -> None:
        self._emit("message.created", {
            "role": "user", "content": user_message.model_dump(mode="json", exclude_none=True),
        }, CallbackContext(invocation_context))

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest,
    ) -> None:
        key = self._key(callback_context)
        if key in self._models:
            raise Ineligible("overlapping model calls have an ambiguous causal identity")
        self._models[key] = llm_request.model or "unknown"
        self._chunks[key] = 0
        self._emit("model.requested", {
            "model": self._models[key], "request": request_body(llm_request),
        }, callback_context)

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse,
    ) -> None:
        key = self._key(callback_context)
        response = llm_response.model_dump(mode="json", exclude_none=True)
        if llm_response.partial:
            index = self._chunks[key]
            self._chunks[key] = index + 1
            self._emit("model.chunk", {"chunk_index": index, "chunk": response}, callback_context)
        else:
            self._emit("model.completed", {
                "model": self._models[key], "response": response,
            }, callback_context)
            del self._models[key]
            del self._chunks[key]

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception,
    ) -> None:
        self._emit("model.failed", {
            "model": llm_request.model or "unknown",
            "error": {"type": type(error).__name__, "message": str(error)},
        }, callback_context)
        self._clear_call(self._key(callback_context))

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
    ) -> None:
        self._emit("tool.requested", {"tool_name": tool.name, "arguments": tool_args}, tool_context)

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
        result: dict[str, Any],
    ) -> None:
        self._emit("tool.completed", {
            "tool_name": tool.name, "arguments": tool_args, "result": result,
        }, tool_context)

    async def on_tool_error_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
        error: BaseException,
    ) -> None:
        self._emit("tool.failed", {
            "tool_name": tool.name, "arguments": tool_args,
            "error": {"type": type(error).__name__, "message": str(error)},
        }, tool_context)

    async def on_event_callback(self, *, invocation_context: InvocationContext, event: Event) -> None:
        if event.content:
            context = CallbackContext(invocation_context)
            self._emit("message.created", {
                "role": "assistant" if event.content.role == "model" else "tool",
                "content": event.model_dump(mode="json", exclude_none=True),
            }, context, {
                "branch": event.branch or "",
                "agent_name": event.author,
                "adk_event_id": event.id,
            })

    def _clear(self, invocation_id: str) -> None:
        for key in list(self._models):
            if key[0] == invocation_id:
                self._clear_call(key)

    def _clear_call(self, key: tuple[str, str, str]) -> None:
        self._models.pop(key, None)
        self._chunks.pop(key, None)

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        self._clear(invocation_context.invocation_id)

    async def on_run_error_callback(
        self, *, invocation_context: InvocationContext, error: Exception,
    ) -> None:
        self._clear(invocation_context.invocation_id)

    async def close(self) -> None:
        self._models.clear()
        self._chunks.clear()
