"""The boundary checker must fail on deliberate violations — S0 DoD.

🔒 Arch §3.5 and the S0 Definition of Done: *"Boundary checker tested against
deliberate violations (must fail)"* and *"Boundary checker **fails** CI on an
intentional cross-module import"*.

A linter that has never been seen to fail is indistinguishable from one that
cannot. These tests are the reason the checker can be trusted, so every rule
gets both directions: a violating tree that must be rejected, and a legal tree
that must be accepted. The false-negative half matters as much as the other —
a checker that fires on correct code gets disabled within a week, and then
nothing is enforced at all.

Trees are synthesised in ``tmp_path`` rather than asserted against the real
codebase, so a test never has to be edited because production code moved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ─── Load the tool ───────────────────────────────────────────────────────
# tools/ is deliberately not an importable package — it holds scripts, not
# application code, and app/ must never be able to import from it.

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "check_boundaries.py"
_spec = importlib.util.spec_from_file_location("check_boundaries", _TOOL_PATH)
assert _spec is not None and _spec.loader is not None
boundaries = importlib.util.module_from_spec(_spec)
sys.modules["check_boundaries"] = boundaries
_spec.loader.exec_module(boundaries)


# ─── Helpers ─────────────────────────────────────────────────────────────


def write(root: Path, relative: str, body: str) -> Path:
    """Create a Python file, with the package ``__init__.py`` files it needs."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")

    directory = path.parent
    while directory != root.parent:
        init = directory / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
        directory = directory.parent
    return path


def rules_fired(app_root: Path) -> list[str]:
    """Run the backend half of the checker and return the rules that fired."""
    sources, failures = boundaries.parse_backend(app_root)
    violations = [
        *failures,
        *boundaries.check_imports(sources, app_root.name),
        *boundaries.check_tables(sources),
    ]
    return sorted({v.rule for v in violations})


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """An empty ``app`` package root, named so the dotted prefix is ``app``."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    return root


# ─── R1 — kernel must not import a module ────────────────────────────────


def test_r1_kernel_importing_a_module_fails(app: Path) -> None:
    write(app, "kernel/authz.py", "from app.modules.clients import ClientService\n")
    write(app, "modules/clients/__init__.py", "ClientService = object\n")

    assert "R1" in rules_fired(app)


def test_r1_module_importing_the_kernel_is_legal(app: Path) -> None:
    """The permitted direction. Modules depend on the kernel, never the reverse."""
    write(app, "kernel/authz.py", "def can() -> bool:\n    return False\n")
    write(app, "modules/clients/service.py", "from app.kernel.authz import can\n")

    assert rules_fired(app) == []


# ─── R2 — only a module's __init__.py may be imported ────────────────────


def test_r2_importing_module_internals_fails(app: Path) -> None:
    """The importer is the kernel-free HTTP layer, so R3 cannot mask R2."""
    write(app, "modules/clients/service.py", "class ClientService: pass\n")
    write(app, "http/routes.py", "from app.modules.clients.service import ClientService\n")

    assert "R2" in rules_fired(app)


def test_r2_from_import_of_a_submodule_fails(app: Path) -> None:
    """``from pkg import submodule`` is the same violation in different syntax."""
    write(app, "modules/clients/service.py", "class ClientService: pass\n")
    write(app, "http/routes.py", "from app.modules.clients import service\n")

    assert "R2" in rules_fired(app)


def test_r2_importing_a_symbol_through_the_front_door_is_legal(app: Path) -> None:
    """🔒 The false-positive guard.

    ``from app.modules.clients import ClientService`` is indistinguishable from
    a submodule import in the AST. It is legal — and if the checker flagged it,
    every correct import in the codebase would be a violation and the tool would
    be switched off.
    """
    write(app, "modules/clients/__init__.py", "ClientService = object\n")
    write(app, "modules/clients/service.py", "class ClientService: pass\n")
    write(app, "http/routes.py", "from app.modules.clients import ClientService\n")

    assert rules_fired(app) == []


def test_r2_does_not_fire_within_the_owning_module(app: Path) -> None:
    """A module's own files import each other freely — that is just cohesion."""
    write(app, "modules/clients/models.py", "class Client: pass\n")
    write(app, "modules/clients/service.py", "from app.modules.clients.models import Client\n")

    assert rules_fired(app) == []


# ─── R3 — modules must not import each other at all ─────────────────────


