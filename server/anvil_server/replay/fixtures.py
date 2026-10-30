"""Strict, single-consumption recorded responses; never executes tools or models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from threading import RLock

from ..canonical import CanonicalizationError, canonical_bytes

MAX_FIXTURES = 10_000
MAX_FIXTURE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
FIXTURE_VERSION = "1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ReplayError(ValueError):
    """Redacted, stable failure. Messages contain no recorded request or response."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 1024:
        raise ReplayError("INVALID_FIXTURE", f"Expected nonempty bounded {label}")
    return value


def _path(value: object) -> tuple[str, ...]:
    if type(value) not in {list, tuple} or not value or len(value) > 64:
        raise ReplayError("INVALID_PATH", "Expected an explicit bounded causal branch path")
    return tuple(_text(part, "path component") for part in value)


def _canonical(value: object) -> bytes:
    try:
        data = canonical_bytes(value)
    except (CanonicalizationError, RecursionError) as exc:
        raise ReplayError("INVALID_JSON", "Value has no supported canonical JSON representation") from exc
    if len(data) > MAX_FIXTURE_BYTES:
        raise ReplayError("RESOURCE_LIMIT", "Fixture request or response exceeds byte limit")
    return data


def _request(kind: str, value: object) -> bytes:
    if type(value) is not dict:
        raise ReplayError("INVALID_REQUEST", "Expected a request object")
    if kind == "tool":
        if set(value) != {"tool_name", "schema_digest", "arguments"}:
            raise ReplayError("INVALID_REQUEST", "Expected tool name, schema digest and arguments")
        _text(value["tool_name"], "tool name")
        digest = value["schema_digest"]
        if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
            raise ReplayError("INVALID_REQUEST", "Expected a pinned SHA-256 tool schema digest")
    elif kind == "model":
        if set(value) != {"model", "messages", "tools", "system_instruction", "generation"}:
            raise ReplayError("INVALID_REQUEST", "Expected complete canonical model request")
        _text(value["model"], "model name")
        if (type(value["messages"]) is not list or type(value["tools"]) is not list
                or type(value["system_instruction"]) is not str or type(value["generation"]) is not dict):
            raise ReplayError("INVALID_REQUEST", "Invalid model request fields")
        for message in value["messages"]:
            if (type(message) is not dict or set(message) != {"role", "content"}
                    or type(message["role"]) is not str
                    or message["role"] not in {"system", "user", "assistant", "tool"}):
                raise ReplayError("INVALID_REQUEST", "Invalid canonical model message")
        names = set()
        for tool in value["tools"]:
            if type(tool) is not dict or set(tool) != {"name", "schema_digest"}:
                raise ReplayError("INVALID_REQUEST", "Invalid model tool schema")
            name = _text(tool["name"], "tool name")
            digest = tool["schema_digest"]
            if name in names or type(digest) is not str or _DIGEST.fullmatch(digest) is None:
                raise ReplayError("INVALID_REQUEST", "Duplicate tool name or invalid schema digest")
            names.add(name)
    else:
        raise ReplayError("INVALID_KIND", "Only recorded tool and model fixtures are supported")
    return _canonical(value)


def _usage(value: dict) -> None:
    for key in ("input_tokens", "output_tokens"):
        if type(value[key]) is not int or value[key] < 0:
            raise ReplayError("INVALID_RESPONSE", "Expected nonnegative captured token counts")


