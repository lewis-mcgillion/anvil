"""Offline E08 contract, negative, permutation and gate truth-table checks."""

import copy
import itertools
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from anvil_schema import ValidationError

from anvil_server.canonical import CanonicalizationError, canonical_bytes
from anvil_server.divergence import NormalizationPolicy, Recording, aggregate, compare


ROOT = Path(__file__).resolve().parents[2]


def uid(number):
    return f"00000000-0000-7000-8000-{number:012x}"


def event(number, kind, payload, parents=(), invocation=90):
    return {
        "schema_version": "1", "event_id": uid(number), "run_id": uid(100),
        "stream_id": uid(101), "sequence": number - 1, "type": kind,
        "occurred_at": "2026-01-01T00:00:00.000Z",
        "trace_id": "0" * 32, "span_id": "0" * 16,
        "parent_event_ids": [uid(parent) for parent in parents],
        "invocation_id": uid(invocation),
        "payload": {**payload, "redactions": [], "replay_safe": True},
    }


def recording():
    return Recording((
        event(1, "run.started", {"agent_name": "agent", "adapter": "test", "adapter_version": "1", "input": {"q": "hi"}}),
        event(2, "model.requested", {"model": "stub", "request": {"messages": ["hi"]}}, (1,), 91),
        event(3, "model.completed", {"model": "stub", "response": "call lookup", "usage": {"input_tokens": 2, "output_tokens": 3}}, (2,), 91),
        event(4, "tool.requested", {"tool_name": "lookup", "arguments": {"q": "hi"}}, (3,), 92),
        event(5, "tool.completed", {"tool_name": "lookup", "arguments": {"q": "hi"}, "result": {"answer": 1}, "duration_ms": 5}, (4,), 92),
        event(6, "run.completed", {"outcome": "succeeded", "output": "done"}, (5,)),
    ), "sealed_complete")


def mutate(source, index, field, value):
    events = copy.deepcopy(source.events)
    events[index]["payload"][field] = value
    return Recording(events, source.capture_completeness, source.logical_branches)


def reidentify(source, offset=1000):
    events = copy.deepcopy(source.events)
    identifiers = {item["event_id"]: uid(offset + i) for i, item in enumerate(events)}
    for item in events:
        item["event_id"] = identifiers[item["event_id"]]
        item["parent_event_ids"] = [identifiers[parent] for parent in item["parent_event_ids"]]
        item["run_id"] = uid(offset + 100)
        item["stream_id"] = uid(offset + 101)
        item["invocation_id"] = uid(int(item["invocation_id"][-12:], 16) + offset)
        item["occurred_at"] = "2026-10-30T12:34:56.789Z"
        item["trace_id"] = "1" * 32
        item["span_id"] = "1" * 16
    branches = {
        uid(int(invocation[-12:], 16) + offset): path
        for invocation, path in source.logical_branches.items()
    }
    return Recording(events, source.capture_completeness, branches)


def parallel(*, branches=True):
    events = (
        event(1, "run.started", {"agent_name": "a", "adapter": "test", "adapter_version": "1"}),
        event(2, "tool.requested", {"tool_name": "lookup", "arguments": {"q": "a"}}, (1,), 91),
        event(3, "tool.requested", {"tool_name": "lookup", "arguments": {"q": "b"}}, (1,), 92),
        event(4, "tool.completed", {"tool_name": "lookup", "arguments": {"q": "a"}, "result": 1}, (2,), 91),
        event(5, "tool.completed", {"tool_name": "lookup", "arguments": {"q": "b"}, "result": 2}, (3,), 92),
        event(6, "run.completed", {"outcome": "succeeded", "output": [1, 2]}, (4, 5)),
    )
    return Recording(events, "sealed_complete", {uid(91): ("a",), uid(92): ("b",)} if branches else {})


def test_identical_and_reidentified_recordings_pass_without_mutation():
    source = recording()
    original = copy.deepcopy(source)
    report = compare(source, reidentify(source))
    assert report["gate"] == {"status": "pass", "exit_code": 0}
    assert report["alignment_confidence"] == "exact"
    assert report["findings"] == []
    assert report["recording_sha256"]["baseline"] != report["recording_sha256"]["candidate"]
    assert source == original


