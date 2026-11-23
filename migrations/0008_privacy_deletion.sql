-- Copyright 2026 Anvil contributors
-- SPDX-License-Identifier: Apache-2.0

BEGIN;

-- Events and recordings are immutable: the trigger from the core migration
-- refuses every UPDATE and every DELETE, which is what makes a sealed
-- recording evidence rather than a mutable log.
--
-- Privacy deletion needs exactly one exception to that, and the exception has
-- to be narrow enough that it does not quietly become a way to edit history.
-- The shape here is: a row may be deleted, never updated, and only inside a
-- transaction that has declared itself a deletion by setting anvil.deletion.
-- The declaration is transaction-local (SET LOCAL), so it cannot leak into a
-- later statement on a pooled connection, and no API path sets it -- only the
-- deletion worker does, in the same transaction that writes the audit row.
--
-- The alternative was to drop the trigger for the deletion path by making the
-- worker a superuser or by disabling triggers on the table. Both remove the
-- guarantee for everyone for the duration, and neither leaves evidence in the
-- schema of what was allowed.
CREATE OR REPLACE FUNCTION reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('anvil.deletion', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'immutable row cannot be changed in %', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

INSERT INTO schema_migrations (version) VALUES ('0008_privacy_deletion');

COMMIT;
