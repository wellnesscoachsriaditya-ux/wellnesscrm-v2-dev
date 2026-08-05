-- WellnessCRM V2 — database roles.  DB §2.4.
--
-- ═══════════════════════════════════════════════════════════════════════════
-- 🔒 RUN ONCE PER DATABASE, BY A SUPERUSER, BEFORE THE FIRST MIGRATION.
-- ═══════════════════════════════════════════════════════════════════════════
--
-- ⚠️ **Why this is not an Alembic migration.** Three reasons, any one of which
-- would be sufficient:
--
--   1. Alembic connects as `app_migrator`. A migration cannot create the role
--      it is already authenticating as.
--   2. `CREATE ROLE` requires SUPERUSER or CREATEROLE. Granting `app_migrator`
--      CREATEROLE would let the migration credential mint its own privileges,
--      which defeats having two roles at all.
--   3. Roles are **cluster-scoped**, not database-scoped. They do not belong in
--      one database's linear migration history, and a `downgrade` that dropped
--      them could take out a sibling database.
--
-- Provisioning is therefore an operator action, recorded here as a reviewable,
-- re-runnable artefact rather than a paragraph in a runbook.
--
-- ─── Usage ────────────────────────────────────────────────────────────────
--
--   psql "$SUPERUSER_URL" -v ON_ERROR_STOP=1 -f ops/db/001_roles.sql
--
-- On Supabase: paste into the SQL editor and run as the `postgres` role.
-- Idempotent — safe to re-run after new tables exist, which is the intended way
-- to repair grants if a migration ever creates a table outside `app_migrator`.
--
-- 🔒 **This script sets no passwords, by design** (NFR-034: secrets never enter
-- source control). Both roles are created with LOGIN but no password, and a role
-- with no password cannot authenticate under `md5`/`scram` — so the default
-- state is closed, not open. Set them separately, from your secret store:
--
--   ALTER ROLE app_migrator WITH PASSWORD '<from secret store>';
--   ALTER ROLE app_user     WITH PASSWORD '<from secret store>';
--
-- Then put the two connection strings in `DATABASE_MIGRATION_URL` and
-- `DATABASE_URL` respectively (see `.env.example`).

\set ON_ERROR_STOP on

BEGIN;

-- ─── The roles ────────────────────────────────────────────────────────────
--
-- 🔒 Every negative attribute is stated explicitly rather than left to the
-- server default. Defaults are correct today; an explicit NOBYPASSRLS is a
-- claim this file makes and a reviewer can check, and it survives someone
-- changing a template database.
--
-- 🔒 NOBYPASSRLS on `app_user` is the single most important line in this file.
-- With BYPASSRLS, every tenant-isolation policy in DB §8 becomes decorative
-- while still appearing present in `pg_policies` — the exact V1 failure mode
-- ADR-02 exists to prevent. `app.platform.db.verify_no_rls_bypass` re-checks
-- this at every startup outside local, because one line in one script is not
-- enough assurance for the guarantee the whole tenancy model rests on.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator LOGIN
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
        RAISE NOTICE 'created role app_migrator (no password set — see header)';
    ELSE
        ALTER ROLE app_migrator
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
        RAISE NOTICE 'role app_migrator already existed — attributes reasserted';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
        RAISE NOTICE 'created role app_user (no password set — see header)';
    ELSE
        ALTER ROLE app_user
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
        RAISE NOTICE 'role app_user already existed — attributes reasserted';
    END IF;
END
$$;

-- 🟡 `app_readonly` (DB §2.4, future analytics) is deliberately NOT created.
-- An unused role that can log in is attack surface for a capability nobody has
-- asked for yet. Add it in the sprint that adds the analytics consumer.


-- ─── Schema access ────────────────────────────────────────────────────────
--
-- 🔒 `PUBLIC` — the implicit role every role inherits — must not be able to
-- create objects. Postgres 15+ already revokes this, but earlier clusters and
-- restored dumps do not, and a table created by an unexpected role would not
-- pick up the default privileges configured below.

REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- `GRANT ... ON DATABASE` requires a literal name, so the current database is
-- interpolated rather than named — this script must be safe to run against
-- local, staging and production without editing it (NFR-075).
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_migrator', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_user', current_database());
END
$$;

-- 🔒 CREATE on the schema is the DDL right, and only `app_migrator` has it.
-- This is what makes "app_user has no DDL" true at the database level rather
-- than being a property of the code that happens to never issue DDL.
GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;
GRANT USAGE          ON SCHEMA public TO app_user;


-- ─── Default privileges for future tables ─────────────────────────────────
--
-- Without this, every migration that creates a table must also remember to
-- grant on it, and the failure mode is a runtime permission error on a code
-- path nobody exercised before release. `FOR ROLE app_migrator` scopes these to
-- objects that role creates — which, given only `app_migrator` holds CREATE, is
-- every application object.
--
-- ⚠️ 🔒 **DDR-15 obligation, and it points the other way.** Audit and consent
-- immutability is enforced by the *absence* of an UPDATE/DELETE grant. These
-- defaults grant all four verbs, so the migration that creates `audit_log`,
-- `consent_records` or `operator_actions` MUST revoke the two it must not have:
--
--     REVOKE UPDATE, DELETE ON TABLE audit_log FROM app_user;
--
-- That is an S1 obligation on the kernel migration, not something this file can
-- do for a table that does not exist yet. It is written here because this is
-- the file that creates the hazard.

ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;

ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;

-- 🔒 No default EXECUTE on functions. A function runs with the privileges its
-- definition specifies, so a blanket grant is how a SECURITY DEFINER helper
-- becomes an unintended RLS bypass. Grant per function, deliberately.


-- ─── Repair grants on objects that already exist ──────────────────────────
--
-- Default privileges apply only to objects created *after* they are configured.
-- These two statements make the script idempotent in the useful sense: re-run it
-- after a migration and any table created outside the expected path is brought
-- into line.
--
-- ⚠️ This is a blanket grant, so it also re-grants UPDATE/DELETE on append-only
-- tables. Re-run `002_verify_grants.sql` afterwards — it is what catches that.

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;


-- ─── Self-verification ────────────────────────────────────────────────────
--
-- 🔒 A provisioning script that reports success without checking its own
-- postcondition is how a cluster ends up one attribute away from having no
-- tenant isolation. These assertions abort the transaction rather than warn.

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb
             FROM pg_roles WHERE rolname IN ('app_user', 'app_migrator')
    LOOP
        IF r.rolsuper THEN
            RAISE EXCEPTION 'FATAL: % is a SUPERUSER. RLS would not apply.', r.rolname;
        END IF;
        IF r.rolbypassrls THEN
            RAISE EXCEPTION
                'FATAL: % has BYPASSRLS. Every tenant isolation policy in DB '
                '§8 would be inert while still appearing present.', r.rolname;
        END IF;
        IF r.rolcreaterole OR r.rolcreatedb THEN
            RAISE EXCEPTION 'FATAL: % holds CREATEROLE/CREATEDB.', r.rolname;
        END IF;
    END LOOP;

    IF (SELECT count(*) FROM pg_roles
        WHERE rolname IN ('app_user', 'app_migrator')) <> 2 THEN
        RAISE EXCEPTION 'FATAL: both app_user and app_migrator must exist.';
    END IF;

    -- 🔒 The DDL boundary, asserted rather than assumed.
    IF has_schema_privilege('app_user', 'public', 'CREATE') THEN
        RAISE EXCEPTION
            'FATAL: app_user holds CREATE on schema public — it has DDL rights '
            'it must not have (DB §2.4).';
    END IF;

    IF NOT has_schema_privilege('app_migrator', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'FATAL: app_migrator cannot create objects; migrations would fail.';
    END IF;

    RAISE NOTICE 'Roles verified: app_user (CRUD, RLS enforced, no DDL), app_migrator (DDL).';
END
$$;

COMMIT;