@pytest.mark.parametrize("index,field,value,category", [
    (0, "input", {"q": "changed"}, "input"),
    (1, "request", {"messages": ["changed"]}, "model_request"),
    (1, "model", "different", "model_request"),
    (3, "tool_name", "changed", "tool_identity"),
    (3, "arguments", {"q": "changed"}, "tool_arguments"),
    (3, "arguments_schema_digest", "1" * 64, "tool_identity"),
    (4, "result", {"answer": 2}, "tool_result"),
    (4, "duration_ms", 6, "budget"),
    (2, "usage", {"input_tokens": 3, "output_tokens": 3}, "budget"),
    (2, "response", "different", "output"),
    (5, "output", "different", "output"),
])
def test_single_mutations_have_category_and_event_pointer_evidence(index, field, value, category):
    source = recording()
    report = compare(source, mutate(source, index, field, value))
    assert report["deterministic"] == "fail"
    assert report["gate"]["exit_code"] == 1
    assert report["alignment_confidence"] == "exact"
    finding = report["findings"][0]
    assert finding["category"] == category
    assert finding["path"].startswith(f"/payload/{field}")
    assert finding["baseline_event_id"] == uid(index + 1)
    assert finding["candidate_event_id"] == uid(index + 1)
    assert finding["baseline_sha256"] != finding["candidate_sha256"]


def test_changed_error_is_aligned_to_original_terminal():
    source = recording()
    events = copy.deepcopy(source.events)
    events[4]["type"] = "tool.failed"
    del events[4]["payload"]["result"]
    del events[4]["payload"]["duration_ms"]
    events[4]["payload"]["error"] = {"type": "timeout", "message": "fixture timed out"}
    report = compare(source, Recording(events, "sealed_complete"))
    assert report["alignment_confidence"] == "exact"
    assert report["deterministic"] == "fail"
    assert any(finding["path"] == "/type" and finding["category"] == "error" for finding in report["findings"])


def test_missing_and_null_are_not_equal():
    source = recording()
    events = copy.deepcopy(source.events)
    del events[0]["payload"]["input"]
    report = compare(Recording(events, "sealed_complete"), mutate(source, 0, "input", None))
    finding = report["findings"][0]
    assert finding["baseline_present"] is False
    assert finding["candidate_present"] is True
    assert finding["candidate_sha256"] is not None
    assert report["deterministic"] == "fail"


def test_complete_missing_branch_is_partial_and_fails():
    source = parallel()
    events = copy.deepcopy(source.events)
    retained = [events[0], events[1], events[3], events[5]]
    retained[-1]["parent_event_ids"] = [uid(4)]
    for sequence, item in enumerate(retained):
        item["sequence"] = sequence
    candidate = Recording(tuple(retained), "sealed_complete", {uid(91): ("a",)})
    report = compare(source, candidate)
    assert report["alignment_confidence"] == "partial"
    assert report["deterministic"] == "fail"
    assert report["gate"]["exit_code"] == 1
    assert any(finding["category"] == "missing_event" for finding in report["findings"])


def test_parallel_reordering_is_not_alignment_by_sequence_or_timestamp():
    source = parallel()
    events = copy.deepcopy(source.events)
    events[1]["sequence"], events[2]["sequence"] = events[2]["sequence"], events[1]["sequence"]
    events[3]["sequence"], events[4]["sequence"] = events[4]["sequence"], events[3]["sequence"]
    assert compare(source, reidentify(Recording(tuple(reversed(events)), "sealed_complete", source.logical_branches)))["gate"]["exit_code"] == 0


def test_duplicate_parallel_branches_are_ambiguous_even_with_distinct_arguments():
    source = parallel(branches=False)
    report = compare(source, source)
    assert report["alignment_confidence"] == "ambiguous"
    assert report["deterministic"] == "inconclusive"
    assert report["gate"]["exit_code"] == 5
    assert report["alignment"]["ambiguous_baseline"]


def test_changed_logical_branch_is_partial_not_guessed_by_payload():
    source = parallel()
    report = compare(source, Recording(source.events, "sealed_complete", {uid(91): ("renamed",), uid(92): ("b",)}))
    assert report["alignment_confidence"] == "partial"
    assert report["deterministic"] == "fail"


@pytest.mark.parametrize("completeness", ["open", "sealing", "sealed_incomplete"])
def test_incomplete_capture_never_passes(completeness):
    source = recording()
    report = compare(source, Recording(source.events, completeness), judge="pass", judge_required=True)
    assert report["deterministic"] == "inconclusive"
    assert report["gate"]["exit_code"] == 5