def test_r3_cross_module_import_fails(app: Path) -> None:
    """🔒 The named S0 Definition of Done: an intentional cross-module import."""
    write(app, "modules/nutrition/__init__.py", "PlanService = object\n")
    write(app, "modules/clients/service.py", "from app.modules.nutrition import PlanService\n")

    assert "R3" in rules_fired(app)


def test_r3_fires_even_through_the_public_interface(app: Path) -> None:
    """R3 is stricter than R2 on purpose (Arch §3.1).

    Importing another module's ``__init__.py`` is legal for the HTTP layer and
    forbidden for a module. "Only through the published interface" is not
    enforceable with one developer; zero is.
    """
    write(app, "modules/messaging/__init__.py", "send = object\n")
    write(app, "modules/appointments/service.py", "from app.modules.messaging import send\n")

    fired = rules_fired(app)
    assert "R3" in fired
    assert "R2" not in fired, "a single mistake must be reported once, as R3"


def test_r3_plain_import_statement_fails(app: Path) -> None:
    write(app, "modules/nutrition/__init__.py", "")
    write(app, "modules/clients/service.py", "import app.modules.nutrition\n")

    assert "R3" in rules_fired(app)


def test_r3_relative_cross_module_import_fails(app: Path) -> None:
    """Ruff bans relative imports, but the checker must not depend on that.

    A bypassed lint step should not silently disable boundary enforcement.
    """
    write(app, "modules/nutrition/__init__.py", "PlanService = object\n")
    write(app, "modules/clients/service.py", "from ..nutrition import PlanService\n")

    assert "R3" in rules_fired(app)


# ─── R4 — integrations must not import modules ───────────────────────────


def test_r4_integration_importing_a_module_fails(app: Path) -> None:
    write(app, "modules/messaging/__init__.py", "Message = object\n")
    write(app, "integrations/whatsapp/adapter.py", "from app.modules.messaging import Message\n")

    assert "R4" in rules_fired(app)


def test_r4_integration_importing_the_kernel_is_legal(app: Path) -> None:
    """Adapters sit behind ports the kernel owns, so they may import it."""
    write(app, "kernel/notifications.py", "class SendPort: pass\n")
    write(
        app,
        "integrations/whatsapp/adapter.py",
        "from app.kernel.notifications import SendPort\n",
    )

    assert rules_fired(app) == []


# ─── R5 — only entry points import platform/ ─────────────────────────────


def test_r5_module_importing_platform_fails(app: Path) -> None:
    write(app, "platform/db.py", "def get_session() -> None: ...\n")
    write(app, "modules/clients/service.py", "from app.platform.db import get_session\n")

    assert "R5" in rules_fired(app)


def test_r5_kernel_importing_platform_fails(app: Path) -> None:
    write(app, "platform/config.py", "def get_settings() -> None: ...\n")
    write(app, "kernel/audit.py", "from app.platform.config import get_settings\n")

    assert "R5" in rules_fired(app)


def test_r5_entry_points_may_import_platform(app: Path) -> None:
    """``main.py`` and ``worker.py`` are the wiring; this is their whole job."""
    write(app, "platform/config.py", "def get_settings() -> None: ...\n")
    write(app, "main.py", "from app.platform.config import get_settings\n")
    write(app, "worker.py", "from app.platform.config import get_settings\n")

    assert rules_fired(app) == []


def test_r5_platform_may_import_the_kernel(app: Path) -> None:
    """The real codebase does this: platform/logging.py uses kernel.scrubbing."""
    write(app, "kernel/scrubbing.py", "def scrub_text(t: str) -> str:\n    return t\n")
    write(app, "platform/logging.py", "from app.kernel.scrubbing import scrub_text\n")

    assert rules_fired(app) == []


# ─── R6 — a module must not reference another module's tables ────────────


def test_r6_foreign_key_into_another_modules_table_fails(app: Path) -> None:
    """🔒 The rule that stops the boundary being bypassed one join at a time.

    An import is easy to respect and easy to avoid; a foreign key couples two
    schemas just as hard, and does it where no import checker would look.
    """
    write(
        app,
        "modules/clients/models.py",
        "class Client:\n    __tablename__ = 'client'\n",
    )
    write(
        app,
        "modules/nutrition/models.py",
        "from sqlalchemy import Column, ForeignKey\n\n"
        "class Plan:\n"
        "    __tablename__ = 'diet_plan'\n"
        "    client_id = Column(ForeignKey('client.id'))\n",
    )

    assert "R6" in rules_fired(app)


