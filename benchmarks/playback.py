"""Reproducible, correctness-gated projection timings with hardware metadata."""

import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

def measure_fixture(path: Path, repeats: int = 5) -> dict:
    from anvil_server.playback import project, projection_hash

    if repeats < 1:
        raise ValueError("At least one measurement is required")
    fixture = json.loads(path.read_text())
    events = fixture["events"]
    reference = projection_hash(project(events))
    measurements = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        result = project(events)
        measurements.append(time.perf_counter_ns() - start)
        if projection_hash(result) != reference:
            raise RuntimeError("Projection result changed during benchmark")
    return {"dataset": path.name, "events": len(events), "projection_sha256": reference,
            "samples_ns": measurements, "median_ns": statistics.median(measurements),
            "hardware": {"platform": platform.platform(), "machine": platform.machine(),
                         "processor": platform.processor(), "cpu_count": os.cpu_count(),
                         "python": sys.version.split()[0]}}


def measure_generated(event_count: int = 10000, repeats: int = 5) -> dict:
    from anvil_server.playback import project, projection_hash

    if not 1 <= event_count <= 100000 or repeats < 1:
        raise ValueError("Invalid generated benchmark size or repeats")
    events = [{"event_id": f"00000000-0000-7000-8000-{index:012d}",
               "run_id": "00000000-0000-7000-8000-000000900001",
               "stream_id": "00000000-0000-7000-8000-000000900002",
               "sequence": index, "type": "llm.token.chunk", "payload": {"text": "token"},
               "parent_event_ids": []} for index in range(event_count)]
    reference = projection_hash(project(events))
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        result = project(events)
        samples.append(time.perf_counter_ns() - start)
        if projection_hash(result) != reference:
            raise RuntimeError("Generated projection changed during benchmark")
    return {"dataset": "generated-token-chunks", "events": event_count,
            "projection_sha256": reference, "samples_ns": samples,
            "median_ns": statistics.median(samples),
            "hardware": {"platform": platform.platform(), "machine": platform.machine(),
                         "processor": platform.processor(), "cpu_count": os.cpu_count(),
                         "python": sys.version.split()[0]}}


if __name__ == "__main__":
    print(json.dumps(measure_fixture(Path(sys.argv[1])), sort_keys=True))
