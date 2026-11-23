"""Reject incomplete alpha evidence across three agents and two installations."""

import json
import re
import sys
from pathlib import Path


def verify_alpha(records: list[dict]) -> None:
    if type(records) is not list or len(records) != 6:
        raise ValueError("Alpha requires six agent/environment results")
    agents = set()
    environments = set()
    pairs = set()
    for record in records:
        if type(record) is not dict:
            raise ValueError("Invalid alpha result")
        agent, environment = record.get("agent"), record.get("environment")
        if (type(agent) is not str or not agent.strip() or type(environment) is not str
                or not environment.strip() or (agent, environment) in pairs):
            raise ValueError("Invalid or duplicate agent/environment result")
        agents.add(agent)
        environments.add(environment)
        pairs.add((agent, environment))
        if (any(record.get(gate) is not True for gate in
                ("capture", "seal", "replay", "comparison", "ci_regression", "isolation"))
                or type(record.get("recording_hash")) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", record["recording_hash"]) is None
                or type(record.get("evidence_path")) is not str or not record["evidence_path"]):
            raise ValueError("Alpha result has missing or inconclusive evidence")
    if len(agents) != 3 or len(environments) != 2:
        raise ValueError("Alpha requires three agents in both environments")


if __name__ == "__main__":
    try:
        verify_alpha(json.loads(Path(sys.argv[1]).read_text()))
    except (IndexError, OSError, ValueError) as error:
        print(f"Alpha not ready: {error}", file=sys.stderr)
        raise SystemExit(1) from error
