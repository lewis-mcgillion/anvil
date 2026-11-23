import unittest

from scripts.verify_alpha_readiness import verify_alpha


def evidence():
    return [{"agent": agent, "environment": environment, "capture": True,
             "seal": True, "replay": True, "comparison": True,
             "ci_regression": True, "isolation": True,
             "recording_hash": "sha256:" + "a" * 64, "evidence_path": "logs/result.json"}
            for agent in ("a", "b", "c") for environment in ("local", "hosted")]


class AlphaReadinessTests(unittest.TestCase):
    def test_complete_matrix_passes_structural_gate(self):
        verify_alpha(evidence())

    def test_missing_failed_or_duplicate_results_rejected(self):
        for mutation in (lambda rows: rows.pop(),
                         lambda rows: rows[0].update(isolation=False),
                         lambda rows: rows[0].update(recording_hash="sha256:abc"),
                         lambda rows: rows[0].update(recording_hash="sha256:" + "A" * 64),
                         lambda rows: rows[0].update(evidence_path=""),
                         lambda rows: rows[0].update(environment="hosted")):
            rows = evidence()
            mutation(rows)
            with self.assertRaises(ValueError):
                verify_alpha(rows)
