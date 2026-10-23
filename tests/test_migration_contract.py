"""Contract checks for the first PostgreSQL migration."""

from pathlib import Path
import unittest


MIGRATION = Path(__file__).parents[1] / "migrations" / "0001_core.sql"
<<<<<<< HEAD
AUTH_MIGRATION = Path(__file__).parents[1] / "migrations" / "0002_authorization.sql"
=======
ADMIN_MIGRATION = Path(__file__).parents[1] / "migrations" / "0002_administration.sql"
>>>>>>> origin/pr/118


class CoreMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_migration_is_transactional_and_versioned(self):
        self.assertTrue(self.sql.startswith("-- Copyright"))
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("COMMIT;", self.sql)
        self.assertIn("CREATE TABLE schema_migrations", self.sql)
        self.assertIn("INSERT INTO schema_migrations (version) VALUES ('0001_core')", self.sql)

    def test_core_entities_and_project_scope_are_present(self):
        for table in (
            "organizations",
            "projects",
            "principals",
            "project_grants",
            "runs",
            "run_streams",
            "events",
            "artifacts",
            "recordings",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
        self.assertIn("project_id uuid NOT NULL", self.sql)
        self.assertIn("CREATE OR REPLACE FUNCTION is_uuidv7", self.sql)

    def test_event_conflicts_are_rejected_by_constraints(self):
        self.assertIn("event_id uuid PRIMARY KEY", self.sql)
        self.assertIn("UNIQUE (stream_id, sequence)", self.sql)
        self.assertIn("content_hash bytea NOT NULL", self.sql)
        self.assertIn("octet_length(content_hash) = 32", self.sql)
        self.assertIn("events_are_immutable", self.sql)

    def test_replay_data_has_no_execution_capability(self):
        self.assertNotIn("command", self.sql.lower())
        self.assertNotIn("production", self.sql.lower())
        self.assertIn("replay_safe boolean NOT NULL", self.sql)


<<<<<<< HEAD
class AuthorizationMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = AUTH_MIGRATION.read_text(encoding="utf-8")

    def test_api_keys_are_project_scoped_and_hashed(self):
        self.assertIn("bootstrap_principal", self.sql)
        self.assertIn("CREATE TABLE api_keys", self.sql)
        self.assertIn("key_hash bytea NOT NULL", self.sql)
        self.assertIn("octet_length(key_hash) = 32", self.sql)
        self.assertIn("UNIQUE (project_id, name)", self.sql)
        self.assertIn("rotated_from uuid", self.sql)
        self.assertIn("revoked_at timestamptz", self.sql)

    def test_scope_and_rls_guards_are_fail_closed(self):
        self.assertIn("key_hash)", self.sql)
        self.assertIn("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("CREATE POLICY api_keys_project_isolation", self.sql)
        self.assertIn("'capture:write' = ANY(scopes)", self.sql)
        self.assertIn("'runs:read' = ANY(scopes)", self.sql)
        self.assertIn("'replay:write' = ANY(scopes)", self.sql)
        self.assertIn("INSERT INTO schema_migrations (version) VALUES ('0002_authorization')", self.sql)
=======
class AdministrationMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = ADMIN_MIGRATION.read_text(encoding="utf-8")

    def test_migration_is_transactional_and_versioned(self):
        self.assertTrue(self.sql.startswith("-- Copyright"))
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("COMMIT;", self.sql)
        self.assertIn("INSERT INTO schema_migrations (version) VALUES ('0002_administration')", self.sql)

    def test_jobs_and_mutation_records_are_project_scoped(self):
        for table in ("jobs", "idempotency_keys", "deletion_requests", "audit_events"):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("PRIMARY KEY (project_id, key)", self.sql)
        self.assertIn("UNIQUE (project_id, target_kind, target_id)", self.sql)

    def test_job_states_and_retention_are_bounded(self):
        self.assertIn("'cancelling', 'cancelled', 'succeeded', 'failed'", self.sql)
        self.assertIn("retention_days BETWEEN 1 AND 365", self.sql)
>>>>>>> origin/pr/118


if __name__ == "__main__":
    unittest.main()
