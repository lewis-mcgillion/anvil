"""Validators for Anvil's frozen v1 event and recording JSON Schemas.

This package is the Python implementation of the cross-language contract:
the fixtures in ``fixtures/schema/v1`` are validated by Go, Python and
TypeScript and all three must agree.

Validation is pure and in-memory; there is nothing to cancel and no
resource to clean up. Compiled validators are immutable and safe to reuse
across threads.
"""

from anvil_schema._validator import (
    SCHEMA_NAMES,
    SchemaName,
    ValidationError,
    validate,
)

__all__ = [
    "SCHEMA_NAMES",
    "SchemaName",
    "ValidationError",
    "validate",
]
