"""Fail closed when required disposable-environment drill evidence is absent."""

import json
import sys
from pathlib import Path


REQUIRED = frozenset({"database_outage", "disk_full", "worker_crash",
                      "broker_outage", "high_load", "cancellation"})


def verify_drills(records: list[dict]) -> None:
    if type(records) is not list or len(records) != len(REQUIRED):
        raise ValueError("Missing or duplicate resilience drills")
    seen = set()
    for record in records:
        if type(record) is not dict or record.get("drill") not in REQUIRED or record["drill"] in seen:
            raise ValueError("Unknown or duplicate resilience drill")
        seen.add(record["drill"])
        if (record.get("passed") is not True or type(record.get("revision")) is not str
                or not record["revision"] or type(record.get("platform")) is not str
                or not record["platform"] or type(record.get("evidence_path")) is not str
                or not record["evidence_path"] or type(record.get("orphan_runners")) is not int
                or record["orphan_runners"] != 0 or type(record.get("peak_memory_bytes")) is not int
                or record["peak_memory_bytes"] <= 0):
            raise ValueError("Resilience drill lacks passing, bounded evidence")
        evidence_path = Path(record["evidence_path"])
        if (evidence_path.is_absolute() or any(part in (".", "..") for part in record["evidence_path"].split("/"))
                or "\\" in record["evidence_path"] or not evidence_path.suffix == ".json"):
            raise ValueError("Resilience drill evidence path is unsafe")


if __name__ == "__main__":
    try:
        verify_drills(json.loads(Path(sys.argv[1]).read_text()))
    except (IndexError, OSError, ValueError) as error:
        print(f"Failure drills not verified: {error}", file=sys.stderr)
        raise SystemExit(1) from error
