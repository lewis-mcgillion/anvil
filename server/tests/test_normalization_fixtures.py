"""Cross-language normalization fixtures (ANVIL-043).

The corpus in ``fixtures/normalization/v1`` is the contract: this module checks
the Python implementation against it, and ``web/schemas/test/normalization.test.ts``
checks an independent TypeScript implementation against the same file. Two
implementations agreeing is what makes the canonical form a contract rather than
an artefact of one runtime.

Regenerate the corpus only as a deliberate compatibility event; the published
hashes are what downstream comparisons pin.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

from anvil_server.canonical import (
    NormalizationPolicy,
    canonical_hash,
    canonicalize,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORPUS = ROOT / "fixtures" / "normalization" / "v1" / "cases.json"
TABLE = ROOT / "web" / "schemas" / "src" / "casefold-exceptions.ts"
GENERATOR = ROOT / "server" / "tests" / "tools" / "generate_casefold_exceptions.py"


def _document() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _cases() -> list[dict]:
    return _document()["cases"]


def _policy(descriptor: dict) -> NormalizationPolicy:
    return NormalizationPolicy(
        tuple(descriptor["ignore_pointers"]),
        normalize_whitespace=descriptor["normalize_whitespace"],
        casefold_strings=descriptor["casefold_strings"],
    )


IDS = [case["id"] for case in _cases()]


def _case(case_id: str) -> dict:
    return next(case for case in _cases() if case["id"] == case_id)


class TestCorpusShape:
    def test_the_corpus_is_published(self) -> None:
        assert CORPUS.exists(), "the normalization corpus must be published"
        assert IDS, "the corpus must contain cases"

    def test_case_ids_are_unique(self) -> None:
        assert len(IDS) == len(set(IDS))

    def test_the_corpus_pins_the_policy_version(self) -> None:
        assert _document()["policy_version"] == NormalizationPolicy.VERSION

    def test_every_case_declares_a_descriptor_for_the_current_version(self) -> None:
        for case in _cases():
            assert case["policy"]["version"] == NormalizationPolicy.VERSION

    def test_every_case_explains_itself(self) -> None:
        """A fixture nobody can interpret cannot be maintained."""
        for case in _cases():
            assert case["description"].strip()


@pytest.mark.parametrize("case_id", IDS)
class TestPythonMatchesTheCorpus:
    def test_normalized_value_matches(self, case_id: str) -> None:
        case = _case(case_id)
        normalized, _ = _policy(case["policy"]).apply(case["input"])
        assert normalized == case["expected_normalized"]

    def test_evidence_matches(self, case_id: str) -> None:
        case = _case(case_id)
        _, evidence = _policy(case["policy"]).apply(case["input"])
        assert evidence.as_dict() == case["expected_evidence"]

    def test_canonical_form_matches(self, case_id: str) -> None:
        case = _case(case_id)
        normalized, _ = _policy(case["policy"]).apply(case["input"])
        assert canonicalize(normalized) == case["expected_canonical"]

    def test_hash_matches(self, case_id: str) -> None:
        case = _case(case_id)
        assert _policy(case["policy"]).canonical_hash(case["input"]) == case[
            "expected_hash"
        ]

    def test_the_published_hash_covers_the_policy_identity(self, case_id: str) -> None:
        """The hash must bind the descriptor, not just the normalized value."""
        case = _case(case_id)
        expected = canonical_hash(
            {"policy": case["policy"], "value": case["expected_normalized"]}
        )
        assert expected == case["expected_hash"]

    def test_apply_does_not_mutate_the_input(self, case_id: str) -> None:
        case = _case(case_id)
        before = json.dumps(case["input"], sort_keys=True)
        _policy(case["policy"]).apply(case["input"])
        assert json.dumps(case["input"], sort_keys=True) == before

    def test_normalization_is_idempotent(self, case_id: str) -> None:
        """Re-normalizing an already normalized value must not move the result.

        Array-index ignores are the documented exception: a pointer such as
        ``/items/1`` addresses a position, not an element, so re-applying it
        removes whatever shifted into that slot. Policies are applied once per
        document, so this is a sharp edge rather than a defect, but it is
        excluded here deliberately instead of being asserted away.
        """
        case = _case(case_id)
        policy = _policy(case["policy"])
        if any(
            re.search(r"/\d+(/|$)", pointer)
            for pointer in case["policy"]["ignore_pointers"]
        ):
            pytest.skip("array-index ignores are positional, see the docstring")
        once, _ = policy.apply(case["input"])
        twice, _ = policy.apply(once)
        assert canonicalize(twice) == canonicalize(once)

    def test_repeated_application_is_stable(self, case_id: str) -> None:
        case = _case(case_id)
        policy = _policy(case["policy"])
        hashes = {policy.canonical_hash(case["input"]) for _ in range(5)}
        assert hashes == {case["expected_hash"]}


class TestHiddenRemovalIsImpossible:
    """A policy may only drop what it explicitly names."""

    def test_unnamed_fields_survive(self) -> None:
        case = _case("unnamed-fields-always-survive")
        normalized, _ = _policy(case["policy"]).apply(case["input"])
        assert normalized["secret_divergence"] == "present"
        assert normalized["nested"]["trace_id"] == "kept"

    def test_an_ignore_does_not_apply_recursively(self) -> None:
        policy = NormalizationPolicy(("/trace_id",))
        normalized, _ = policy.apply({"trace_id": 1, "a": {"trace_id": 2}})
        assert normalized == {"a": {"trace_id": 2}}

    def test_array_index_ignores_are_positional_not_idempotent(self) -> None:
        """Pinned so the sharp edge is visible rather than discovered later."""
        policy = NormalizationPolicy(("/items/1",))
        once, first = policy.apply({"items": ["a", "b", "c"]})
        assert once == {"items": ["a", "c"]}
        assert first.removed == ("/items/1",)
        twice, second = policy.apply(once)
        assert twice == {"items": ["a"]}
        assert second.removed == ("/items/1",)

    def test_a_misdirected_ignore_is_reported_not_silent(self) -> None:
        case = _case("unmatched-pointer-is-reported")
        _, evidence = _policy(case["policy"]).apply(case["input"])
        assert evidence.unmatched == ("/absent",)
        assert evidence.removed == ("/trace_id",)

    def test_dropping_a_field_changes_the_hash_without_a_policy_for_it(self) -> None:
        """Removing evidence cannot be disguised as an equal comparison."""
        policy = NormalizationPolicy()
        full = policy.canonical_hash({"a": 1, "b": 2})
        trimmed = policy.canonical_hash({"a": 1})
        assert full != trimmed

    def test_a_lenient_policy_cannot_collide_with_a_strict_one(self) -> None:
        payload = {"ignored": "x", "kept": 1}
        lenient = NormalizationPolicy(("/ignored",)).canonical_hash(payload)
        strict = NormalizationPolicy().canonical_hash({"kept": 1})
        assert lenient != strict


class TestCasefoldExceptionTable:
    """The TypeScript table must stay current with Python's Unicode data."""

    def _published(self) -> dict[int, str]:
        text = TABLE.read_text(encoding="utf-8")
        entries: dict[int, str] = {}
        for code, folded in re.findall(r'\[0x([0-9a-f]+), "((?:[^"\\]|\\.)*)"\]', text):
            entries[int(code, 16)] = re.sub(
                r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), folded
            )
        return entries

    def test_the_table_exists_and_is_generated(self) -> None:
        assert TABLE.exists()
        assert "GENERATED FILE" in TABLE.read_text(encoding="utf-8")
        assert GENERATOR.exists(), "a generated file needs a published generator"

    def test_the_table_matches_the_running_unicode_data(self) -> None:
        expected = {
            cp: chr(cp).casefold()
            for cp in range(0x110000)
            if chr(cp).casefold() != chr(cp).lower()
        }
        assert self._published() == expected, (
            "casefold exception table is stale; regenerate with "
            "python server/tests/tools/generate_casefold_exceptions.py"
        )

    def test_the_table_covers_the_cases_javascript_gets_wrong(self) -> None:
        published = self._published()
        assert published[0x00DF] == "ss"
        assert published[0xFB01] == "fi"
        assert published[0x03C2] == "\u03c3"

    def test_the_generator_is_reproducible(self, tmp_path: pathlib.Path) -> None:
        """Regenerating must produce byte-identical output."""
        before = TABLE.read_bytes()
        subprocess.run(
            [sys.executable, str(GENERATOR)], cwd=ROOT, check=True, capture_output=True
        )
        assert TABLE.read_bytes() == before


class TestCorpusCoverage:
    """The corpus must exercise the behaviours the contract claims."""

    @pytest.mark.parametrize(
        "case_id",
        [
            "object-key-ordering",
            "number-formatting",
            "string-escaping",
            "unicode-is-not-escaped",
            "ignore-array-element-by-index",
            "ignore-escaped-pointer-tokens",
            "unmatched-pointer-is-reported",
            "casefold-strings",
        ],
    )
    def test_required_case_is_present(self, case_id: str) -> None:
        assert case_id in IDS

    def test_at_least_one_case_reports_unmatched_pointers(self) -> None:
        assert any(case["expected_evidence"]["unmatched"] for case in _cases())

    def test_at_least_one_case_reports_removed_pointers(self) -> None:
        assert any(case["expected_evidence"]["removed"] for case in _cases())

    def test_the_corpus_is_sensitive_to_payload_changes(self) -> None:
        """A golden that cannot fail proves nothing."""
        for case in _cases():
            policy = _policy(case["policy"])
            mutated = dict(case["input"])
            mutated["anvil_sensitivity_probe"] = "changed"
            assert policy.canonical_hash(mutated) != case["expected_hash"]
