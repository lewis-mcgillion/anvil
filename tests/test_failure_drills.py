import unittest

from scripts.verify_failure_drills import REQUIRED, verify_drills


def records():
    return [{"drill": drill, "passed": True, "revision": "abc123", "platform": "linux",
             "evidence_path": f"logs/{drill}.json", "orphan_runners": 0,
             "peak_memory_bytes": 1024} for drill in sorted(REQUIRED)]


class FailureDrillTests(unittest.TestCase):
    def test_all_required_drills_need_passing_evidence(self):
        verify_drills(records())

    def test_missing_failure_and_unbounded_resource_evidence_rejected(self):
        for mutation in (lambda values: values.pop(),
                         lambda values: values[0].update(passed=False),
                         lambda values: values[0].update(orphan_runners=1),
                         lambda values: values[0].update(peak_memory_bytes=0),
                         lambda values: values[0].update(evidence_path=""),
                         lambda values: values[0].update(evidence_path="../secrets.json"),
                         lambda values: values[0].update(evidence_path="/etc/passwd.json"),
                         lambda values: values[0].update(evidence_path="logs\\file.json")):
            values = records()
            mutation(values)
            with self.assertRaises(ValueError):
                verify_drills(values)
