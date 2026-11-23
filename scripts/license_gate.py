"""Strict preflight license check for a supplied dependency inventory."""

import json
import sys
from pathlib import Path


APPROVED = frozenset({"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Python-2.0"})


def check_inventory(inventory: list[dict]) -> None:
    if type(inventory) is not list:
        raise ValueError("Expected dependency inventory list")
    for dependency in inventory:
        if (type(dependency) is not dict or type(dependency.get("name")) is not str
                or not dependency["name"] or type(dependency.get("license")) is not str
                or dependency["license"] not in APPROVED):
            raise ValueError("Dependency has missing or unapproved license")


if __name__ == "__main__":
    try:
        check_inventory(json.loads(Path(sys.argv[1]).read_text()))
    except (IndexError, OSError, ValueError) as error:
        print(f"License gate failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
