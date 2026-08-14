"""Tests for the public identity routes to ensure they work without RLS errors.

These tests run against a live database using the actual FastAPI application,
proving that ANONYMOUS_SCOPE can successfully call the SECURITY DEFINER functions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.isolation


@pytest.fixture
def client() -> TestClient:
    """The real application, configured for testing."""
    # We import here to avoid loading the app before fixtures are ready
    from app.main import create_app
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


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
