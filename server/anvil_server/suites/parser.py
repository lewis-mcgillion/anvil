"""Bounded YAML-to-JSON decoding with explicit rejection of unsafe YAML features."""

from __future__ import annotations

import math

import rfc8785
import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent

from .models import Suite

MAX_SUITE_BYTES = 1024 * 1024
MAX_NODES = 20000
MAX_DEPTH = 32


class SuiteError(ValueError):
    """Invalid or unsafe suite input."""


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise SuiteError("YAML object keys must be strings")
            if key in result:
                raise SuiteError(f"Duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _json_value(value):
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        if ((type(value) is int and abs(value) > 9007199254740991)
                or (type(value) is float and not math.isfinite(value))):
            raise SuiteError("Numbers must be finite JSON-safe values")
        return
    if isinstance(value, str):
        value.encode("utf-8")
        if any(marker in value for marker in ("${", "{{", "$(", "`")):
            raise SuiteError("Executable/template expressions are forbidden")
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _json_value(key)
            _json_value(item)
        return
    raise SuiteError("Only JSON-compatible YAML values are supported")


def parse_suite(source: str | bytes) -> Suite:
    try:
        if isinstance(source, bytes):
            if len(source) > MAX_SUITE_BYTES:
                raise SuiteError("Suite exceeds byte limit")
            source = source.decode("utf-8")
        if len(source.encode("utf-8")) > MAX_SUITE_BYTES:
            raise SuiteError("Suite exceeds byte limit")
        depth = 0
        for count, event in enumerate(yaml.parse(source), 1):
            if count > MAX_NODES:
                raise SuiteError("Suite exceeds node limit")
            if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
                raise SuiteError("YAML aliases and anchors are forbidden")
            if getattr(event, "tag", None):
                raise SuiteError("Explicit YAML tags are forbidden")
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            if depth > MAX_DEPTH:
                raise SuiteError("Suite exceeds nesting limit")
        data = yaml.load(source, Loader=_Loader)
        _json_value(data)
        suite = Suite.model_validate(data)
        rfc8785.dumps(suite.model_dump())
        return suite
    except SuiteError:
        raise
    except (ValueError, TypeError, yaml.YAMLError, UnicodeError) as exc:
        raise SuiteError(f"Invalid suite: {exc}") from exc
