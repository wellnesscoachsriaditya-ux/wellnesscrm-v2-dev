"""s1_identity_security_definer

Revision ID: 0022_s1_identity_security_definer
Revises: 0021_messaging
Created: 2026-08-14 21:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_s1_sec_definer"
down_revision: str | None = "0021_messaging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 🔒 1. identity_lookup_by_subject
    op.execute(
        """
        CREATE OR REPLACE FUNCTION identity_lookup_by_subject(p_subject_id text)
        RETURNS TABLE (
            user_id uuid,
            tenant_id uuid,
            role user_role,
            status user_status,
            archived_at timestamptz
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id AS user_id, tenant_id, role, status, archived_at
            FROM users
            WHERE auth_subject_id = p_subject_id;
        $$;
        
        REVOKE ALL ON FUNCTION identity_lookup_by_subject(text) FROM public;
        GRANT EXECUTE ON FUNCTION identity_lookup_by_subject(text) TO app_user;
        """
    )

    # 🔒 2. identity_lookup_by_email
    op.execute(
        """
        CREATE OR REPLACE FUNCTION identity_lookup_by_email(p_email text)
        RETURNS TABLE (auth_subject_id text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT auth_subject_id
            FROM users
            WHERE email = p_email;
        $$;

        REVOKE ALL ON FUNCTION identity_lookup_by_email(text) FROM public;
        GRANT EXECUTE ON FUNCTION identity_lookup_by_email(text) TO app_user;
        """
    )

    # 🔒 3. identity_consume_magic_link
    op.execute(
        """
        CREATE OR REPLACE FUNCTION identity_consume_magic_link(p_token_hash text, p_now timestamptz)
        RETURNS TABLE (
            tenant_id uuid,
            client_id uuid,
            purpose link_purpose,
            target_ref text
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            UPDATE magic_links
            SET consumed_at = p_now
            WHERE token_hash = p_token_hash
              AND consumed_at IS NULL
              AND expires_at > p_now
            RETURNING tenant_id, client_id, purpose, target_ref;
        $$;

        REVOKE ALL ON FUNCTION identity_consume_magic_link(text, timestamptz) FROM public;
        GRANT EXECUTE ON FUNCTION identity_consume_magic_link(text, timestamptz) TO app_user;
        """
    )

    # 🔒 4. identity_consume_auth_token
    op.execute(
        """
        CREATE OR REPLACE FUNCTION identity_consume_auth_token(p_token_hash text, p_purpose auth_token_purpose, p_now timestamptz)
        RETURNS TABLE (auth_subject_id text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            UPDATE auth_tokens
            SET consumed_at = p_now
            WHERE token_hash = p_token_hash
              AND purpose = p_purpose
              AND consumed_at IS NULL
              AND expires_at > p_now
            RETURNING auth_subject_id;
        $$;

        REVOKE ALL ON FUNCTION identity_consume_auth_token(text, auth_token_purpose, timestamptz) FROM public;
        GRANT EXECUTE ON FUNCTION identity_consume_auth_token(text, auth_token_purpose, timestamptz) TO app_user;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS identity_consume_auth_token(text, auth_token_purpose, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS identity_consume_magic_link(text, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS identity_lookup_by_email(text)")
    op.execute("DROP FUNCTION IF EXISTS identity_lookup_by_subject(text)")
