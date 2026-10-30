"""Core schema validation for Anvil's frozen v1 contract."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal, Union

import jsonschema
from referencing import Registry, Resource

SchemaName = Literal[
    "event", "run", "stream", "artifact", "seal", "capture-gap", "manifest"
]

SCHEMA_NAMES: tuple[str, ...] = (
    "event",
    "run",
    "stream",
    "artifact",
    "seal",
    "capture-gap",
    "manifest",
)

_BASE_URI = "https://anvil.dev/schemas/v1/"
_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
if not _SCHEMA_DIR.is_dir():
    _SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "v1"


class ValidationError(Exception):
    """A document failed schema validation.

    Raised explicitly; the validator never silently accepts or coerces
    input. Malformed JSON is reported as a ValidationError too.
    """

    def __init__(self, schema: str, details: str) -> None:
        self.schema = schema
        self.details = details
        super().__init__(f"anvil schema {schema} v1: validation failed: {details}")


def _load_resource(name: str) -> Resource:
    path = _SCHEMA_DIR / f"{name}.schema.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    # Each frozen document declares "$schema": draft 2020-12, so the resource
    # specification is inferred from the document itself.
    return Resource.from_contents(doc)


@lru_cache(maxsize=None)
def _registry() -> Registry:
    resources = [(f"{_BASE_URI}{name}.json", _load_resource(name)) for name in (*SCHEMA_NAMES, "defs")]
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _validator(schema: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        {"$ref": f"{_BASE_URI}{schema}.json"},
        registry=_registry(),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _summarize(error: jsonschema.ValidationError) -> str:
    parts: list[str] = []
    stack = [error]
    while stack and len(parts) < 4:
        err = stack.pop(0)
        if err.context:
            stack.extend(err.context)
            continue
        loc = "/" + "/".join(str(p) for p in err.absolute_path)
        parts.append(f"{err.message} at {loc}")
    return "; ".join(parts) if parts else str(error.message)


def validate(schema: Union[SchemaName, str], data: Union[bytes, str]) -> None:
    """Validate ``data`` (JSON text) against the named v1 schema.

    Returns ``None`` for valid documents and raises :class:`ValidationError`
    otherwise, including for malformed JSON or an unknown schema name.
    """
    if schema not in SCHEMA_NAMES:
        raise ValidationError(str(schema), f"unknown schema {schema!r}")
    try:
        doc = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValidationError(str(schema), f"invalid JSON: {exc}") from exc
    try:
        _validator(str(schema)).validate(doc)
    except jsonschema.ValidationError as exc:
        raise ValidationError(str(schema), _summarize(exc)) from exc