def test_r6_foreign_key_within_the_same_module_is_legal(app: Path) -> None:
    write(
        app,
        "modules/nutrition/models.py",
        "from sqlalchemy import Column, ForeignKey\n\n"
        "class Plan:\n"
        "    __tablename__ = 'diet_plan'\n\n"
        "class PlanItem:\n"
        "    __tablename__ = 'diet_plan_item'\n"
        "    plan_id = Column(ForeignKey('diet_plan.id'))\n",
    )

    assert rules_fired(app) == []


def test_r6_ignores_kernel_owned_tables(app: Path) -> None:
    """Kernel tables are shared infrastructure; every module may reference them."""
    write(app, "kernel/models.py", "class Tenant:\n    __tablename__ = 'tenant'\n")
    write(
        app,
        "modules/clients/models.py",
        "from sqlalchemy import Column, ForeignKey\n\n"
        "class Client:\n"
        "    __tablename__ = 'client'\n"
        "    tenant_id = Column(ForeignKey('tenant.id'))\n",
    )

    assert rules_fired(app) == []


def test_r6_detects_foreign_key_constraint_form(app: Path) -> None:
    """``ForeignKeyConstraint(['a'], ['other.id'])`` is the same coupling."""
    write(app, "modules/clients/models.py", "class Client:\n    __tablename__ = 'client'\n")
    write(
        app,
        "modules/nutrition/models.py",
        "from sqlalchemy import ForeignKeyConstraint\n\n"
        "class Plan:\n"
        "    __tablename__ = 'diet_plan'\n"
        "    __table_args__ = (ForeignKeyConstraint(['client_id'], ['client.id']),)\n",
    )

    assert "R6" in rules_fired(app)


# ─── R7 — supabase-js must be absent (ADR-02) ───────────────────────────


def test_r7_supabase_in_dependencies_fails(tmp_path: Path) -> None:
    """🔒 The explicit S0 DoD item, and the direct fix for V1's dual-backend auth."""
    frontend = tmp_path / "frontend"
    (frontend / "apps" / "practitioner").mkdir(parents=True)
    (frontend / "apps" / "practitioner" / "package.json").write_text(
        '{\n  "name": "practitioner",\n'
        '  "dependencies": {\n    "@supabase/supabase-js": "2.47.0"\n  }\n}\n',
        encoding="utf-8",
    )

    violations = boundaries.check_frontend_dependencies(frontend)

    assert [v.rule for v in violations] == ["R7"]
    assert violations[0].line > 0, "the report must point at the offending line"


def test_r7_detects_supabase_in_any_dependency_section(tmp_path: Path) -> None:
    """devDependencies is not a loophole — installable means importable."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"devDependencies": {"@supabase/auth-helpers-react": "0.5.0"}}\n',
        encoding="utf-8",
    )

    assert [v.rule for v in boundaries.check_frontend_dependencies(frontend)] == ["R7"]


def test_r7_clean_manifest_passes(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"dependencies": {"react": "18.3.1", "react-dom": "18.3.1"}}\n',
        encoding="utf-8",
    )

    assert boundaries.check_frontend_dependencies(frontend) == []


def test_r7_ignores_node_modules(tmp_path: Path) -> None:
    """A transitive copy on disk is not a declared dependency of ours."""
    frontend = tmp_path / "frontend"
    nested = frontend / "node_modules" / "@supabase" / "supabase-js"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text(
        '{"name": "@supabase/supabase-js", "dependencies": {"supabase-x": "1.0.0"}}\n',
        encoding="utf-8",
    )

    assert boundaries.check_frontend_dependencies(frontend) == []


def test_r7_reports_an_unparseable_manifest(tmp_path: Path) -> None:
    """Silence on a broken manifest would mean silence on a real violation."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{ not json", encoding="utf-8")

    assert [v.rule for v in boundaries.check_frontend_dependencies(frontend)] == ["R7"]


# ─── R8 — no business logic in components (NFR-068) ─────────────────────


