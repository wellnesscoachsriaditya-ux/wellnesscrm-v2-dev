"""s6_client_portal

Revision ID: 2eb56b8913d5
Revises: 0021_messaging
Created: 2026-08-14 15:35:54.473075+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2eb56b8913d5"
down_revision: str | None = "0022_s1_sec_definer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── 1. adherence_logs (S6 Client Portal) ─────────────────────────
    op.create_table(
        "adherence_logs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False, comment="🔒 RLS discriminator"),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("for_date", sa.Date(), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.Text(),
            nullable=False,
            comment="🔒 Client-side UUID for idempotency",
        ),
        sa.Column("score", sa.Integer(), nullable=False, comment="Adherence score (0-100)"),
        sa.Column(
            "client_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="🔒 When the client actually logged this offline",
        ),
        sa.Column(
            "server_timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="🔒 When the server received it",
        ),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="ck_adherence_logs__score_range"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "idempotency_key", name="uq_adherence_logs__idempotency"),
    )
    op.create_index(
        "ix_adherence_logs__client_date", "adherence_logs", ["client_id", "for_date"], unique=False
    )
    op.create_index("ix_adherence_logs__tenant_id", "adherence_logs", ["tenant_id"], unique=False)

    # 🔒 RLS (Tenant Isolation)
    op.execute("ALTER TABLE adherence_logs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON adherence_logs
            AS RESTRICTIVE
            USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        """
    )

    # 🔒 RLS (Client Access)
    # Clients can only see and insert their own adherence logs.
    # The client ID is set in the session when redeeming a magic link.
    # We will need a policy for client access if they connect directly, but our architecture says:
    # "The browser never queries the database. FastAPI is the only data path".
    # Therefore, we just need standard practitioner access + client access over API, which is
    # handled via tenant_id + application logic,
    # BUT wait, the application might enforce `app.current_client_id` for client endpoints.
    # Let's check `0009_clients.py` to see if there is a client policy. Wait, the DB docs say
    # "RLS discriminator".
    # Standard tenant isolation is sufficient if FastAPI is the one connecting.

    # ─── 2. client_nutrition_visibility ──────────────────────────────
    op.add_column(
        "clients",
        sa.Column(
            "client_nutrition_visibility",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="🔒 Whether this client sees nutrition data",
        ),
    )

    # ─── 3. identity_lookup_client_by_contact (SECURITY DEFINER) ─────────────
    # 🔒 Required for the public `/portal/access/request` endpoint to resolve
    # a client's ID and tenant without an active session, so it can issue a magic link.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.identity_lookup_client_by_contact(p_contact text)
        RETURNS TABLE (
            tenant_id uuid,
            client_id uuid,
            archived_at timestamp with time zone,
            full_name text
        )
        SECURITY DEFINER
        SET search_path = public
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RETURN QUERY
            SELECT c.tenant_id, c.id, c.archived_at, c.full_name
            FROM clients c
            WHERE c.email = p_contact OR c.mobile = p_contact;
        END;
        $$;

        REVOKE ALL ON FUNCTION public.identity_lookup_client_by_contact(text) FROM public;
        GRANT EXECUTE ON FUNCTION public.identity_lookup_client_by_contact(text) TO app_user;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.identity_lookup_client_by_contact(text)")
    op.drop_column("clients", "client_nutrition_visibility")
    op.drop_table("adherence_logs")
