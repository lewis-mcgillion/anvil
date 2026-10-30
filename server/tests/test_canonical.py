"""RFC 8785 conformance and fail-closed behaviour for canonical normalization."""

from __future__ import annotations

import pytest

from anvil_server.canonical import (
    CanonicalizationError,
    NormalizationPolicy,
    canonical_bytes,
    canonical_hash,
    canonicalize,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-0.0, "0"),
        (1, "1"),
        (1.0, "1"),
        (-1.5, "-1.5"),
        (1e21, "1e+21"),
        (1e-7, "1e-7"),
        (0.000001, "0.000001"),
        (1e20, "100000000000000000000"),
        (333333333.33333329, "333333333.3333333"),
        ("", '""'),
        ("\u20ac", '"\u20ac"'),
        ("\n", '"\\n"'),
        ("\u0001", '"\\u0001"'),
        ([], "[]"),
        ({}, "{}"),
        ([1, [2, 3]], "[1,[2,3]]"),
    ],
)
def test_scalar_and_container_forms(value, expected):
    assert canonicalize(value) == expected


def test_object_keys_sort_by_utf16_code_unit():
    # "\U0001f600" is a surrogate pair in UTF-16, so it sorts after "\uffff".
    document = {"\u00e4": 1, "a": 2, "\uffff": 3, "\U0001f600": 4, "A": 5}
    assert canonicalize(document) == (
        '{"A":5,"a":2,"\u00e4":1,"\U0001f600":4,"\uffff":3}'
    )


def test_rfc8785_reference_object():
    document = {
        "numbers": [333333333.33333329, 1e30, 4.5, 2e-3, 0.000000000000000000000000001],
        "string": "\u20ac$\u000f\nA'\u0042\u0022\u005c\\\"/",
        "literals": [None, True, False],
    }
    assert canonicalize(document) == (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )


def test_canonical_form_is_independent_of_insertion_order():
    first = {"b": {"d": 1, "c": 2}, "a": [1, 2]}
    second = {"a": [1, 2], "b": {"c": 2, "d": 1}}
    assert canonicalize(first) == canonicalize(second)
    assert canonical_hash(first) == canonical_hash(second)


def test_canonical_bytes_are_utf8():
    assert canonical_bytes("\u20ac") == b'"\xe2\x82\xac"'


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        2**53,
        -(2**53),
        {1: "int key"},
        {(1, 2): "tuple key"},
        object(),
        b"bytes",
        "\ud800",
    ],
)
def test_ambiguous_values_fail_closed(value):
    with pytest.raises(CanonicalizationError):
        canonicalize(value)


def test_exact_integer_boundary_is_accepted():
    assert canonicalize(2**53 - 1) == "9007199254740991"


def test_cycles_are_rejected_rather_than_recursing():
    document: dict = {}
    document["self"] = document
    with pytest.raises(CanonicalizationError, match="Cycle"):
        canonicalize(document)

    array: list = []
    array.append(array)
    with pytest.raises(CanonicalizationError, match="Cycle"):
        canonicalize(array)


def test_repeated_structures_are_not_mistaken_for_cycles():
    shared = {"a": 1}
    assert canonicalize([shared, shared]) == '[{"a":1},{"a":1}]'


class TestNormalizationPolicy:
    def test_default_policy_removes_nothing(self):
        policy = NormalizationPolicy()
        value, removed = policy.apply({"a": 1})
        assert value == {"a": 1}
        assert removed == []

    def test_ignored_pointers_are_removed_and_reported(self):
        policy = NormalizationPolicy(("/timing/latency_ms", "/trace_id"))
        value, removed = policy.apply(
            {"timing": {"latency_ms": 12, "kept": 1}, "trace_id": "abc", "output": "x"}
        )
        assert value == {"timing": {"kept": 1}, "output": "x"}
        assert removed == ["/timing/latency_ms", "/trace_id"]

    def test_apply_does_not_mutate_the_input(self):
        original = {"trace_id": "abc", "nested": {"keep": [1, 2]}}
        policy = NormalizationPolicy(("/trace_id",))
        policy.apply(original)
        assert original == {"trace_id": "abc", "nested": {"keep": [1, 2]}}

    def test_absent_pointer_is_not_reported_as_removed(self):
        policy = NormalizationPolicy(("/missing",))
        value, removed = policy.apply({"a": 1})
        assert value == {"a": 1}
        assert removed == []

    def test_hidden_field_removal_is_impossible(self):
        """A field not named by the policy always survives normalization."""
        policy = NormalizationPolicy(("/trace_id",))
        value, _ = policy.apply({"trace_id": "x", "secret_divergence": "present"})
        assert value == {"secret_divergence": "present"}

    def test_policy_identity_is_part_of_the_hash(self):
        lenient = NormalizationPolicy(("/trace_id",))
        strict = NormalizationPolicy()
        payload = {"trace_id": "a", "value": 1}
        assert lenient.canonical_hash(payload) != strict.canonical_hash(payload)

    def test_descriptor_is_versioned_and_sorted(self):
        policy = NormalizationPolicy(("/b", "/a", "/a"))
        assert policy.descriptor() == {
            "version": "1",
            "ignore_pointers": ["/a", "/b"],
            "normalize_whitespace": False,
            "casefold_strings": False,
        }

    def test_text_normalization_is_opt_in(self):
        policy = NormalizationPolicy(normalize_whitespace=True, casefold_strings=True)
        value, _ = policy.apply({"text": "  Hello   World \n"})
        assert value == {"text": "hello world"}

    def test_pointer_must_be_rooted(self):
        with pytest.raises(ValueError, match="must start with"):
            NormalizationPolicy(("timing",))

    def test_document_root_cannot_be_ignored(self):
        policy = NormalizationPolicy()
        policy.ignore_pointers = ("/",)
        # "/" addresses the empty-string member, not the root; the root itself
        # is unreachable because every pointer must contain at least one token.
        value, removed = policy.apply({"": 1, "a": 2})
        assert value == {"a": 2}
        assert removed == ["/"]

    def test_array_elements_can_be_ignored_by_index(self):
        policy = NormalizationPolicy(("/items/1",))
        value, removed = policy.apply({"items": [1, 2, 3]})
        assert value == {"items": [1, 3]}
        assert removed == ["/items/1"]

    def test_escaped_pointer_tokens_are_decoded(self):
        policy = NormalizationPolicy(("/a~1b", "/c~0d"))
        value, removed = policy.apply({"a/b": 1, "c~d": 2, "keep": 3})
        assert value == {"keep": 3}
        assert sorted(removed) == ["/a~1b", "/c~0d"]
