import unittest
from pathlib import Path

from benchmarks.playback import measure_fixture, measure_generated


class PlaybackBenchmarkTests(unittest.TestCase):
    def test_invalid_generated_measurement_limits_fail_closed(self):
        for count, repeats in ((True, 1), (0, 1), (100001, 1),
                               (10, True), (10, 0), (10, 101)):
            with self.assertRaises(ValueError):
                measure_generated(count, repeats)

    def test_generated_ten_thousand_event_projection(self):
        try:
            import anvil_server.playback
        except ModuleNotFoundError:
            self.skipTest("Anvil server is not installed in the SDK-only test environment")
        result = measure_generated(10000, 1)
        self.assertEqual(result["events"], 10000)
        self.assertEqual(len(result["samples_ns"]), 1)
        self.assertEqual(len(result["projection_sha256"]), 64)

    def test_benchmark_checks_correctness_and_reports_raw_samples(self):
        try:
            import anvil_server.playback
        except ModuleNotFoundError:
            self.skipTest("Anvil server is not installed in the SDK-only test environment")
        fixture = Path(__file__).resolve().parents[1] / "fixtures/conformance/v1/retries.json"
        result = measure_fixture(fixture, repeats=2)
        self.assertEqual(len(result["samples_ns"]), 2)
        self.assertGreater(result["events"], 0)
        self.assertIn("machine", result["hardware"])
        self.assertEqual(len(result["projection_sha256"]), 64)
