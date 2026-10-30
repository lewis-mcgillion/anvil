"""Pinned, explicit payload transformations before RFC 8785 comparison."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..canonical import canonical_bytes


def pointer_tokens(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("A non-root JSON pointer is required")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid JSON pointer escape")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class NormalizationPolicy:
    version: str = "1"
    ignore_pointers: tuple[str, ...] = ()
    text_pointers: tuple[str, ...] = ()
    normalize_whitespace: bool = False
    casefold_strings: bool = False

    def __post_init__(self) -> None:
        if self.version != "1":
            raise ValueError("Unsupported divergence normalization version")
        if type(self.normalize_whitespace) is not bool or type(self.casefold_strings) is not bool:
            raise ValueError("Text transformation switches must be booleans")
        if not isinstance(self.ignore_pointers, tuple) or not isinstance(self.text_pointers, tuple):
            raise ValueError("Policy pointers must be immutable tuples")
        pointers = self.ignore_pointers + self.text_pointers
        tokens = [pointer_tokens(pointer) for pointer in pointers]
        for index, left in enumerate(tokens):
            if any(left == right[:len(left)] or right == left[:len(right)]
                   for right in tokens[index + 1:]):
                raise ValueError("Duplicate or overlapping normalization pointers")
        if (self.normalize_whitespace or self.casefold_strings) and not self.text_pointers:
            raise ValueError("Text transformations require explicit text pointers")

    def descriptor(self) -> dict:
        return {
            "version": self.version,
            "canonicalization": "RFC8785",
            "ignore_pointers": sorted(self.ignore_pointers),
            "text_pointers": sorted(self.text_pointers),
            "normalize_whitespace": self.normalize_whitespace,
            "casefold_strings": self.casefold_strings,
        }

    def apply(self, value: dict) -> tuple[dict, tuple[str, ...]]:
        # Round-tripping canonical bytes validates every value, including ignored ones.
        result = json.loads(canonical_bytes(value))
        applied = []
        removals = []
        for pointer in self.ignore_pointers + self.text_pointers:
            tokens = pointer_tokens(pointer)
            parent = result
            for token in tokens[:-1]:
                key = _key(parent, token)
                if key is None:
                    parent = None
                    break
                parent = parent[key]
            key = _key(parent, tokens[-1])
            if key is None:
                continue
            if pointer in self.ignore_pointers:
                removals.append((parent, key))
            else:
                if not isinstance(parent[key], str):
                    raise ValueError(f"Text normalization requires a string at {pointer}")
                text = parent[key]
                if self.normalize_whitespace:
                    text = " ".join(text.split())
                if self.casefold_strings:
                    text = text.casefold()
                parent[key] = text
            applied.append(pointer)
        # Array indices refer to the original document, independent of policy order.
        for parent, key in sorted(removals, key=lambda item: (
            isinstance(item[1], int), item[1] if isinstance(item[1], int) else 0,
        ), reverse=True):
            del parent[key]
        return result, tuple(sorted(applied))


def _key(parent: object, token: str) -> str | int | None:
    if isinstance(parent, dict):
        return token if token in parent else None
    if isinstance(parent, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
        index = int(token)
        return index if index < len(parent) else None
    return None
