import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Event

import pytest

from anvil_server.canonical import canonical_bytes
from anvil_server.replay import Fixture, FixtureSession, ReplayError


def tool_request(arguments=None):
    return {"tool_name": "inventory.lookup", "schema_digest": "sha256:" + "a" * 64,
            "arguments": {"sku": "ABC"} if arguments is None else arguments}


def model_request():
    return {"model": "recorded", "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "inventory.lookup", "schema_digest": "sha256:" + "a" * 64}],
            "system_instruction": "answer", "generation": {"temperature": 0.5, "max_output_tokens": 40}}


def tool(*, path=("root",), ordinal=0, arguments=None, response=None):
    return Fixture.create(kind="tool", path=path, ordinal=ordinal,
                          request=tool_request(arguments),
                          response={"status": "completed", "result": {"available": True}}
                          if response is None else response)


def model(*, path=("root",), ordinal=0, response=None):
    return Fixture.create(kind="model", path=path, ordinal=ordinal, request=model_request(),
                          response={"status": "completed", "response": "hello",
                                    "input_tokens": 3, "output_tokens": 1}
                          if response is None else response)


def resolve(session, fixture, *, request_id="one", **changes):
    kwargs = {"request_id": request_id, "kind": fixture.kind, "path": fixture.path,
              "ordinal": fixture.ordinal, "request": json.loads(fixture.request)}
    kwargs.update(changes)
    return session.resolve(**kwargs)


def assert_code(code, call):
    with pytest.raises(ReplayError) as error:
        call()
    assert error.value.code == code


def test_strict_tool_canonical_keys_and_single_consumption():
    fixture = tool(arguments={"b": 2, "a": 1})
    session = FixtureSession([fixture])
    assert resolve(session, fixture, request=tool_request({"a": 1, "b": 2})) == {
        "status": "completed", "result": {"available": True},
    }
    evidence = session.finish()
    assert evidence == {
        "fixture_version": "1",
        "model_mode": "recorded", "fixture_policy": "strict",
        "fixtures_manifest_sha256": session.manifest_sha256, "consumed": 1, "provider_calls": 0,
    }
    assert_code("SESSION_FINISHED", lambda: resolve(session, fixture))
    assert not session._fixtures and not session._requests


@pytest.mark.parametrize("changes,code", [
    ({"path": ("missing",)}, "FIXTURE_MISSING"),
    ({"path": ()}, "INVALID_PATH"),
    ({"ordinal": 1}, "FIXTURE_REORDERED"),
    ({"ordinal": True}, "INVALID_ORDINAL"),
    ({"request_id": ""}, "INVALID_FIXTURE"),
    ({"request": tool_request({"sku": "CHANGED_SECRET"})}, "FIXTURE_MISMATCH"),
    ({"request": {**tool_request(), "tool_name": "original.production.tool"}}, "FIXTURE_MISMATCH"),
    ({"request": {**tool_request(), "schema_digest": "sha256:" + "b" * 64}}, "FIXTURE_MISMATCH"),
    ({"kind": "model", "request": model_request()}, "FIXTURE_MISMATCH"),
])
def test_missing_reordered_changed_and_invalid_calls_abort(changes, code):
    fixture = tool()
    session = FixtureSession([fixture])
    with pytest.raises(ReplayError) as error:
        resolve(session, fixture, **changes)
    assert error.value.code == code
    assert "CHANGED_SECRET" not in str(error.value)
    assert "original.production.tool" not in str(error.value)
    assert_code("SESSION_ABORTED", lambda: resolve(session, fixture))
    assert not session._fixtures


def test_repeated_request_id_and_consumed_ordinal_rejected():
    first, second = tool(), tool(ordinal=1)
    session = FixtureSession([first, second])
    resolve(session, first)
    assert_code("REQUEST_REPEATED", lambda: resolve(session, second))
    session = FixtureSession([first, second])
    resolve(session, first)
    assert_code("FIXTURE_CONSUMED", lambda: resolve(session, first, request_id="new-id"))


def test_missing_and_unconsumed_fixtures_never_succeed():
    fixture = tool()
    assert_code("FIXTURE_MISSING", lambda: resolve(FixtureSession([]), fixture))
    session = FixtureSession([fixture])
    assert_code("FIXTURES_UNCONSUMED", session.finish)
    assert_code("SESSION_ABORTED", session.finish)


def test_duplicate_ambiguous_and_noncontiguous_fixtures_rejected():
    assert_code("AMBIGUOUS_FIXTURE", lambda: FixtureSession([tool(), tool()]))
    assert_code("AMBIGUOUS_FIXTURE", lambda: FixtureSession([tool(), model()]))
    assert_code("FIXTURE_GAP", lambda: FixtureSession([tool(ordinal=1)]))
    assert_code("FIXTURE_GAP", lambda: FixtureSession([tool(), tool(ordinal=2)]))


