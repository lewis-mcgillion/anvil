"""Load deployment secrets from operator-controlled mounted files."""

import os
from pathlib import Path


def deployment_secret(name: str) -> str | None:
    value = os.getenv(name)
    path = os.getenv(f"{name}_FILE")
    if value is not None and path is not None:
        raise ValueError(f"Configure only one of {name} and {name}_FILE")
    if path is None:
        return value
    if not Path(path).is_absolute():
        raise ValueError(f"{name}_FILE must be an absolute path")
    secret = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    if not secret:
        raise ValueError(f"{name}_FILE must not be empty")
    return secret