def test_empty_recordings_are_inconclusive():
    report = compare(Recording((), "sealed_complete"), Recording((), "sealed_complete"))
    assert report["gate"]["exit_code"] == 5


@pytest.mark.parametrize("damage", ["parent", "sequence", "terminal", "redaction", "orphan_response", "disconnected"])
def test_missing_evidence_cannot_be_hidden_by_equality(damage):
    source = recording()
    events = copy.deepcopy(source.events)
    if damage == "parent":
        events[1]["parent_event_ids"] = [uid(999)]
    elif damage == "sequence":
        events[-1]["sequence"] += 1
    elif damage == "terminal":
        events = events[:-1]
    elif damage == "redaction":
        events[0]["payload"]["redactions"] = ["/input"]
    elif damage == "orphan_response":
        events[4]["parent_event_ids"] = [uid(3)]
    else:
        events[3]["parent_event_ids"] = []
    damaged = Recording(tuple(events), "sealed_complete")
    report = compare(damaged, damaged)
    assert report["deterministic"] == "inconclusive"
    assert report["evidence_issues"]["baseline"]


@pytest.mark.parametrize("damage", ["cycle", "duplicate_id", "duplicate_sequence", "mixed_runs", "unknown_field", "bad_type"])
def test_invalid_recordings_raise_instead_of_success_shaped_fallback(damage):
    events = copy.deepcopy(recording().events)
    if damage == "cycle":
        events[0]["parent_event_ids"] = [uid(6)]
    elif damage == "duplicate_id":
        events[1]["event_id"] = uid(1)
    elif damage == "duplicate_sequence":
        events[1]["sequence"] = 0
    elif damage == "mixed_runs":
        events[1]["run_id"] = uid(999)
    elif damage == "unknown_field":
        events[1]["payload"]["hidden"] = "must not be stripped"
    else:
        events[1]["type"] = "production.execute"
    with pytest.raises((ValueError, ValidationError)):
        compare(recording(), Recording(events, "sealed_complete"))


@pytest.mark.parametrize("branches", [{uid(999): ("bad",)}, {uid(91): ()}, {uid(91): ["mutable"]}])
def test_invalid_branch_sidecars_are_rejected(branches):
    source = recording()
    with pytest.raises(ValueError):
        compare(source, Recording(source.events, "sealed_complete", branches))


def test_explicit_timing_and_text_policy_is_reported_and_payloads_remain_unchanged():
    source = recording()
    changed = mutate(mutate(source, 4, "duration_ms", 999), 5, "output", "  DONE\n")
    assert compare(source, changed)["deterministic"] == "fail"
    policy = NormalizationPolicy(ignore_pointers=("/duration_ms",), text_pointers=("/output",),
                                 normalize_whitespace=True, casefold_strings=True)
    report = compare(source, changed, policy=policy)
    assert report["gate"]["exit_code"] == 0
    assert report["normalization"]["ignore_pointers"] == ["/duration_ms"]
    assert len(report["normalization_evidence"]) == 4
    assert changed.events[4]["payload"]["duration_ms"] == 999
    assert changed.events[5]["payload"]["output"] == "  DONE\n"
    assert report["normalization_evidence"][0]["original_sha256"]


def test_redaction_cannot_be_normalized_away_to_pass():
    source = recording()
    damaged = mutate(source, 0, "redactions", ["/input"])
    report = compare(damaged, damaged, policy=NormalizationPolicy(ignore_pointers=("/redactions",)))
    assert report["gate"]["exit_code"] == 5


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, "\ud800", {1: "not a string key"}])
def test_ignored_values_are_validated_before_they_can_be_removed(value):
    with pytest.raises(CanonicalizationError):
        NormalizationPolicy(ignore_pointers=("/ignored",)).apply({"ignored": value})


@pytest.mark.parametrize("kwargs", [
    {"version": "2"}, {"ignore_pointers": ("",)}, {"ignore_pointers": ("/bad~2",)},
    {"ignore_pointers": ("/a", "/a/b")}, {"ignore_pointers": ("/a",), "text_pointers": ("/a",)},
    {"normalize_whitespace": True}, {"ignore_pointers": ["/a"]},
    {"casefold_strings": "yes", "text_pointers": ("/x",)},
])
def test_invalid_normalization_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        NormalizationPolicy(**kwargs)


