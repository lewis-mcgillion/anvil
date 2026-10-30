import copy
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import urllib.request

import pytest
import rfc8785
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from anvil_server.suites import SuiteError, compile_suite, parse_suite, suite_schema
from anvil_server.suites import compiler, parser

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "suites"


@pytest.fixture
def document():
    return yaml.safe_load((FIXTURES / "baseline.yaml").read_text())


def encode(document):
    return yaml.safe_dump(document, sort_keys=False)


def test_baseline_and_structural_schema(document):
    suite = parse_suite(encode(document))
    Draft202012Validator(suite_schema()).validate(document)
    assert suite.cases[0].model_mode == "recorded"
    assert suite.cases[0].budgets.max_wall_time_seconds == 120
    assert suite.cases[0].normalizer_version == "1"
    assert suite.model_config["extra"] == "forbid"


@pytest.mark.parametrize("filename", sorted(path.name for path in FIXTURES.glob("*.invalid.yaml")))
def test_malicious_fixture_corpus(filename):
    with pytest.raises(SuiteError):
        parse_suite((FIXTURES / filename).read_bytes())


@pytest.mark.parametrize("source", [
    "", "---\n", "[]", "true", "{", "a: 1\na: 2", "? [a, b]\n: c",
    "1: a", "true: a", "a: !!str hello", "a: !include /etc/passwd",
    "a: &a [*a]", "a: &a [x]\nb: {<<: *a}", "---\na: b\n---\nc: d",
    "name: 2026-10-30", "name: !!binary SGVsbG8=", "name: .nan",
    "name: .inf", "name: -.inf", "name: 9007199254740992",
    "name: " + "9" * 400, b"\xff\xfe", 'name: "\\uD800"',
    'name: "${SECRET}"', 'name: "{{agent()}}"', 'name: "$(id)"',
    'name: "`id`"', "a: !!python/object/apply:os.system ['touch should-not-exist']",
])
def test_malformed_and_unsafe_yaml(source):
    with pytest.raises(SuiteError):
        parse_suite(source)


@pytest.mark.parametrize("field,value", [
    ("apiVersion", "anvil.dev/v2"), ("kind", "Job"), ("unknown", "bad"),
    ("cases", []), ("name", 1),
])
def test_root_contract_rejections(document, field, value):
    document[field] = value
    with pytest.raises(SuiteError):
        parse_suite(encode(document))


