import unittest
from pathlib import Path

from benchmarks.playback import measure_fixture


class PlaybackBenchmarkTests(unittest.TestCase):
    def test_benchmark_checks_correctness_and_reports_raw_samples(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/conformance/v1/retries.json"
        result = measure_fixture(fixture, repeats=2)
        self.assertEqual(len(result["samples_ns"]), 2)
        self.assertGreater(result["events"], 0)
        self.assertIn("machine", result["hardware"])
        self.assertEqual(len(result["projection_sha256"]), 64)