def test_unused_pointer_is_not_a_silent_configuration_typo():
    with pytest.raises(ValueError, match="matched no payload"):
        compare(recording(), recording(), policy=NormalizationPolicy(ignore_pointers=("/typo",)))


def test_json_pointer_escapes_array_indices_and_order_independent_removal():
    value = {"a/b": {"~": ["zero", "one", "two"]}}
    for pointers in itertools.permutations(("/a~1b/~0/0", "/a~1b/~0/2")):
        normalized, applied = NormalizationPolicy(ignore_pointers=pointers).apply(value)
        assert normalized == {"a/b": {"~": ["one"]}}
        assert len(applied) == 2
    normalized, _ = NormalizationPolicy(text_pointers=("/a~1b/~0/1",), casefold_strings=True).apply(
        {"a/b": {"~": ["zero", "ONE"]}})
    assert normalized["a/b"]["~"][1] == "one"


def test_text_pointer_requires_string():
    with pytest.raises(ValueError, match="requires a string"):
        NormalizationPolicy(text_pointers=("/x",), casefold_strings=True).apply({"x": 1})


def test_permutations_and_object_insertion_order_preserve_report():
    source = recording()
    expected = compare(source, source)
    randomizer = random.Random(43)
    for _ in range(25):
        events = copy.deepcopy(list(source.events))
        randomizer.shuffle(events)
        for index, item in enumerate(events):
            events[index] = dict(reversed(list(item.items())))
            events[index]["payload"] = dict(reversed(list(item["payload"].items())))
        report = compare(source, Recording(tuple(events), "sealed_complete"))
        assert report == expected


def test_canonical_number_equivalence_and_unicode_non_equivalence():
    source = recording()
    assert compare(mutate(source, 5, "output", 1), mutate(source, 5, "output", 1.0))["gate"]["exit_code"] == 0
    assert compare(mutate(source, 5, "output", "\u00e9"), mutate(source, 5, "output", "e\u0301"))["gate"]["exit_code"] == 1


@pytest.mark.parametrize("deterministic,judge,infrastructure,confidence,required", tuple(itertools.product(
    ("pass", "fail", "inconclusive"), ("pass", "fail", "inconclusive", "not_run"),
    ("ok", "error", "cancelled", "timed_out"), ("exact", "partial", "ambiguous"), (False, True),
)))
def test_exhaustive_gate_truth_table(deterministic, judge, infrastructure, confidence, required):
    gate = aggregate(deterministic, judge, infrastructure, confidence, judge_required=required)
    if infrastructure == "cancelled":
        expected = 4
    elif infrastructure != "ok":
        expected = 3
    elif deterministic == "fail" or (required and judge == "fail"):
        expected = 1
    elif deterministic != "pass" or confidence != "exact" or (required and judge != "pass"):
        expected = 5
    else:
        expected = 0
    assert gate.exit_code == expected
    assert (gate.status == "pass") == (expected == 0)


@pytest.mark.parametrize("args", [
    ("unknown", "not_run", "ok", "exact"), ("pass", "refused", "ok", "exact"),
    ("pass", "not_run", "running", "exact"), ("pass", "not_run", "ok", "guessed"),
])
def test_unknown_status_is_never_pass(args):
    with pytest.raises(ValueError):
        aggregate(*args)


def test_judges_do_not_override_failure_and_optional_judges_do_not_gate():
    assert aggregate("fail", "pass", "ok", "exact", judge_required=True).exit_code == 1
    assert aggregate("pass", "fail", "ok", "exact").exit_code == 0
    with pytest.raises(ValueError):
        aggregate("pass", "pass", "ok", "exact", judge_required="yes")


def test_shared_rfc8785_fixtures_match_python_and_ecmascript():
    fixtures_path = ROOT / "fixtures/divergence/canonical-v1.json"
    fixtures = json.loads(fixtures_path.read_text())
    actual = [canonical_bytes(fixture["value"]).decode() for fixture in fixtures]
    assert actual == [fixture["canonical"] for fixture in fixtures]
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required for the E08 cross-language canonicalization gate")
    reference = subprocess.run(
        [node, str(ROOT / "fixtures/divergence/canonical-v1.cjs"), str(fixtures_path)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert actual == json.loads(reference.stdout)