@pytest.mark.parametrize("path,value", [
    (("recording", "file"), "../secret"),
    (("recording", "file"), "sub/../../secret"),
    (("recording", "file"), "/etc/passwd"),
    (("recording", "file"), "https://example.com/recording"),
    (("recording", "file"), "file:///etc/passwd"),
    (("recording", "file"), r"C:\secret"),
    (("recording", "file"), r"sub\secret"),
    (("recording", "file"), "sub//secret"),
    (("recording", "file"), "./secret"),
    (("recording", "file"), "sub/./secret"),
    (("recording", "file"), "sub/\x00secret"),
    (("recording", "file"), "sub/\nsecret"),
    (("recording", "file"), ""),
    (("recording", "sha256"), "A" * 64),
    (("recording", "sha256"), "a" * 63),
    (("recording", "extra"), True),
    (("agent", "image"), "example/agent:latest"),
    (("agent", "image"), "example/agent:1.0.0"),
    (("agent", "image"), "https://example/agent@sha256:" + "a" * 64),
    (("agent", "image"), "../agent@sha256:" + "a" * 64),
    (("agent", "image"), "example/agent@sha256:" + "A" * 64),
    (("agent", "entrypoint"), "python -c 'bad'"),
    (("agent", "adapter_version"), "latest"),
    (("agent", "adapter_version"), ">=1.0.0"),
    (("agent", "unknown"), True),
    (("model_mode",), "live"), (("tool_policy",), "fallback"),
    (("seed",), True), (("seed",), -1), (("seed",), "1"),
    (("normalizer_version",), "latest"), (("parameter_version",), "2"),
    (("budgets",), {"max_tool_calls": -1}),
    (("budgets",), {"max_wall_time_seconds": 0}),
    (("budgets",), {"max_wall_time_seconds": True}),
    (("budgets",), {"extra": 1}),
    (("assertions",), []),
    (("initial_state",), {"nested": "${run()}" }),
    (("initial_state",), {"nested": {"a": float("inf")}}),
])
def test_case_contract_rejections(document, path, value):
    target = document["cases"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(SuiteError):
        parse_suite(encode(document))


def test_duplicate_nested_keys_and_case_ids(document):
    source = encode(document).replace("seed: 0", "seed: 0\n  seed: 1")
    with pytest.raises(SuiteError):
        parse_suite(source)
    document["cases"].append(copy.deepcopy(document["cases"][0]))
    with pytest.raises(SuiteError, match="Duplicate case"):
        parse_suite(encode(document))


def test_resource_limits(document, monkeypatch):
    with pytest.raises(SuiteError, match="byte limit"):
        parse_suite(" " * (parser.MAX_SUITE_BYTES + 1))
    with pytest.raises(SuiteError, match="byte limit"):
        parse_suite(b" " * (parser.MAX_SUITE_BYTES + 1))
    with pytest.raises(SuiteError, match="nesting"):
        parse_suite("name: " + "[" * 33 + "0" + "]" * 33)
    monkeypatch.setattr(parser, "MAX_NODES", 10)
    with pytest.raises(SuiteError, match="node limit"):
        parse_suite(encode(document))


def test_bounded_random_malformed_input():
    randomizer = random.Random(66)
    for _ in range(250):
        source = bytes(randomizer.randrange(256) for _ in range(randomizer.randrange(1, 200)))
        with pytest.raises(SuiteError):
            parse_suite(source)
    alphabet = "abc:[]{}&*!#%>|-'\"0123 \n\t"
    for _ in range(250):
        source = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(1, 200)))
        with pytest.raises(SuiteError):
            parse_suite(source)


def test_no_execution_or_remote_resolution(document, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Suite foundations must not execute commands or access the network")

    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert compile_suite(encode(document), FIXTURES).sha256
    with pytest.raises(SuiteError):
        parse_suite((FIXTURES / "tags.invalid.yaml").read_bytes())
    document["cases"][0]["assertions"] = [{
        "kind": "output.schema",
        "expected": {"$ref": "https://evil.invalid/schema"},
    }]
    with pytest.raises(SuiteError):
        parse_suite(encode(document))


def test_compile_deterministic_canonical_snapshot(document, tmp_path):
    (tmp_path / "recording.json").write_bytes((FIXTURES / "recording.json").read_bytes())
    first = compile_suite(encode(document), tmp_path)
    second = compile_suite(json.dumps(document, sort_keys=True), tmp_path)
    assert first == second
    assert first.sha256 == hashlib.sha256(first.canonical).hexdigest()
    assert first.canonical == rfc8785.dumps(json.loads(first.canonical))
    document["cases"][0]["budgets"] = {"max_wall_time_seconds": 120}
    assert compile_suite(encode(document), tmp_path) == first
    (tmp_path / "recording.json").write_bytes(b"mutated")
    assert first.references[0].content == (FIXTURES / "recording.json").read_bytes()
    with pytest.raises(SuiteError, match="SHA-256 mismatch"):
        compile_suite(encode(document), tmp_path)
    with pytest.raises(AttributeError):
        first.sha256 = "mutate"


@pytest.mark.parametrize("path,value", [
    (("seed",), 42),
    (("agent", "image"), "example/agent@sha256:" + "b" * 64),
    (("agent", "adapter_version"), "1.0.1"),
    (("initial_state",), {"key": "value"}),
    (("budgets",), {"max_tool_calls": 5}),
    (("assertions",), [{"kind": "outcome", "expected": "failed"}]),
])
def test_locked_configuration_changes_hash(document, path, value):
    before = compile_suite(encode(document), FIXTURES).sha256
    target = document["cases"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert compile_suite(encode(document), FIXTURES).sha256 != before


def test_shared_references_conflicts_and_fault_snapshot(document, tmp_path, monkeypatch):
    content = b'{"fault_plan_version":"1","faults":[]}'
    (tmp_path / "faults.json").write_bytes(content)
    (tmp_path / "recording.json").write_bytes((FIXTURES / "recording.json").read_bytes())
    document["cases"][0]["fault_plan"] = {
        "file": "faults.json", "sha256": hashlib.sha256(content).hexdigest(),
    }
    other = copy.deepcopy(document["cases"][0])
    other["id"] = "other"
    document["cases"].append(other)
    read = compiler._read_local
    reads = []

    def counting_read(root, name, limit):
        reads.append(name)
        return read(root, name, limit)

    monkeypatch.setattr(compiler, "_read_local", counting_read)
    locked = compile_suite(encode(document), tmp_path)
    assert reads == ["recording.json", "faults.json"]
    assert [ref.file for ref in locked.references] == ["faults.json", "recording.json"]
    other["recording"]["sha256"] = "0" * 64
    with pytest.raises(SuiteError, match="Conflicting checksums"):
        compile_suite(encode(document), tmp_path)


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "fifo", "parent-symlink"])
def test_non_regular_references_reject(document, tmp_path, kind):
    name = "recording.json"
    if kind == "symlink":
        (tmp_path / name).symlink_to(FIXTURES / name)
    elif kind == "directory":
        (tmp_path / name).mkdir()
    elif kind == "fifo":
        os.mkfifo(tmp_path / name)
    elif kind == "parent-symlink":
        (tmp_path / "linked").symlink_to(FIXTURES, target_is_directory=True)
        document["cases"][0]["recording"]["file"] = "linked/recording.json"
    with pytest.raises(SuiteError):
        compile_suite(encode(document), tmp_path)


