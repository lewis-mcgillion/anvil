"""The comparisons migration must be transactional, scoped and fail-closed."""

import unittest
from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "0007_comparisons.sql"


class ComparisonsMigrationContract(unittest.TestCase):
    def setUp(self):
        self.sql = MIGRATION.read_text()

    def test_the_migration_is_transactional_and_recorded(self):
        self.assertTrue(self.sql.lstrip().startswith("BEGIN;"))
        self.assertIn("COMMIT;", self.sql)
        self.assertIn("INSERT INTO schema_migrations (version) VALUES ('0007_comparisons')",
                      self.sql)

    def test_both_tables_are_project_isolated(self):
        for table in ("comparisons", "comparison_divergences"):
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_project_isolation", self.sql)

    def test_the_three_statuses_are_separate_columns(self):
        # Collapsing them would make it impossible for a consumer to see what
        # each layer said, which is the acceptance criterion for ANVIL-046.
        self.assertIn("deterministic text NOT NULL", self.sql)
        self.assertIn("judge text NOT NULL", self.sql)
        self.assertIn("infrastructure text NOT NULL", self.sql)

    def test_an_unfinished_comparison_does_not_default_to_a_pass(self):
        self.assertIn("deterministic text NOT NULL DEFAULT 'inconclusive'", self.sql)
        self.assertNotIn("deterministic text NOT NULL DEFAULT 'pass'", self.sql)

    def test_status_columns_are_constrained_to_the_published_vocabularies(self):
        self.assertIn("CHECK (deterministic IN ('pass', 'fail', 'inconclusive'))", self.sql)
        self.assertIn("CHECK (judge IN ('pass', 'fail', 'inconclusive', 'not_run'))", self.sql)
        self.assertIn("CHECK (infrastructure IN ('ok', 'error', 'cancelled', 'timed_out'))",
                      self.sql)
        self.assertIn("CHECK (alignment_confidence IN ('exact', 'partial', 'ambiguous'))",
                      self.sql)

    def test_findings_are_ordered_for_stable_pagination(self):
        self.assertIn("ordinal integer NOT NULL CHECK (ordinal >= 0)", self.sql)
        self.assertIn("UNIQUE (comparison_id, ordinal)", self.sql)

    def test_findings_are_removed_with_their_comparison(self):
        self.assertIn("REFERENCES comparisons (id, project_id) ON DELETE CASCADE", self.sql)

    def test_comparisons_are_removed_with_their_project(self):
        self.assertIn("REFERENCES projects(id) ON DELETE CASCADE", self.sql)


if __name__ == "__main__":
    unittest.main()
