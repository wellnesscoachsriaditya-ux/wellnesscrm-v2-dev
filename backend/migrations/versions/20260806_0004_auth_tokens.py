"""Practitioner-realm one-time tokens — email verification and password reset.

Revision ID: 0004_auth_tokens
Revises: 0003_audit_infrastructure
Created: 2026-08-06

🔒 The DDR-04 pattern, applied to the practitioner realm: store a hash, never the
token; expire quickly; enforce single use with a conditional update rather than
an application check.

⚠️ **A schema addition beyond the approved DB design.** `magic_links` (§4.5)
covers the client realm and carries `client_id` and `tenant_id` — neither applies
to a practitioner verifying an email address before any tenant is usable, or
resetting a password on an archived account. Overloading that table would have
meant two nullable columns and a purpose enum spanning two realms, which is
exactly the shape that later produces a cross-realm redemption bug.

🔒 **No RLS** (Pattern D). The rows are presented by callers who are not yet
authenticated, so there is no tenant in scope to isolate against; isolation is
the token's own 256 bits of entropy. Consistent with `sessions`, which is a
platform table for the same reason.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_auth_tokens"
down_revision: str | None = "0003_audit_infrastructure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ENUMS: dict[str, tuple[str, ...]] = {
    # 🔒 Stored and checked on redemption. Without it, a token issued to prove
    # mailbox ownership would be redeemable at the password-reset endpoint —
    # account takeover using a link the product itself sent.
    "auth_token_purpose": ("email_verification", "password_reset"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Reference an existing enum type without re-emitting its DDL (see 0002)."""
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    for name, values in _ENUMS.items():
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=False)

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # 🔒 The identity provider's handle, not `users.id`. Verification precedes
        # a usable account and reset must work for an archived one; both are
        # credential operations, and credentials are the provider's (NFR-029).
        sa.Column("auth_subject_id", sa.Text(), nullable=False),
        # 🔒 DDR-04 — the hash. A database disclosure yields no usable token.
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("purpose", _enum("auth_token_purpose"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # 🔒 Single use. Redemption is `UPDATE ... WHERE consumed_at IS NULL`,
        # so two concurrent redemptions of the same token cannot both win — the
        # database decides, not a read-then-write in application code.
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_auth_tokens__token_hash"),
    )

    # "Does this account already have a live token of this purpose?" — the
    # re-request path, which must not mint an unbounded number of valid tokens.
    op.create_index(
        "ix_auth_tokens__subject_purpose", "auth_tokens", ["auth_subject_id", "purpose"]
    )
    # Supports the expiry sweep (NFR-049): consumed and stale rows are deleted.
    op.create_index("ix_auth_tokens__expires", "auth_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_tokens__expires", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens__subject_purpose", table_name="auth_tokens")
    op.drop_table("auth_tokens")

    for name in _ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=False)