def test_independent_parallel_identical_branches_have_explicit_identity():
    fixtures = [tool(path=("root", str(index))) for index in range(100)]
    session = FixtureSession(fixtures)
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda pair: resolve(session, pair[1], request_id=str(pair[0])),
                                  enumerate(reversed(fixtures))))
    assert all(response["result"] == {"available": True} for response in responses)
    assert session.finish()["consumed"] == 100


def test_racing_same_consumption_only_one_response():
    fixture = tool()
    session = FixtureSession([fixture])

    def consume(index):
        try:
            resolve(session, fixture, request_id=str(index))
            return "resolved"
        except ReplayError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, [1, 2]))
    assert sorted(results) == ["FIXTURE_CONSUMED", "resolved"]
    assert_code("SESSION_ABORTED", session.finish)


def test_tools_and_models_share_branch_order_and_finish_independent_of_ingestion():
    fixtures = [tool(), model(ordinal=1), tool(path=("other",))]
    assert FixtureSession(fixtures).manifest_sha256 == FixtureSession(list(reversed(fixtures))).manifest_sha256
    session = FixtureSession(list(reversed(fixtures)))
    assert_code("FIXTURE_REORDERED", lambda: resolve(session, fixtures[1]))
    with FixtureSession(fixtures) as session:
        resolve(session, fixtures[2], request_id="other")
        resolve(session, fixtures[0], request_id="tool")
        resolve(session, fixtures[1], request_id="model")
        assert session.finish()["consumed"] == 3


@pytest.mark.parametrize("field,value", [
    ("model", "different"),
    ("messages", [{"role": "user", "content": "changed prompt"}]),
    ("tools", []), ("system_instruction", "changed"),
    ("generation", {"temperature": 0.6, "max_output_tokens": 40}),
])
def test_changed_model_requests_fail_exactly(field, value):
    fixture = model()
    request = model_request()
    request[field] = value
    assert_code("FIXTURE_MISMATCH", lambda: resolve(FixtureSession([fixture]), fixture, request=request))


def test_captured_model_stream_and_failure_are_returned_without_interpretation():
    chunks = {"status": "stream", "chunks": [
        {"chunk_index": 0, "chunk": "hel"}, {"chunk_index": 1, "chunk": {"text": "lo"}},
    ], "input_tokens": 3, "output_tokens": 2}
    error = {"status": "error", "error": {"type": "provider.timeout", "message": "recorded"}}
    fixtures = [model(response=chunks), model(ordinal=1, response=error)]
    with FixtureSession(fixtures) as session:
        assert resolve(session, fixtures[0]) == chunks
        assert resolve(session, fixtures[1], request_id="second") == error
        assert session.finish()["provider_calls"] == 0


@pytest.mark.parametrize("response", [
    {"status": "stream", "chunks": [{"chunk_index": 1, "chunk": "gap"}],
     "input_tokens": 0, "output_tokens": 0},
    {"status": "stream", "chunks": [{"chunk_index": 0, "chunk": "a"},
                                  {"chunk_index": 0, "chunk": "duplicate"}],
     "input_tokens": 0, "output_tokens": 0},
    {"status": "stream", "chunks": [{"chunk_index": True, "chunk": "bool"}],
     "input_tokens": 0, "output_tokens": 0},
    {"status": "completed", "response": {}, "input_tokens": -1, "output_tokens": 0},
    {"status": "completed", "response": {}, "input_tokens": True, "output_tokens": 0},
    {"status": "error", "error": {"message": "missing type"}},
    {"status": "unknown"},
])
def test_incomplete_or_invalid_model_responses_rejected(response):
    assert_code("INVALID_RESPONSE", lambda: model(response=response))


@pytest.mark.parametrize("recorded_request", [
    {**tool_request(), "schema_digest": "mutable"},
    {**tool_request(), "extra": "rejected"},
    {**tool_request(), "tool_name": ""},
])
def test_invalid_tool_fixture_requests_rejected(recorded_request):
    with pytest.raises(ReplayError):
        Fixture.create(kind="tool", path=("root",), ordinal=0, request=recorded_request,
                       response={"status": "completed", "result": None})


def test_unknown_kinds_live_mode_and_invalid_paths_fail_closed():
    assert_code("INVALID_KIND", lambda: Fixture.create(
        kind="http", path=("root",), ordinal=0, request={}, response={}))
    for mode in ["live", "unspecified", None]:
        assert_code("LIVE_MODE_DISABLED", lambda: FixtureSession([], model_mode=mode))
    assert_code("UNSUPPORTED_VERSION", lambda: FixtureSession([], fixture_version="2"))
    for path in [(), "", ("",), (1,), ("a",) * 65]:
        assert_code("INVALID_PATH" if type(path) not in {tuple, list} or not path or len(path) > 64
                    else "INVALID_FIXTURE", lambda: tool(path=path))


def test_direct_dataclass_bytes_cannot_bypass_validation():
    fixture = tool()
    assert_code("INVALID_FIXTURE", lambda: FixtureSession([replace(fixture, request=b'{"bad":')]))
    assert_code("INVALID_FIXTURE", lambda: FixtureSession([replace(fixture, request=b" " + fixture.request)]))
    assert_code("INVALID_ORDINAL", lambda: FixtureSession([replace(fixture, ordinal=True)]))
    assert_code("INVALID_FIXTURE", lambda: FixtureSession([replace(fixture, path=["root"])]))


