"""Alembic environment — the one place DDL connects to the database.

🔒 **DB §2.4.** Migrations run as ``app_migrator``: DDL rights, no runtime use.
The application runs as ``app_user``: CRUD only, no DDL, and critically no
``BYPASSRLS``. Keeping them apart is what stops a runtime bug from being able to
alter the schema, and what stops a migration credential from sitting in the web
process's environment.

The URL comes from :func:`app.platform.config.get_settings` rather than from
``alembic.ini`` — one typed, validated definition of configuration (NFR-075),
and no secret in source control (NFR-034).

⚠️ ``Base.metadata`` is imported for ``--autogenerate``. It is empty at S0: the
platform-core tables (D0) land in S1. Autogenerate against an empty metadata
would propose dropping every table, so **do not run it until models exist** —
and always read the generated diff before applying it. Alembic writes what it
infers, which is not always what was meant.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.platform.config import get_settings
from app.platform.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares against this. Models register themselves on it by
# subclassing `Base`; each module's `models.py` must therefore be imported here
# once it exists, or autogenerate will not see its tables.
target_metadata = Base.metadata


def _migration_url() -> str:
    """Return the DDL connection string, escaped for ConfigParser.

    ⚠️ ``%`` is ConfigParser's interpolation character, and a URL-encoded
    password (``%40`` for ``@``) is common. Unescaped, it raises deep inside
    Alembic with an error that names neither the password nor the cause.
    """
    return get_settings().migration_url.replace("%", "%%")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it.

    Used to review exactly what a migration will do before it touches a real
    database — the reviewable artefact for anything running against production.
    """
    context.configure(
        url=_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Without this, a column type change is invisible to autogenerate.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Execute migrations against the database.

    🔒 One transaction for the whole run: a failed migration leaves the schema
    exactly as it was. PostgreSQL supports transactional DDL, so a half-applied
    migration — the thing that turns a five-minute deploy into an outage — is
    simply not a state this can reach.
    """
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _migration_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # 🔒 The version table is owned by `app_migrator`. `app_user` is
            # revoked from it in the baseline revision — the application has no
            # business reading, let alone rewriting, its own schema history.
            version_table="alembic_version",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
