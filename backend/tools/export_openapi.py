#!/usr/bin/env python3
"""Export the OpenAPI schema to a file — the first half of NFR-079.

🔒 **Arch §4.5 / API §16.2** — ``frontend/packages/api-client`` is generated from
the backend's OpenAPI document, never hand-edited, and CI fails if it is stale.
A backend contract change therefore breaks the frontend *build* rather than
production.

That guarantee needs a committed artefact both sides can compare against, and
this script produces it: ``frontend/packages/api-client/openapi.json``. The
TypeScript generation that consumes it lives on the frontend side
(``npm run generate:client``), because that is where the toolchain is.

⚠️ **Why a file rather than a running server.** Generating from ``localhost:8000``
would make the frontend build depend on a database, a port and a process. The
schema is a pure function of the code — ``create_app()`` builds it without any
I/O — so it is exported statically and committed. CI can then verify freshness
with no Python at all, which matters because the frontend job has no Python.

🔒 **Admin endpoints are excluded** (API §17.3). ``/admin/*`` is served as a
separate document and must not be discoverable from a practitioner's browser.
The filter is applied here, at the source, rather than trusted to whoever writes
the generation command later.

Usage::

    python tools/export_openapi.py            # write the schema
    python tools/export_openapi.py --check    # verify it is current, write nothing
    python tools/export_openapi.py --stdout   # print, write nothing

Exit code 0 on success, 1 if ``--check`` finds the committed file stale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = _REPO / "frontend" / "packages" / "api-client" / "openapi.json"

# 🔒 API §17.3 — the operator console's contract is not part of the public
# document. Paths are matched after the /api/v1 prefix.
EXCLUDED_PREFIXES: tuple[str, ...] = ("/api/v1/admin",)


def build_schema() -> dict[str, Any]:
    """Return the OpenAPI document for the public realms.

    Settings are forced to a known-safe shape before the app is imported.
    ``create_app`` disables ``/openapi.json`` in production-like environments,
    so exporting from a machine with ``APP_ENV=staging`` in its environment
    would otherwise produce a schema that silently differs from the developer's.
    The document must be a function of the *code*, not of who ran it.
    """
    os.environ["APP_ENV"] = "local"
    os.environ["APP_DEBUG"] = "true"

    # Imported here, after the environment is set: `app.main` reads settings at
    # import time, and `get_settings` is cached for the process lifetime.
    from app.main import create_app
    from app.platform.config import get_settings

    get_settings.cache_clear()
    schema = create_app().openapi()

    schema["paths"] = {
        path: item
        for path, item in schema.get("paths", {}).items()
        if not path.startswith(EXCLUDED_PREFIXES)
    }

    _assert_operation_ids(schema)
    return schema


def _assert_operation_ids(schema: dict[str, Any]) -> None:
    """🔒 API §17.1 — every endpoint needs a stable, unique ``operationId``.

    Operation ids become the generated client's method names. FastAPI invents
    one from the function name and route when it is omitted, which produces
    names like ``read_client_api_v1_app_clients__id__get`` — and, worse, changes
    them when a function is renamed or a route is moved. Both are silent
    frontend breaking changes, so they are caught here rather than in review.
    """
    missing: list[str] = []
    seen: dict[str, str] = {}
    duplicates: list[str] = []

    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            if method not in {"get", "put", "post", "delete", "patch", "head", "options"}:
                continue
            where = f"{method.upper()} {path}"
            operation_id = operation.get("operationId")
            if not operation_id:
                missing.append(where)
                continue
            if operation_id in seen:
                duplicates.append(f"{operation_id!r}: {seen[operation_id]} and {where}")
            else:
                seen[operation_id] = where

    problems: list[str] = []
    if missing:
        problems.append(
            "Endpoints without an explicit operationId (API §17.1):\n  "
            + "\n  ".join(sorted(missing))
            + '\n\nAdd `operation_id="..."` to the route decorator. Without it '
            "FastAPI derives a name from the function and path, so renaming "
            "either silently breaks the generated client."
        )
    if duplicates:
        problems.append(
            "Duplicate operationIds — the generated client would lose a method:\n  "
            + "\n  ".join(sorted(duplicates))
        )

    if problems:
        raise SystemExit("\n\n".join(problems))


def serialise(schema: dict[str, Any]) -> str:
    """Render the schema deterministically.

    🔒 Byte-for-byte stability is what makes the freshness check meaningful.
    ``sort_keys`` defeats dictionary ordering changes between Python versions,
    and the trailing newline keeps the file POSIX-clean so git does not report a
    diff that ``--check`` cannot see.
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the OpenAPI schema (NFR-079).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the file on disk differs from the current schema.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args(argv)

    rendered = serialise(build_schema())

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    output: Path = args.output.resolve()

    if args.check:
        if not output.is_file():
            print(f"OpenAPI schema has never been exported: {output}", file=sys.stderr)
            print("Run: python tools/export_openapi.py", file=sys.stderr)
            return 1
        if output.read_text(encoding="utf-8") != rendered:
            print(
                f"OpenAPI schema is stale: {output.relative_to(_REPO).as_posix()}\n\n"
                "The API has changed since the schema was last exported. Run:\n"
                "  cd backend && python tools/export_openapi.py\n"
                "  cd frontend && npm run generate:client\n"
                "and commit both files with the change that caused them.",
                file=sys.stderr,
            )
            return 1
        print(f"OpenAPI schema is current — {len(rendered.splitlines())} lines.")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" — otherwise Windows writes CRLF and the file's hash differs
    # from the one CI computes on Linux, failing freshness for no real reason.
    output.write_text(rendered, encoding="utf-8", newline="\n")

    paths = len(json.loads(rendered).get("paths", {}))
    print(f"Wrote {output.relative_to(_REPO).as_posix()} — {paths} path(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
