"""Tests for the SECURITY DEFINER functions used for public identity routing.

These tests run against a live database to prove that the boundaries defined in
0022_s1_identity_security_definer hold at the database level.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.isolation


async def test_anonymous_cannot_directly_select_users(app_engine: AsyncEngine) -> None:
    """app_user without a tenant scope cannot read the users table directly."""
    async with app_engine.connect() as connection:
        # We intentionally do not call _scope_to()
        rows = (await connection.execute(text("SELECT count(*) FROM users"))).scalar()
        assert rows == 0, "app_user without tenant_id was able to read users"


async def test_anonymous_cannot_directly_select_magic_links(app_engine: AsyncEngine) -> None:
    """app_user without a tenant scope cannot read the magic_links table directly."""
    async with app_engine.connect() as connection:
        rows = (await connection.execute(text("SELECT count(*) FROM magic_links"))).scalar()
        assert rows == 0, "app_user without tenant_id was able to read magic_links"


async def test_identity_lookup_by_email_exposes_only_subject(app_engine: AsyncEngine) -> None:
    """The SECURITY DEFINER function returns ONLY the auth_subject_id."""
    async with app_engine.connect() as connection:
        # We pass a nonexistent email to test the signature
        result = await connection.execute(
            text("SELECT * FROM identity_lookup_by_email('doesnotexist@example.com')")
        )
        keys = list(result.keys())
        assert keys == ["auth_subject_id"], f"identity_lookup_by_email returned unexpected columns: {keys}"


async def test_identity_lookup_by_subject_exposes_minimal_data(app_engine: AsyncEngine) -> None:
    """The SECURITY DEFINER function returns exactly what the token pipeline needs."""
    async with app_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT * FROM identity_lookup_by_subject('fake-subject')")
        )
        keys = list(result.keys())
        assert keys == ["user_id", "tenant_id", "role", "status", "archived_at"], f"identity_lookup_by_subject returned unexpected columns: {keys}"


async def test_malformed_inputs_handled_safely(app_engine: AsyncEngine) -> None:
    """SQL injection attempts or invalid types fail safely."""
    async with app_engine.connect() as connection:
        # Email lookup should just return 0 rows for weird inputs
        result = (await connection.execute(
            text("SELECT count(*) FROM identity_lookup_by_email('invalid'' OR 1=1;--')")
        )).scalar()
        assert result == 0


async def test_app_user_can_execute_definer(app_engine: AsyncEngine) -> None:
    """Verify that app_user actually has EXECUTE permission on these functions."""
    async with app_engine.connect() as connection:
        # If permission is denied, this will throw an exception
        await connection.execute(text("SELECT * FROM identity_lookup_by_email('test')"))
        await connection.execute(text("SELECT * FROM identity_lookup_by_subject('test')"))
        # We also test the others to ensure no permission denied errors
        await connection.execute(text("SELECT * FROM identity_consume_magic_link('fake', now())"))
        await connection.execute(text("SELECT * FROM identity_consume_auth_token('fake', 'email_verification', now())"))
