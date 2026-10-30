DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anvil_test_app') THEN
        CREATE ROLE anvil_test_app;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO anvil_test_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO anvil_test_app;
GRANT anvil_test_app TO CURRENT_USER;

INSERT INTO organizations (id, slug, name)
VALUES ('0199b167-0000-7000-8000-000000000001', 'tenant-test', 'Tenant test');
INSERT INTO projects (id, organization_id, slug, name)
VALUES
    ('0199b167-0000-7000-8000-000000000011', '0199b167-0000-7000-8000-000000000001', 'one', 'One'),
    ('0199b167-0000-7000-8000-000000000012', '0199b167-0000-7000-8000-000000000001', 'two', 'Two');
INSERT INTO runs (id, project_id)
VALUES
    ('0199b167-0000-7000-8000-000000000021', '0199b167-0000-7000-8000-000000000011'),
    ('0199b167-0000-7000-8000-000000000022', '0199b167-0000-7000-8000-000000000012');

SET ROLE anvil_test_app;
DO $$ BEGIN
    IF (SELECT count(*) FROM runs) <> 0 THEN
        RAISE EXCEPTION 'unscoped read exposed runs';
    END IF;
END $$;

BEGIN;
SELECT set_config('anvil.project_id', '0199b167-0000-7000-8000-000000000011', true);
DO $$ BEGIN
    IF (SELECT count(*) FROM runs) <> 1 THEN
        RAISE EXCEPTION 'project-scoped read exposed another tenant';
    END IF;
END $$;
COMMIT;

DO $$ BEGIN
    IF (SELECT count(*) FROM runs) <> 0 THEN
        RAISE EXCEPTION 'project setting leaked beyond the transaction';
    END IF;
END $$;
RESET ROLE;
