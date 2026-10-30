"""RFC 8785 JSON Canonicalization Scheme (JCS) with fail-closed value handling.

Canonical bytes are the single normalization primitive shared by projection
hashing, fixture matching and divergence detection. Every consumer must obtain
identical bytes for logically identical values, so this module refuses any input
whose canonical form would be ambiguous instead of silently coercing it.

Rejected inputs raise :class:`CanonicalizationError`:

* non-finite floats (``NaN``, ``Infinity``) have no JSON representation;
* integers outside the IEEE-754 exact range, because JCS defines numbers as
  doubles and a lossy round-trip would let two different values share bytes;
* non-``str`` object keys, which would otherwise be coerced and could collide;
* unpaired UTF-16 surrogates, which cannot be encoded as UTF-8;
* cycles, which would otherwise recurse until the interpreter fails.

The module performs no I/O and holds no resources, so there is nothing to clean
up and no cancellation semantics; callers own any surrounding timeouts.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

__all__ = [
    "CanonicalizationError",
    "canonicalize",
    "canonical_bytes",
    "canonical_hash",
    "NormalizationPolicy",
]

# Largest integer magnitude that survives a float round-trip exactly.
MAX_EXACT_INTEGER = 2**53 - 1

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalizationError(ValueError):
    """Raised when a value has no unambiguous canonical representation."""


def _format_number(value: int | float) -> str:
    if isinstance(value, bool):  # bool is an int subclass; handled by caller.
        raise CanonicalizationError("Booleans are not numbers")
    if isinstance(value, int):
        if abs(value) > MAX_EXACT_INTEGER:
            raise CanonicalizationError(
                f"Integer {value} exceeds the exact IEEE-754 range required by RFC 8785"
            )
        return str(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise CanonicalizationError("Non-finite numbers have no JSON representation")
    if value == 0:
        return "0"  # Collapses -0.0, matching ECMAScript Number::toString.
    sign = "-" if value < 0 else ""
    return sign + _ecmascript_double(abs(value))


def _ecmascript_double(value: float) -> str:
    """Render a positive finite double exactly as ECMAScript ``Number::toString``."""
    if value.is_integer() and abs(value) <= MAX_EXACT_INTEGER:
        digits, exponent = str(int(value)), None
        stripped = digits.rstrip("0") or "0"
        k, n = len(stripped), len(digits)
        digits = stripped
    else:
        parts = Decimal(repr(value)).as_tuple()
        digits = "".join(str(digit) for digit in parts.digits)
        trimmed = digits.rstrip("0") or "0"
        n = int(parts.exponent) + len(digits)
        digits, k = trimmed, len(trimmed)
    exponent = n

    if k <= exponent <= 21:
        return digits + "0" * (exponent - k)
    if 0 < exponent <= 21:
        return digits[:exponent] + "." + digits[exponent:]
    if -6 < exponent <= 0:
        return "0." + "0" * (-exponent) + digits
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    power = exponent - 1
    return f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _format_string(value: str) -> str:
    out = ['"']
    for character in value:
        escape = _ESCAPES.get(character)
        if escape is not None:
            out.append(escape)
        elif character < "\u0020":
            out.append(f"\\u{ord(character):04x}")
        elif "\ud800" <= character <= "\udfff":
            raise CanonicalizationError("Unpaired UTF-16 surrogate cannot be encoded")
        else:
            out.append(character)
    out.append('"')
    return "".join(out)


def _sort_key(key: str) -> bytes:
    # RFC 8785 orders members by UTF-16 code unit; big-endian UTF-16 bytes
    # compare in exactly that order.
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as error:
        raise CanonicalizationError("Object key contains an unpaired surrogate") from error


def _write(value: Any, out: list[str], seen: set[int]) -> None:
    if value is None:
        out.append("null")
        return
    if value is True:
        out.append("true")
        return
    if value is False:
        out.append("false")
        return
    if isinstance(value, str):
        out.append(_format_string(value))
        return
    if isinstance(value, (int, float)):
        out.append(_format_number(value))
        return
    if isinstance(value, (list, tuple)):
        if id(value) in seen:
            raise CanonicalizationError("Cycle detected while canonicalizing")
        seen.add(id(value))
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out, seen)
        out.append("]")
        seen.discard(id(value))
        return
    if isinstance(value, dict):
        if id(value) in seen:
            raise CanonicalizationError("Cycle detected while canonicalizing")
        seen.add(id(value))
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"Object keys must be strings, found {type(key).__name__}"
                )
        out.append("{")
        for index, key in enumerate(sorted(value, key=_sort_key)):
            if index:
                out.append(",")
            out.append(_format_string(key))
            out.append(":")
            _write(value[key], out, seen)
        out.append("}")
        seen.discard(id(value))
        return
    raise CanonicalizationError(f"Unsupported type {type(value).__name__}")


def canonicalize(value: Any) -> str:
    """Return the RFC 8785 canonical JSON text for ``value``."""
    out: list[str] = []
    _write(value, out, set())
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    """Return the UTF-8 encoded canonical JSON for ``value``."""
    return canonicalize(value).encode("utf-8")


def canonical_hash(value: Any) -> str:
    """Return the lowercase hex SHA-256 of ``value``'s canonical JSON."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class NormalizationPolicy:
    """Explicitly configured, versioned comparison normalization.

    Only the JSON pointers listed in ``ignore_pointers`` are removed, and every
    removal is recorded so a comparison can report that evidence was dropped.
    Policies cannot hide fields implicitly: an unknown pointer is a no-op that
    is still reported, and a pointer may not be the document root.
    """

    VERSION = "1"

    def __init__(
        self,
        ignore_pointers: tuple[str, ...] = (),
        *,
        normalize_whitespace: bool = False,
        casefold_strings: bool = False,
    ) -> None:
        for pointer in ignore_pointers:
            if not pointer.startswith("/"):
                raise ValueError(f"JSON pointer {pointer!r} must start with '/'")
        self.ignore_pointers = tuple(sorted(set(ignore_pointers)))
        self.normalize_whitespace = normalize_whitespace
        self.casefold_strings = casefold_strings

    def descriptor(self) -> dict[str, Any]:
        """Return the serializable policy identity recorded in reports."""
        return {
            "version": self.VERSION,
            "ignore_pointers": list(self.ignore_pointers),
            "normalize_whitespace": self.normalize_whitespace,
            "casefold_strings": self.casefold_strings,
        }

    def apply(self, value: Any) -> tuple[Any, list[str]]:
        """Return the normalized copy of ``value`` and the pointers removed."""
        removed: list[str] = []
        result = _deep_copy(value)
        for pointer in self.ignore_pointers:
            if _remove_pointer(result, pointer):
                removed.append(pointer)
        if self.normalize_whitespace or self.casefold_strings:
            result = self._map_strings(result)
        return result, removed

    def _map_strings(self, value: Any) -> Any:
        if isinstance(value, str):
            text = " ".join(value.split()) if self.normalize_whitespace else value
            return text.casefold() if self.casefold_strings else text
        if isinstance(value, list):
            return [self._map_strings(item) for item in value]
        if isinstance(value, dict):
            return {key: self._map_strings(item) for key, item in value.items()}
        return value

    def canonical_hash(self, value: Any) -> str:
        normalized, _ = self.apply(value)
        return canonical_hash({"policy": self.descriptor(), "value": normalized})


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_copy(item) for item in value]
    return value


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _remove_pointer(document: Any, pointer: str) -> bool:
    tokens = [_unescape(token) for token in pointer.split("/")[1:]]
    if not tokens:
        raise ValueError("Refusing to ignore the document root")
    cursor = document
    for token in tokens[:-1]:
        if isinstance(cursor, dict):
            if token not in cursor:
                return False
            cursor = cursor[token]
        elif isinstance(cursor, list):
            if not token.isdigit() or int(token) >= len(cursor):
                return False
            cursor = cursor[int(token)]
        else:
            return False
    last = tokens[-1]
    if isinstance(cursor, dict) and last in cursor:
        del cursor[last]
        return True
    if isinstance(cursor, list) and last.isdigit() and int(last) < len(cursor):
        del cursor[int(last)]
        return True
    return False
