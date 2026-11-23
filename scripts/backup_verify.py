"""Verify recorded archive hashes after operator-managed backup/restore."""

import hashlib
import re
from pathlib import Path


def recording_hashes(files: list[Path]) -> dict[str, str]:
    hashes = {}
    for path in files:
        if path.is_symlink() or not path.is_file() or path.name in hashes:
            raise ValueError("Missing or duplicated recording archive")
        digest = hashlib.sha256()
        with path.open("rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.name] = digest.hexdigest()
    return dict(sorted(hashes.items()))


def verify_restore(expected: dict[str, str], restored: list[Path]) -> None:
    if (not expected or any(type(name) is not str or not name.endswith(".anvil")
                            or Path(name).name != name or type(digest) is not str
                            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                            for name, digest in expected.items())
            or recording_hashes(restored) != expected):
        raise ValueError("Restored recordings differ from backup manifest")
