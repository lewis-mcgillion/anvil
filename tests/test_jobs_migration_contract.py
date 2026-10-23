from pathlib import Path
import unittest


MIGRATION = Path(__file__).parents[1] / "migrations" / "0002_jobs.sql"


class JobsMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_migration_is_transactional_and_versioned(self):
        self.assertTrue(self.sql.startswith("-- Copyright"))
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("COMMIT;", self.sql)
        self.assertIn("INSERT INTO schema_migrations (version) VALUES ('0002_jobs')", self.sql)

    def test_jobs_are_scoped_and_lease_safe(self):
        self.assertIn("CREATE TABLE jobs", self.sql)
        self.assertIn("project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE", self.sql)
        self.assertIn("lease_token uuid", self.sql)
        self.assertIn("attempts integer NOT NULL DEFAULT 0", self.sql)
        self.assertIn("outcome IN ('none', 'succeeded', 'failed', 'cancelled', 'interrupted')", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("CREATE POLICY jobs_project_isolation", self.sql)

    def test_polling_index_supports_expired_leases(self):
        self.assertIn("CREATE INDEX jobs_polling_idx", self.sql)
        self.assertIn("WHERE status IN ('queued', 'running', 'cancelling')", self.sql)


if __name__ == "__main__":
    unittest.main()
