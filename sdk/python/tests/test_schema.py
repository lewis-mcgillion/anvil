"""Cross-language contract tests for the frozen v1 schemas.

Every fixture in ``fixtures/schema/v1`` is validated here; the Go and
TypeScript suites iterate exactly the same files and must agree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil_schema import SCHEMA_NAMES, ValidationError, validate

FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "schema" / "v1"


def _fixture_cases() -> list[tuple[str, Path, bool]]:
    cases: list[tuple[str, Path, bool]] = []
    for schema_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not schema_dir.is_dir():
            continue
        for fixture in sorted(schema_dir.iterdir()):
            if fixture.name.endswith(".valid.json"):
                cases.append((schema_dir.name, fixture, True))
            elif fixture.name.endswith(".invalid.json"):
                cases.append((schema_dir.name, fixture, False))
            else:
                raise AssertionError(
                    f"fixture {fixture} must end in .valid.json or .invalid.json"
                )
    if not cases:
        raise AssertionError("no schema fixtures found; the corpus must not be empty")
    return cases


CASES = _fixture_cases()


@pytest.mark.parametrize(
    ("schema", "path", "is_valid"),
    CASES,
    ids=[f"{schema}/{path.name}" for schema, path, _ in CASES],
)
def test_fixture_corpus_agreement(schema: str, path: Path, is_valid: bool) -> None:
    data = path.read_text(encoding="utf-8")
    if is_valid:
        validate(schema, data)  # must not raise
    else:
        with pytest.raises(ValidationError):
            validate(schema, data)


def test_malformed_json() -> None:
    with pytest.raises(ValidationError, match="invalid JSON"):
        validate("event", '{"schema_version": "1",')


def test_unknown_schema() -> None:
    with pytest.raises(ValidationError, match="unknown schema"):
        validate("bogus", "{}")


def test_error_names_schema_and_version() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate("seal", json.dumps({"schema_version": "1"}))
    assert "anvil schema seal v1" in str(excinfo.value)


def test_schema_names_cover_every_frozen_document() -> None:
    schema_dir = Path(__file__).resolve().parents[3] / "schemas" / "v1"
    on_disk = {
        p.name[: -len(".schema.json")]
        for p in schema_dir.glob("*.schema.json")
        if p.name != "defs.schema.json"
    }
    assert set(SCHEMA_NAMES) == on_disk