def _response(kind: str, value: object) -> bytes:
    if type(value) is not dict:
        raise ReplayError("INVALID_RESPONSE", "Expected a typed recorded response")
    status = value.get("status")
    if status == "error":
        if (set(value) != {"status", "error"} or type(value["error"]) is not dict
                or set(value["error"]) != {"type", "message"}
                or type(value["error"]["message"]) is not str):
            raise ReplayError("INVALID_RESPONSE", "Expected a recorded typed error")
        _text(value["error"]["type"], "error type")
    elif kind == "tool" and status == "completed":
        if set(value) != {"status", "result"}:
            raise ReplayError("INVALID_RESPONSE", "Expected a recorded tool result")
    elif kind == "model" and status == "completed":
        if set(value) != {"status", "response", "input_tokens", "output_tokens"}:
            raise ReplayError("INVALID_RESPONSE", "Expected a recorded model completion and usage")
        _usage(value)
    elif kind == "model" and status == "stream":
        if set(value) != {"status", "chunks", "input_tokens", "output_tokens"}:
            raise ReplayError("INVALID_RESPONSE", "Expected captured model chunks and usage")
        _usage(value)
        if type(value["chunks"]) is not list:
            raise ReplayError("INVALID_RESPONSE", "Expected an ordered chunk array")
        for index, chunk in enumerate(value["chunks"]):
            if (type(chunk) is not dict or set(chunk) != {"chunk_index", "chunk"}
                    or type(chunk["chunk_index"]) is not int or chunk["chunk_index"] != index):
                raise ReplayError("INVALID_RESPONSE", "Captured chunks must be contiguous from zero")
    else:
        raise ReplayError("INVALID_RESPONSE", "Unsupported recorded response status")
    return _canonical(value)


@dataclass(frozen=True)
class Fixture:
    """Immutable bytes plus causal identity and per-branch consumption ordinal."""

    kind: str
    path: tuple[str, ...]
    ordinal: int
    request: bytes
    response: bytes

    @classmethod
    def create(cls, *, kind: str, path: tuple[str, ...], ordinal: int,
               request: dict, response: dict) -> Fixture:
        if type(ordinal) is not int or ordinal < 0:
            raise ReplayError("INVALID_ORDINAL", "Fixture ordinal must be nonnegative")
        return cls(kind, _path(path), ordinal, _request(kind, request), _response(kind, response))