def test_root_symlink_and_nested_success(document, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "recording.json").write_bytes((FIXTURES / "recording.json").read_bytes())
    document["cases"][0]["recording"]["file"] = "nested/recording.json"
    assert compile_suite(encode(document), root).references
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(SuiteError):
        compile_suite(encode(document), link)


def test_reference_and_total_limits(document, monkeypatch):
    monkeypatch.setattr(compiler, "MAX_REFERENCE_BYTES", 1)
    with pytest.raises(SuiteError, match="byte limit"):
        compile_suite(encode(document), FIXTURES)
    monkeypatch.setattr(compiler, "MAX_REFERENCE_BYTES", 1024)
    monkeypatch.setattr(compiler, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(SuiteError, match="byte limit"):
        compile_suite(encode(document), FIXTURES)


def test_limit_boundary_and_total_of_distinct_references(document, tmp_path, monkeypatch):
    content = (FIXTURES / "recording.json").read_bytes()
    (tmp_path / "recording.json").write_bytes(content)
    monkeypatch.setattr(compiler, "MAX_TOTAL_BYTES", len(content))
    monkeypatch.setattr(compiler, "MAX_REFERENCE_BYTES", len(content))
    assert compile_suite(encode(document), tmp_path).references[0].content == content
    (tmp_path / "faults.json").write_bytes(b"{}")
    document["cases"][0]["fault_plan"] = {
        "file": "faults.json", "sha256": hashlib.sha256(b"{}").hexdigest(),
    }
    with pytest.raises(SuiteError, match="byte limit"):
        compile_suite(encode(document), tmp_path)


def test_resource_cleanup_on_interruption(document, monkeypatch):
    before = len(list(Path("/proc/self/fd").iterdir()))

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(compiler.os, "fdopen", interrupt)
    with pytest.raises(KeyboardInterrupt):
        compile_suite(encode(document), FIXTURES)
    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_descriptors_close_after_rejection(document):
    before = len(list(Path("/proc/self/fd").iterdir()))
    document["cases"][0]["recording"]["sha256"] = "0" * 64
    for _ in range(20):
        with pytest.raises(SuiteError):
            compile_suite(encode(document), FIXTURES)
    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_parse_models_not_frozen_containers_but_compilation_is(document):
    suite = parse_suite(encode(document))
    with pytest.raises(ValidationError):
        suite.name = "other"
    first = compile_suite(encode(document), FIXTURES)
    suite.cases[0].initial_state["extra"] = "later"
    assert "extra" not in json.loads(first.canonical)["suite"]["cases"][0]["initial_state"]
