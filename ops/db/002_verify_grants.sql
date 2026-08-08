-- WellnessCRM V2 — grant verification.  DDR-15.
--
-- ═══════════════════════════════════════════════════════════════════════════
-- 🔒 RUN AFTER EVERY MIGRATION THAT CREATES OR ALTERS AN APPEND-ONLY TABLE.
-- ═══════════════════════════════════════════════════════════════════════════
--
--   psql "$SUPERUSER_URL" -v ON_ERROR_STOP=1 -f ops/db/002_verify_grants.sql
--
-- ⚠️ **Why this exists as a separate, repeatable check.** DDR-15 makes audit and
-- consent immutability a property of the *grant table*, not of application code:
-- "🔒 The application role physically cannot modify audit history, regardless of
-- what any code does." That guarantee has one weakness — it is enforced by the
-- absence of something. Nothing fails loudly when an UPDATE grant is added back.
--
-- And `001_roles.sql` adds it back, on purpose: its ALTER DEFAULT PRIVILEGES
-- grants all four verbs on future tables so that ordinary tables work without
-- per-migration grant statements. That convenience is precisely the hazard here.
-- This script is the counterweight.
--
-- Exits 0 and reports which tables it checked. Aborts with an exception naming
-- the table and the offending privilege if any append-only table is writable.
--
-- 🟡 Tables absent from the database are skipped and listed as such — this runs
-- cleanly at S0, where none of them exist yet, and becomes a real gate on the S1
-- migration that creates `audit_log`.

\set ON_ERROR_STOP on

DO $$
DECLARE
    -- 🔒 The append-only set, from DB §16 (`audit_log`, `consent_records`),
    -- DB §8.15 / §21 (`operator_actions`) and DB §14.3
    -- (`subscription_events`). Pattern E in DB §11.2: SELECT policy plus no
    -- UPDATE/DELETE grant.
    --
    -- ⚠️ Add a table here in the same commit that creates it. A table that is
    -- append-only in the design but missing from this list is unverified, and
    -- "we assumed it was covered" is the failure mode this file exists to stop.
    --
    -- ⚠️ 🔒 `usage_events` is append-only in design but deliberately **absent**
    -- from this list, and that is not an oversight. It retains UPDATE so a
    -- reconciliation pass can set `is_reconciled`; every other column is frozen
    -- by `trg_usage_events__immutable` (migration 0007), because a grant cannot
    -- express "every column but one". Listing it here would fail on the UPDATE
    -- it is designed to hold. The trigger is the check for that table.
    append_only CONSTANT text[] := ARRAY[
        'audit_log',
        'consent_records',
        'operator_actions',
        'subscription_events'
    ];
    forbidden   CONSTANT text[] := ARRAY['UPDATE', 'DELETE'];

    table_name  text;
    privilege   text;
    checked     text[] := ARRAY[]::text[];
    skipped     text[] := ARRAY[]::text[];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        RAISE EXCEPTION
            'FATAL: role app_user does not exist. Run ops/db/001_roles.sql first.';
    END IF;

    FOREACH table_name IN ARRAY append_only LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_tables
            WHERE schemaname = 'public' AND tablename = table_name
        ) THEN
            skipped := skipped || table_name;
            CONTINUE;
        END IF;

        FOREACH privilege IN ARRAY forbidden LOOP
            IF has_table_privilege('app_user', format('public.%I', table_name), privilege) THEN
                RAISE EXCEPTION
                    'FATAL (DDR-15): app_user holds % on %, which is append-only. '
                    'Audit immutability is enforced by the absence of this grant, so '
                    'the table is currently mutable by the application. Fix with: '
                    'REVOKE UPDATE, DELETE ON TABLE % FROM app_user;',
                    privilege, table_name, table_name;
            END IF;
        END LOOP;

        checked := checked || table_name;
    END LOOP;

    IF array_length(checked, 1) IS NULL THEN
        RAISE NOTICE
            'No append-only tables exist yet — nothing to verify. This becomes a '
            'real gate on the S1 migration that creates audit_log.';
    ELSE
        RAISE NOTICE 'Append-only immutability verified (DDR-15): %',
            array_to_string(checked, ', ');
    END IF;

    IF array_length(skipped, 1) IS NOT NULL THEN
        RAISE NOTICE 'Not present in this database, skipped: %',
            array_to_string(skipped, ', ');
    END IF;
END
$$;