class FixtureSession:
    """Attempt-local finite store with serialized matching and consumption.

    Branches may interleave freely, but calls inside each explicit causal path
    share one ordered queue (including tool and model requests). Match failure
    aborts the session and releases payloads. There is no execution fallback.
    """

    def __init__(self, fixtures: list[Fixture], *, model_mode: str = "recorded",
                 fixture_version: str = FIXTURE_VERSION):
        if fixture_version != FIXTURE_VERSION:
            raise ReplayError("UNSUPPORTED_VERSION", "Unsupported fixture manifest version")
        if model_mode != "recorded":
            raise ReplayError("LIVE_MODE_DISABLED", "This offline session has no provider capability")
        if type(fixtures) is not list or len(fixtures) > MAX_FIXTURES:
            raise ReplayError("RESOURCE_LIMIT", "Expected a bounded fixture list")
        self._lock = RLock()
        self._fixtures: dict[tuple[tuple[str, ...], int], Fixture] = {}
        self._next: dict[tuple[str, ...], int] = {}
        self._requests: set[str] = set()
        self._consumed = 0
        self._terminal: str | None = None
        total_bytes = 0
        manifest = []
        ordinals: dict[tuple[str, ...], list[int]] = {}
        for fixture in fixtures:
            if type(fixture) is not Fixture or type(fixture.request) is not bytes or type(fixture.response) is not bytes:
                raise ReplayError("INVALID_FIXTURE", "Expected immutable fixture values")
            validated = Fixture.create(
                kind=fixture.kind, path=fixture.path, ordinal=fixture.ordinal,
                request=self._decode(fixture.request), response=self._decode(fixture.response),
            )
            if validated != fixture:
                raise ReplayError("INVALID_FIXTURE", "Fixture bytes must be canonical")
            key = fixture.path, fixture.ordinal
            if key in self._fixtures:
                raise ReplayError("AMBIGUOUS_FIXTURE", "Multiple fixtures share a causal path and ordinal")
            self._fixtures[key] = fixture
            self._next[fixture.path] = 0
            ordinals.setdefault(fixture.path, []).append(fixture.ordinal)
            total_bytes += len(fixture.request) + len(fixture.response)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ReplayError("RESOURCE_LIMIT", "Total fixture data exceeds byte limit")
            manifest.append({
                "kind": fixture.kind, "path": list(fixture.path), "ordinal": fixture.ordinal,
                "request_sha256": hashlib.sha256(fixture.request).hexdigest(),
                "response_sha256": hashlib.sha256(fixture.response).hexdigest(),
            })
        for values in ordinals.values():
            if sorted(values) != list(range(len(values))):
                raise ReplayError("FIXTURE_GAP", "Per-branch fixture ordinals must be contiguous from zero")
        manifest.sort(key=lambda item: (item["path"], item["ordinal"]))
        self._manifest_sha256 = hashlib.sha256(canonical_bytes({
            "fixture_version": FIXTURE_VERSION, "model_mode": "recorded", "fixtures": manifest,
        })).hexdigest()

    @staticmethod
    def _decode(data: bytes) -> dict:
        if len(data) > MAX_FIXTURE_BYTES:
            raise ReplayError("RESOURCE_LIMIT", "Fixture exceeds byte limit")
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ReplayError("INVALID_FIXTURE", "Fixture is not valid bounded JSON") from exc

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    def _ensure_active(self) -> None:
        if self._terminal is not None:
            raise ReplayError(self._terminal, "Fixture session is terminal")

    def _terminate(self, code: str) -> None:
        self._fixtures.clear()
        self._next.clear()
        self._requests.clear()
        self._terminal = code

    def resolve(self, *, request_id: str, kind: str, path: tuple[str, ...],
                ordinal: int, request: dict) -> dict:
        with self._lock:
            self._ensure_active()
            try:
                identity = _text(request_id, "request identity")
                branch = _path(path)
                if type(ordinal) is not int or ordinal < 0:
                    raise ReplayError("INVALID_ORDINAL", "Request ordinal must be nonnegative")
                if identity in self._requests:
                    raise ReplayError("REQUEST_REPEATED", "Request identity has already been used")
                expected = self._next.get(branch)
                if expected is None:
                    raise ReplayError("FIXTURE_MISSING", "No fixture for causal branch")
                if ordinal < expected:
                    raise ReplayError("FIXTURE_CONSUMED", "Fixture has already been consumed")
                if ordinal > expected:
                    raise ReplayError("FIXTURE_REORDERED", "Request violates causal branch consumption order")
                fixture = self._fixtures.get((branch, ordinal))
                if fixture is None:
                    raise ReplayError("FIXTURE_MISSING", "No remaining fixture for causal branch")
                if fixture.kind != kind or fixture.request != _request(kind, request):
                    raise ReplayError("FIXTURE_MISMATCH", "Canonical recorded request does not match")
                response = json.loads(fixture.response)
                del self._fixtures[(branch, ordinal)]
                self._requests.add(identity)
                self._next[branch] += 1
                self._consumed += 1
                return response
            except ReplayError:
                self._terminate("SESSION_ABORTED")
                raise

    def finish(self) -> dict:
        with self._lock:
            self._ensure_active()
            if self._fixtures:
                self._terminate("SESSION_ABORTED")
                raise ReplayError("FIXTURES_UNCONSUMED", "Attempt did not consume every recorded fixture")
            evidence = {"fixture_version": FIXTURE_VERSION,
                        "model_mode": "recorded", "fixture_policy": "strict",
                        "fixtures_manifest_sha256": self._manifest_sha256,
                        "consumed": self._consumed, "provider_calls": 0}
            self._terminate("SESSION_FINISHED")
            return evidence

    def cancel(self) -> None:
        with self._lock:
            if self._terminal is None:
                self._terminate("SESSION_CANCELLED")

    def close(self) -> None:
        with self._lock:
            if self._terminal is None:
                self._terminate("SESSION_CLOSED")

    def __enter__(self) -> FixtureSession:
        with self._lock:
            self._ensure_active()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
