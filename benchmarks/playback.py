"""Reproducible, correctness-gated projection timings with hardware metadata."""

import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from anvil_server.playback import project, projection_hash


def measure_fixture(path: Path, repeats: int = 5) -> dict:
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


if __name__ == "__main__":
    print(json.dumps(measure_fixture(Path(sys.argv[1])), sort_keys=True))