def test_canonical_rejection_aborts_session():
    fixture = tool()
    session = FixtureSession([fixture])
    assert_code("INVALID_JSON", lambda: resolve(session, fixture, request=tool_request(float("nan"))))
    assert_code("SESSION_ABORTED", session.finish)
    assert_code("INVALID_JSON", lambda: tool(arguments=2**53))
    assert_code("INVALID_JSON", lambda: tool(arguments="\ud800"))


def test_float_number_normalization_and_unicode_key_order():
    fixture = tool(arguments={"\ue000": 1.0, "\U0001f600": 1e-7})
    with FixtureSession([fixture]) as session:
        resolve(session, fixture, request=tool_request({"\U0001f600": 0.0000001, "\ue000": 1}))
        session.finish()


def test_original_and_resolved_payload_mutation_do_not_change_store():
    request = tool_request()
    response = {"status": "completed", "result": {"list": [1]}}
    fixture = Fixture.create(kind="tool", path=("root",), ordinal=0, request=request, response=response)
    request["arguments"]["sku"] = "mutated"
    response["result"]["list"].append(2)
    with FixtureSession([fixture]) as session:
        result = resolve(session, fixture)
        assert result["result"]["list"] == [1]
        result["result"]["list"].append(3)
        session.finish()
    with FixtureSession([fixture]) as session:
        assert resolve(session, fixture)["result"]["list"] == [1]


@pytest.mark.parametrize("terminal", ["cancel", "close"])
def test_terminal_cleanup_releases_payloads_and_rejects_reuse(terminal):
    fixture = tool()
    session = FixtureSession([fixture])
    getattr(session, terminal)()
    getattr(session, terminal)()
    assert not session._fixtures and not session._next and not session._requests
    code = "SESSION_CANCELLED" if terminal == "cancel" else "SESSION_CLOSED"
    assert_code(code, lambda: resolve(session, fixture))


def test_cancellation_serializes_with_an_inflight_resolution(monkeypatch):
    import anvil_server.replay.fixtures as module
    fixture = tool()
    session = FixtureSession([fixture, tool(ordinal=1)])
    entered, release, cancelling = Event(), Event(), Event()
    original = module._request

    def blocked_request(kind, value):
        entered.set()
        assert release.wait(5)
        return original(kind, value)

    def cancel():
        cancelling.set()
        session.cancel()

    monkeypatch.setattr(module, "_request", blocked_request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolution = pool.submit(resolve, session, fixture)
        assert entered.wait(5)
        cancellation = pool.submit(cancel)
        assert cancelling.wait(5)
        release.set()
        assert resolution.result(timeout=5)["status"] == "completed"
        cancellation.result(timeout=5)
    assert not session._fixtures
    assert_code("SESSION_CANCELLED", session.finish)


def test_context_exception_closes_session():
    session = FixtureSession([tool()])
    with pytest.raises(RuntimeError):
        with session:
            raise RuntimeError("caller failed")
    assert_code("SESSION_CLOSED", session.finish)


def test_limits_are_explicit(monkeypatch):
    import anvil_server.replay.fixtures as module
    fixture = tool()
    monkeypatch.setattr(module, "MAX_FIXTURES", 0)
    assert_code("RESOURCE_LIMIT", lambda: FixtureSession([fixture]))
    monkeypatch.setattr(module, "MAX_FIXTURES", 100)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 1)
    assert_code("RESOURCE_LIMIT", lambda: FixtureSession([fixture]))
    monkeypatch.setattr(module, "MAX_FIXTURE_BYTES", 1)
    assert_code("RESOURCE_LIMIT", lambda: tool())


def test_no_provider_tool_files_network_or_subprocess_capability(monkeypatch):
    import builtins
    fixture = model()
    other = tool()

    def forbidden(*args, **kwargs):
        pytest.fail("Offline fixtures attempted I/O or execution")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    with FixtureSession([fixture, replace(other, path=("other",))]) as session:
        resolve(session, fixture)
        resolve(session, replace(other, path=("other",)), request_id="tool")
        assert session.finish()["provider_calls"] == 0


def test_replay_package_imports_no_runner_or_provider():
    allowed = {"__future__", "hashlib", "json", "re", "dataclasses", "threading"}
    root = Path(__file__).resolve().parents[1] / "anvil_server/replay"
    for source in root.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module in allowed


def test_manifest_pins_response_and_branch():
    fixture = tool()
    digest = FixtureSession([fixture]).manifest_sha256
    changed = tool(response={"status": "completed", "result": None})
    assert FixtureSession([changed]).manifest_sha256 != digest
    assert FixtureSession([replace(fixture, path=("different",))]).manifest_sha256 != digest
    assert len(digest) == 64
    assert fixture.request == canonical_bytes(tool_request())
