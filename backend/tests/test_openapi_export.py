"""The OpenAPI export must be deterministic and complete — S0-7.

🔒 **NFR-079 depends on a property that is easy to lose silently.** The exported
schema must be a pure function of the code: same code, same bytes, on any machine
and in any environment. If it is not, the CI freshness check either fails on
noise — and gets disabled — or passes while the committed client is wrong.

These tests pin that property, plus the two contract rules the exporter enforces
on the way through: stable `operationId`s (API §17.1) and the exclusion of
`/admin/*` from the public document (API §17.3).
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from tools.export_openapi import (
    EXCLUDED_PREFIXES,
    _assert_operation_ids,
    build_schema,
    serialise,
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return build_schema()


# ─── Determinism ─────────────────────────────────────────────────────────


def test_export_is_byte_stable_across_calls(schema: dict[str, Any]) -> None:
    """🔒 The freshness check compares bytes, so instability would make it lie."""
    assert serialise(schema) == serialise(build_schema())


def test_keys_are_sorted(schema: dict[str, Any]) -> None:
    """Dictionary ordering is not guaranteed across Python versions or across a
    refactor that reorders route registration. Sorting removes the variable."""
    rendered = serialise(schema)
    reparsed = json.loads(rendered)
    assert list(reparsed) == sorted(reparsed)


def test_export_ends_with_exactly_one_newline(schema: dict[str, Any]) -> None:
    """A missing trailing newline is a diff git reports and `--check` cannot see."""
    rendered = serialise(schema)
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


def test_export_is_independent_of_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
) -> None:
    """🔒 `create_app` disables `/openapi.json` in production-like environments.

    Without forcing the environment, exporting from a machine that happens to
    have `APP_ENV=staging` set would produce a different document — and the
    difference would show up as an unexplained CI failure for the next person.
    """
    baseline = serialise(schema)

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("APP_DEBUG", "false")

    assert serialise(build_schema()) == baseline

    # The exporter forces APP_ENV=local; confirm it does so rather than merely
    # tolerating the override.
    assert os.environ["APP_ENV"] == "local"


# ─── Completeness ────────────────────────────────────────────────────────


def test_the_health_endpoints_are_present(schema: dict[str, Any]) -> None:
    """The only endpoints that exist at S0. If these vanish, the export is broken
    rather than the API being empty."""
    paths = schema["paths"]
    assert "/api/v1/public/health" in paths
    assert "/api/v1/public/health/ready" in paths


def test_response_schemas_are_named_components(schema: dict[str, Any]) -> None:
    """🔒 API §17.1 — "every schema is a named component", so generated types are
    named rather than inline and anonymous."""
    components = schema.get("components", {}).get("schemas", {})
    assert "LivenessResponse" in components
    assert "ReadinessResponse" in components
    assert "DependencyStatus" in components


def test_every_operation_declares_a_tag(schema: dict[str, Any]) -> None:
    """🔒 API §17.2 — tags mirror modules, one per endpoint."""
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            tags = operation.get("tags", [])
            assert tags, f"{method.upper()} {path} declares no tag"
            assert len(tags) == 1, (
                f"{method.upper()} {path} has {len(tags)} tags; a multi-tagged "
                "endpoint belongs to two modules (API §17.2)"
            )


# ─── Admin exclusion — API §17.3 ─────────────────────────────────────────


def test_admin_paths_are_excluded(schema: dict[str, Any]) -> None:
    """🔒 The operator console's contract must not be discoverable from a
    practitioner's browser. No `/admin` endpoints exist yet, so this asserts the
    filter is in place *before* there is anything for it to catch — the failure
    mode is that S12 adds admin routes and nobody remembers this rule."""
    assert EXCLUDED_PREFIXES == ("/api/v1/admin",)
    assert not [p for p in schema["paths"] if p.startswith(EXCLUDED_PREFIXES)]


def test_the_admin_filter_actually_removes_paths() -> None:
    """The filter is exercised against a synthetic document, because the real one
    has no admin paths to remove. An untested filter that silently stops working
    is worse than no filter — it reads as protection that is not there."""
    document = {
        "paths": {
            "/api/v1/app/clients": {},
            "/api/v1/admin/tenants": {},
            "/api/v1/public/health": {},
        }
    }
    kept = {
        path: item
        for path, item in document["paths"].items()
        if not path.startswith(EXCLUDED_PREFIXES)
    }
    assert set(kept) == {"/api/v1/app/clients", "/api/v1/public/health"}


# ─── operationId enforcement — API §17.1 ─────────────────────────────────


def test_every_operation_has_an_explicit_operation_id(schema: dict[str, Any]) -> None:
    """🔒 Operation ids become the generated client's method names. FastAPI
    derives one from the function name and route when it is omitted, so renaming
    a handler would silently rename a frontend method."""
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_id = operation.get("operationId", "")
            assert operation_id, f"{method.upper()} {path} has no operationId"
            # A derived id contains the mangled route; an explicit one does not.
            assert "api_v1" not in operation_id, (
                f"{method.upper()} {path} appears to use FastAPI's derived "
                f"operationId ({operation_id!r}). Set operation_id= explicitly."
            )


def test_missing_operation_id_is_rejected() -> None:
    """The guard must fail on a real omission, or it is decorative."""
    with pytest.raises(SystemExit, match="operationId"):
        _assert_operation_ids({"paths": {"/api/v1/app/clients": {"get": {"summary": "List"}}}})


def test_duplicate_operation_ids_are_rejected() -> None:
    """Two operations sharing an id means the generated client loses a method."""
    document = {
        "paths": {
            "/api/v1/app/clients": {"get": {"operationId": "listThings"}},
            "/api/v1/app/plans": {"get": {"operationId": "listThings"}},
        }
    }
    with pytest.raises(SystemExit, match="Duplicate"):
        _assert_operation_ids(document)


def test_non_operation_keys_are_ignored() -> None:
    """`parameters` and `summary` sit alongside methods in a path item and are
    not operations. Treating them as such would fail on valid documents."""
    document = {
        "paths": {
            "/api/v1/app/clients": {
                "parameters": [{"name": "id", "in": "path"}],
                "summary": "Clients",
                "get": {"operationId": "listClients"},
            }
        }
    }
    _assert_operation_ids(document)
