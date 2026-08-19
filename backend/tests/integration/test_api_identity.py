"""Tests for the public identity routes to ensure they work without RLS errors.

These tests run against a live database using the actual FastAPI application,
proving that ANONYMOUS_SCOPE can successfully call the SECURITY DEFINER functions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.kernel.tenancy import TenantScope
from app.platform.http.pipeline import (
    configure_transaction_provider,
    get_transaction_provider,
)

pytestmark = pytest.mark.isolation


@pytest_asyncio.fixture
async def client(app_engine: AsyncEngine) -> AsyncIterator[TestClient]:
    """The real application, wired to use the test database engine.

    The pipeline's ``PublicRoute`` opens a transaction via
    ``get_transaction_provider()``.  To make public routes use the test
    database (which has the SECURITY DEFINER functions from migration 0022),
    we install a test transaction provider that creates sessions from
    ``app_engine`` instead of the application's own engine.
    """
    from app.main import create_app

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    original_provider = get_transaction_provider()

    @asynccontextmanager
    async def _test_provider(_scope: TenantScope) -> AsyncIterator[Any]:
        async with factory() as session, session.begin():
            yield session

    configure_transaction_provider(_test_provider)  # type: ignore[arg-type]
    app = create_app()
    yield TestClient(app, raise_server_exceptions=False)
    configure_transaction_provider(original_provider)


def test_public_registration_succeeds(client: TestClient) -> None:
    """Registration should not fail with an RLS error."""
    response = client.post(
        "/api/v1/public/auth/register",
        json={
            "email": "integration-test-register@example.com",
            "password": "correct-horse-battery-staple",
            "full_name": "Integration Test",
            "practice_name": "Integration Clinic",
            "mobile": "+15550100",
            "accepted_terms_version": "1.0",
        },
    )
    # Registration should return 201 Created (even if it's a duplicate, it returns 201)
    assert response.status_code == 201, f"Registration failed: {response.text}"


def test_public_login_fails_safely_for_unknown_user(client: TestClient) -> None:
    """Login should return 401 Unauthorized, not a 500 RLS error."""
    response = client.post(
        "/api/v1/public/auth/login",
        json={
            "email": "integration-unknown@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    # 401 means the credentials were rejected (or not found),
    # but the DB lookup successfully returned 0 rows instead of throwing an RLS exception.
    assert response.status_code == 401, f"Login failed unexpectedly: {response.text}"
    assert response.json()["error"]["type"] == "unauthenticated"


def test_public_password_reset_request_succeeds(client: TestClient) -> None:
    """Password reset should return 202 Accepted, not a 500 RLS error."""
    response = client.post(
        "/api/v1/public/auth/password-reset/request",
        json={
            "email": "integration-unknown@example.com",
        },
    )
    # 202 Accepted is the response for both known and unknown addresses.
    assert response.status_code == 202, f"Password reset request failed: {response.text}"