def component(frontend: Path, relative: str, body: str) -> None:
    path = frontend / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_r8_component_importing_the_api_client_fails(tmp_path: Path) -> None:
    """🔒 V1's most damaging failure, caught by its tell."""
    frontend = tmp_path / "frontend"
    component(
        frontend,
        "apps/practitioner/src/components/PlanCard.tsx",
        "import { getPlan } from '@wellnesscrm/api-client';\n"
        "export const PlanCard = () => <div />;\n",
    )

    assert [v.rule for v in boundaries.check_components(frontend)] == ["R8"]


def test_r8_component_reaching_into_a_feature_fails(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    component(
        frontend,
        "apps/practitioner/src/components/Totals.tsx",
        "import { computeTotals } from '../../features/nutrition/totals';\n",
    )

    assert [v.rule for v in boundaries.check_components(frontend)] == ["R8"]


def test_r8_component_calling_fetch_fails(tmp_path: Path) -> None:
    """Arch §4.4: a component may not fetch its own data."""
    frontend = tmp_path / "frontend"
    component(
        frontend,
        "apps/client-pwa/src/components/Today.tsx",
        "export const Today = () => {\n"
        "  const load = () => fetch('/api/v1/portal/today');\n"
        "  return <button onClick={load} />;\n"
        "};\n",
    )

    assert [v.rule for v in boundaries.check_components(frontend)] == ["R8"]


def test_r8_presentational_component_passes(tmp_path: Path) -> None:
    """The shape every feature component should have: props in, markup out."""
    frontend = tmp_path / "frontend"
    component(
        frontend,
        "apps/practitioner/src/components/PlanCard.tsx",
        "import { Card } from '@wellnesscrm/design-system';\n"
        "import type { Plan } from '../types';\n\n"
        "export const PlanCard = ({ plan }: { plan: Plan }) => <Card>{plan.title}</Card>;\n",
    )

    assert boundaries.check_components(frontend) == []


def test_r8_exempts_the_design_system(tmp_path: Path) -> None:
    """Primitives have no domain to reach for, and `fetch` in a doc example is not logic."""
    frontend = tmp_path / "frontend"
    component(
        frontend,
        "packages/design-system/src/components/Table.tsx",
        "// Usage: rows are fetched by the caller, e.g. fetch('/api/v1/app/clients')\n"
        "export const Table = () => <table />;\n",
    )

    assert boundaries.check_components(frontend) == []


def test_r8_ignores_feature_hooks(tmp_path: Path) -> None:
    """A hook is exactly where data fetching belongs — it must not be flagged."""
    frontend = tmp_path / "frontend"
    component(
        frontend,
        "apps/practitioner/src/features/plans/usePlan.ts",
        "import { getPlan } from '@wellnesscrm/api-client';\n"
        "export const usePlan = (id: string) => getPlan(id);\n",
    )

    assert boundaries.check_components(frontend) == []


# ─── The tool as CI uses it ──────────────────────────────────────────────


def test_main_exits_zero_on_the_real_repository() -> None:
    """🔒 The checker must pass on committed code.

    If this fails, either the codebase has a genuine boundary violation or the
    checker has a false positive. Both are release-blocking, and the difference
    is not something to discover during a deploy.
    """
    backend = Path(__file__).resolve().parents[1]
    exit_code = boundaries.main(
        [
            "--backend",
            str(backend / "app"),
            "--frontend",
            str(backend.parent / "frontend"),
            "--quiet",
        ]
    )

    assert exit_code == 0


def test_main_exits_nonzero_and_names_the_rule(
    app: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure must be actionable: the rule, the location and the remedy."""
    write(app, "modules/nutrition/__init__.py", "PlanService = object\n")
    write(app, "modules/clients/service.py", "from app.modules.nutrition import PlanService\n")

    exit_code = boundaries.main(["--backend", str(app), "--frontend", str(app / "nonexistent")])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "R3" in captured.err
    assert "service.py" in captured.err
    assert "Arch" in captured.err, "the message must point at the governing document"


def test_a_syntax_error_is_reported_not_raised(app: Path) -> None:
    """CI must fail with a filename, not a traceback from the checker itself."""
    write(app, "modules/clients/service.py", "def broken(:\n")

    _, failures = boundaries.parse_backend(app)

    assert [f.rule for f in failures] == ["R0"]
    assert "service.py" in str(failures[0].path)


def test_every_rule_has_a_statement() -> None:
    """The report prints ``RULES[rule]``; a missing entry would print nothing."""
    assert set(boundaries.RULES) == {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"}
