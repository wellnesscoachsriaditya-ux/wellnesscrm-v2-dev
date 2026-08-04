#!/usr/bin/env python3
"""Architectural boundary enforcement — Arch §3.5.

🔒 Principle 3: the rules that matter are enforced by tooling, not discipline.

V1 failed on module boundaries specifically. The lesson recorded in Arch §3.1 is
that with one developer and no code review, *a permitted import is an import
that will happen everywhere*. Zero is enforceable; "only through the published
interface" is not. This script is what makes zero real.

It fails the build on:

===== ==================================================================
Rule   Violation
===== ==================================================================
R1     Kernel importing a module
R2     Import of a module's internals (``app.modules.x.service``)
R3     A module importing another module — at all
R4     An integration importing a module
R5     Anything importing ``platform/`` except an entry point
R6     An ORM model referencing another module's table
R7     ``supabase-js`` present in frontend dependencies (ADR-02)
R8     Business logic in a React component (NFR-068 heuristic)
===== ==================================================================

**Why a custom script rather than a library** (Arch §3.5) — AST inspection with
rules matching *our* module names, no dependency to maintain (NFR-078), and
failure messages that name the exact rule and the exact fix.

⚠️ R6 and R8 are the two approximate checks. R6 reads ``__tablename__``
declarations and ``ForeignKey`` string targets, which is how SQLAlchemy models
actually express cross-table references; it cannot see a raw SQL string. R8 is
explicitly a heuristic — it catches the *shape* of V1's worst failure (a
component reaching for domain code) rather than proving its absence. Both are
tripwires. Neither is a proof, and neither is an excuse to stop thinking.

Usage::

    python tools/check_boundaries.py                 # from backend/
    python tools/check_boundaries.py --backend app --frontend ../frontend

Exit code 0 clean, 1 on any violation.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Container, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

# ─── Rules ───────────────────────────────────────────────────────────────

RULES: Final[dict[str, str]] = {
    "R1": "Kernel MUST NOT import any module",
    "R2": "A module's internals MUST NOT be imported — only its __init__.py",
    "R3": "Modules MUST NOT import each other at all",
    "R4": "Integrations MUST NOT import modules",
    "R5": "Nothing imports platform/ except entry points",
    "R6": "A module MUST NOT read another module's tables",
    "R7": "supabase-js MUST be absent from frontend dependencies (ADR-02)",
    "R8": "Business logic MUST NOT live in a React component (NFR-068)",
}

# Directories that are never source.
# fmt: off — read as a list of names, not one per line.
_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".vite",
        "coverage",
        ".turbo",
        "htmlcov",
        "generated",
    }
)
# fmt: on


class Layer(Enum):
    """Which architectural layer a file belongs to."""

    KERNEL = "kernel"
    MODULE = "module"
    INTEGRATION = "integration"
    PLATFORM = "platform"
    ENTRY = "entry"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Violation:
    """One broken rule, with enough detail to fix it without investigating."""

    rule: str
    path: Path
    line: int
    detail: str
    remedy: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return (
            f"  {shown.as_posix()}:{self.line}\n" f"      {self.detail}\n" f"      → {self.remedy}"
        )


# ─── Python source model ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportEdge:
    """A single import, resolved to an absolute dotted name."""

    dotted: str
    line: int


@dataclass(frozen=True)
class PySource:
    """A parsed Python file and the facts we need from it."""

    path: Path
    dotted: str
    layer: Layer
    module_name: str | None  # the owning domain module, when layer is MODULE
    imports: tuple[ImportEdge, ...]
    tables: tuple[tuple[str, int], ...]  # (__tablename__, line)
    foreign_keys: tuple[tuple[str, int], ...]  # (referenced table, line)


def _walk_python(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _dotted_name(path: Path, root: Path) -> str:
    """``app/modules/clients/service.py`` → ``app.modules.clients.service``.

    The package prefix is the root directory's own name, so the checker works
    against a temporary tree in tests exactly as it does against ``backend/app``.
    """
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join([root.name, *parts])


def _classify(dotted: str, package: str) -> tuple[Layer, str | None]:
    """Map a dotted name onto its layer, and its module when it has one."""
    parts = dotted.split(".")
    if len(parts) < 2 or parts[0] != package:
        return Layer.UNKNOWN, None

    match parts[1]:
        case "kernel":
            return Layer.KERNEL, None
        case "platform":
            return Layer.PLATFORM, None
        case "modules":
            return Layer.MODULE, parts[2] if len(parts) > 2 else None
        case "integrations":
            return Layer.INTEGRATION, parts[2] if len(parts) > 2 else None
        case "main" | "worker":
            return Layer.ENTRY, None
        case _:
            return Layer.UNKNOWN, None


def _resolve_relative(node: ast.ImportFrom, dotted: str) -> str:
    """Resolve ``from ..x import y`` against the importing file's own package.

    Ruff bans relative imports across package roots (``ban-relative-imports``),
    but the boundary checker must not depend on another tool having run first —
    a bypassed lint step should not silently disable boundary enforcement.
    """
    package_parts = dotted.split(".")[:-1]  # the file's containing package
    upward = node.level - 1
    base = package_parts[: len(package_parts) - upward] if upward else package_parts
    tail = node.module.split(".") if node.module else []
    return ".".join([*base, *tail])


class _FileFacts(ast.NodeVisitor):
    """Collect imports, table declarations and foreign keys in one pass."""

    def __init__(self, dotted: str) -> None:
        self.dotted = dotted
        self.imports: list[ImportEdge] = []
        self.tables: list[tuple[str, int]] = []
        self.foreign_keys: list[tuple[str, int]] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast API)
        for alias in node.names:
            self.imports.append(ImportEdge(alias.name, node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        base = _resolve_relative(node, self.dotted) if node.level else (node.module or "")
        if not base:
            return
        self.imports.append(ImportEdge(base, node.lineno))
        # `from app.modules.clients import service` imports an internal just as
        # surely as `import app.modules.clients.service` does, so each imported
        # name is recorded as a candidate dotted path too.
        for alias in node.names:
            if alias.name != "*":
                self.imports.append(ImportEdge(f"{base}.{alias.name}", node.lineno))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "__tablename__"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                self.tables.append((node.value.value, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if name in {"ForeignKey", "ForeignKeyConstraint"}:
            for target in _string_constants(node.args):
                if "." in target:
                    self.foreign_keys.append((target.rsplit(".", 1)[0], node.lineno))
        self.generic_visit(node)


def _string_constants(nodes: Iterable[ast.expr]) -> Iterator[str]:
    """Yield string literals from arguments, including one level of list/tuple."""
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.List | ast.Tuple):
            yield from _string_constants(node.elts)


def parse_backend(root: Path) -> tuple[list[PySource], list[Violation]]:
    """Parse every Python file under ``root``.

    A syntax error is reported as a violation rather than raised: CI must fail
    with a message naming the file, not with a traceback from the checker.
    """
    sources: list[PySource] = []
    failures: list[Violation] = []

    for path in _walk_python(root):
        dotted = _dotted_name(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append(
                Violation(
                    rule="R0",
                    path=path,
                    line=exc.lineno or 0,
                    detail=f"File could not be parsed: {exc.msg}",
                    remedy="Fix the syntax error; boundaries cannot be checked until then.",
                )
            )
            continue

        facts = _FileFacts(dotted)
        facts.visit(tree)
        layer, module_name = _classify(dotted, root.name)
        sources.append(
            PySource(
                path=path,
                dotted=dotted,
                layer=layer,
                module_name=module_name,
                imports=tuple(facts.imports),
                tables=tuple(facts.tables),
                foreign_keys=tuple(facts.foreign_keys),
            )
        )

    return sources, failures


# ─── R1–R5: import boundaries ────────────────────────────────────────────


def check_imports(sources: Sequence[PySource], package: str) -> list[Violation]:
    """Enforce the dependency rule of Arch §3.1.

    One pass over every import edge. Each edge is judged once, and at most one
    violation is reported per import *statement* — ``from app.modules.clients
    import service`` yields two candidate edges (the package and the submodule),
    and reporting both would double-count a single mistake.

    Rule order is deliberate: the first rule that fires wins, so a module
    importing another module's internals is reported as R3 (the stricter, more
    informative rule) rather than as R2.
    """
    violations: list[Violation] = []
    known = {source.dotted for source in sources}

    for source in sources:
        for edge in source.imports:
            target_layer, target_module = _classify(edge.dotted, package)
            if target_layer is Layer.UNKNOWN:
                continue

            # R1 — the kernel is cross-cutting; a dependency on a domain module
            # inverts the layering and makes the kernel untestable alone.
            if source.layer is Layer.KERNEL and target_layer is Layer.MODULE:
                violations.append(
                    Violation(
                        rule="R1",
                        path=source.path,
                        line=edge.line,
                        detail=f"kernel imports `{edge.dotted}`",
                        remedy=(
                            "Invert the dependency: the module should depend on the "
                            "kernel. If the kernel needs data, declare a query port "
                            "(Arch §3.4b)."
                        ),
                    )
                )
                continue

            # R4 — integrations are adapters behind ports the kernel owns.
            if source.layer is Layer.INTEGRATION and target_layer is Layer.MODULE:
                violations.append(
                    Violation(
                        rule="R4",
                        path=source.path,
                        line=edge.line,
                        detail=f"integration imports `{edge.dotted}`",
                        remedy=(
                            "An adapter must not know its callers. Move the shared "
                            "type behind the port the module already depends on."
                        ),
                    )
                )
                continue

            # R3 — zero cross-module imports. Deliberately stricter than
            # "published interfaces only" (Arch §3.1).
            if (
                source.layer is Layer.MODULE
                and target_layer is Layer.MODULE
                and target_module is not None
                and source.module_name is not None
                and target_module != source.module_name
            ):
                violations.append(
                    Violation(
                        rule="R3",
                        path=source.path,
                        line=edge.line,
                        detail=(
                            f"module `{source.module_name}` imports "
                            f"module `{target_module}` (`{edge.dotted}`)"
                        ),
                        remedy=(
                            "Use a kernel domain event (Arch §3.4a), a kernel query "
                            "port (§3.4b), or orchestrate in the router (§3.4c) — "
                            "never inside a module service."
                        ),
                    )
                )
                continue

            # R2 — everyone else may import a module, but only its front door.
            if (
                target_layer is Layer.MODULE
                and target_module is not None
                and _is_module_internal(edge.dotted, package, known)
                and source.module_name != target_module
            ):
                violations.append(
                    Violation(
                        rule="R2",
                        path=source.path,
                        line=edge.line,
                        detail=f"imports module internals `{edge.dotted}`",
                        remedy=(
                            f"Import `{package}.modules.{target_module}` only. Its "
                            "__init__.py is the sole legal import surface (Arch §2.5)."
                        ),
                    )
                )
                continue

            # R5 — platform/ is framework wiring. Domain code that reaches into
            # it becomes untestable without the framework, and framework choices
            # stop being replaceable.
            if target_layer is Layer.PLATFORM and source.layer in {
                Layer.KERNEL,
                Layer.MODULE,
                Layer.INTEGRATION,
            }:
                violations.append(
                    Violation(
                        rule="R5",
                        path=source.path,
                        line=edge.line,
                        detail=f"{source.layer.value} imports `{edge.dotted}`",
                        remedy=(
                            "platform/ is imported by entry points only. Take the "
                            "dependency as an argument, or expose it through the "
                            "kernel."
                        ),
                    )
                )

    return _one_per_statement(violations)


def _one_per_statement(violations: Sequence[Violation]) -> list[Violation]:
    """Keep the first violation reported at each source location.

    ``from app.modules.clients import service`` produces two candidate edges on
    one line — the package and the submodule — and both may break a rule. They
    are one mistake, and the rule-ordered pass above has already put the most
    informative verdict first, so the rest are noise.
    """
    seen: set[tuple[Path, int]] = set()
    kept: list[Violation] = []
    for violation in violations:
        location = (violation.path, violation.line)
        if location in seen:
            continue
        seen.add(location)
        kept.append(violation)
    return kept


def _is_module_internal(dotted: str, package: str, known: Container[str]) -> bool:
    """True for ``app.modules.clients.service``, false for ``app.modules.clients``.

    ⚠️ ``from app.modules.clients import ClientService`` produces the same
    four-part candidate as ``import app.modules.clients.service``, and the two
    are indistinguishable from the AST alone. They are told apart by asking
    whether a file of that name actually exists: ``ClientService`` is a symbol
    re-exported from ``__init__.py`` (legal), whereas ``service`` is a module on
    disk (R2). Without this check, every legal import of a class through a
    module's front door would be reported as a violation.
    """
    if not dotted.startswith(f"{package}.modules.") or len(dotted.split(".")) <= 3:
        return False
    return dotted in known


# ─── R6: cross-module table access ───────────────────────────────────────


def check_tables(sources: Sequence[PySource]) -> list[Violation]:
    """A module MUST NOT read another module's tables.

    🔒 R6 is what stops the module boundary from being bypassed one join at a
    time. Import rules are easy to respect and easy to defeat: a foreign key
    into another module's table couples the two schemas just as hard as an
    import couples the code, and it does so invisibly.
    """
    owner: dict[str, str] = {}
    for source in sources:
        if source.layer is not Layer.MODULE or source.module_name is None:
            continue
        for table, _ in source.tables:
            owner.setdefault(table, source.module_name)

    violations: list[Violation] = []
    for source in sources:
        if source.layer is not Layer.MODULE or source.module_name is None:
            continue
        for table, line in source.foreign_keys:
            holder = owner.get(table)
            if holder is not None and holder != source.module_name:
                violations.append(
                    Violation(
                        rule="R6",
                        path=source.path,
                        line=line,
                        detail=(
                            f"module `{source.module_name}` references table "
                            f"`{table}`, owned by module `{holder}`"
                        ),
                        remedy=(
                            "Hold the identifier as a plain column with no foreign "
                            "key, and resolve it through an event or a kernel query "
                            "port (Arch §3.4)."
                        ),
                    )
                )
    return violations


# ─── R7: supabase-js absence ─────────────────────────────────────────────

_DEPENDENCY_FIELDS: Final[tuple[str, ...]] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def check_frontend_dependencies(frontend: Path) -> list[Violation]:
    """🔒 ADR-02 — FastAPI is the sole data path.

    V1's authorization became unmaintainable because Supabase *and* FastAPI were
    both the backend: two data paths meant two authorization systems, and they
    drifted. The package's mere presence is the violation — once it is
    installable, it will eventually be imported.
    """
    violations: list[Violation] = []
    if not frontend.is_dir():
        return violations

    for manifest in sorted(frontend.rglob("package.json")):
        if any(part in _SKIP_DIRS for part in manifest.parts):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            violations.append(
                Violation(
                    rule="R7",
                    path=manifest,
                    line=0,
                    detail=f"package.json could not be parsed: {exc}",
                    remedy="Fix the manifest; dependencies cannot be verified until then.",
                )
            )
            continue

        for field in _DEPENDENCY_FIELDS:
            section = data.get(field)
            if not isinstance(section, dict):
                continue
            for name in sorted(section):
                if "supabase" in name.lower():
                    violations.append(
                        Violation(
                            rule="R7",
                            path=manifest,
                            line=_line_of(manifest, f'"{name}"'),
                            detail=f"`{name}` declared in {field}",
                            remedy=(
                                "Remove it. The browser never queries the database — "
                                "all data access goes through the FastAPI API "
                                "(ADR-02). Supabase is infrastructure, not a backend."
                            ),
                        )
                    )
    return violations


def _line_of(path: Path, needle: str) -> int:
    """Best-effort line number for a message. Never raises; 0 means unknown."""
    try:
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in text:
                return number
    except OSError:
        pass
    return 0


# ─── R8: business logic in components ────────────────────────────────────

_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"""^\s*import\s+(?:type\s+)?[^'"]*from\s*['"](?P<spec>[^'"]+)['"]"""
    r"""|^\s*import\s*['"](?P<bare>[^'"]+)['"]"""
    r"""|\bfrom\s*['"](?P<dyn>[^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

# A component importing any of these is reaching for domain code.
_DOMAIN_SPECIFIERS: Final[tuple[str, ...]] = (
    "@wellnesscrm/api-client",
    "/features/",
    "/services/",
    "/domain/",
    "/lib/api",
)

_DOMAIN_SEGMENTS: Final[frozenset[str]] = frozenset({"features", "services", "domain"})

# Direct network access from a component, which NFR-068 forbids outright.
_NETWORK_RE: Final[re.Pattern[str]] = re.compile(
    r"""^\s*import\s[^\n]*['"]axios['"]|(?<![\w.])fetch\s*\(""",
    re.MULTILINE,
)

_TS_SUFFIXES: Final[frozenset[str]] = frozenset({".ts", ".tsx", ".js", ".jsx"})


def check_components(frontend: Path) -> list[Violation]:
    """🔒 NFR-068 — V1's most damaging failure, as a heuristic.

    A component may render, hold local UI state, and call hooks. It may not
    compute a domain outcome or fetch its own data (Arch §4.4). We cannot prove
    the absence of business logic, but we can detect its tell: a component
    importing the API client or a feature's domain code.

    ``packages/design-system`` is exempt — its ``components/`` directory holds
    the primitives, which by definition know nothing about the domain and are
    checked by the far stronger rule that they have no domain imports available
    to them at all.
    """
    violations: list[Violation] = []
    if not frontend.is_dir():
        return violations

    for path in sorted(frontend.rglob("*")):
        if path.suffix not in _TS_SUFFIXES or not path.is_file():
            continue
        parts = path.parts
        if any(part in _SKIP_DIRS for part in parts):
            continue
        if "components" not in parts:
            continue
        if _is_design_system(path):
            continue

        text = path.read_text(encoding="utf-8", errors="replace")

        for match in _IMPORT_RE.finditer(text):
            spec = match.group("spec") or match.group("bare") or match.group("dyn")
            if not spec or not _is_domain_specifier(spec):
                continue
            violations.append(
                Violation(
                    rule="R8",
                    path=path,
                    line=text.count("\n", 0, match.start()) + 1,
                    detail=f"component imports domain code `{spec}`",
                    remedy=(
                        "Move the call into a feature hook and pass the result in as "
                        "props. Components render; they do not fetch or decide "
                        "(Arch §4.4)."
                    ),
                )
            )

        for match in _NETWORK_RE.finditer(text):
            violations.append(
                Violation(
                    rule="R8",
                    path=path,
                    line=text.count("\n", 0, match.start()) + 1,
                    detail="component performs a direct network call",
                    remedy=(
                        "Data fetching belongs in a feature hook using the generated "
                        "api-client, never in a component (Arch §4.4)."
                    ),
                )
            )

    return violations


def _is_design_system(path: Path) -> bool:
    parts = path.parts
    return any(
        parts[i] == "packages" and i + 1 < len(parts) and parts[i + 1] == "design-system"
        for i in range(len(parts))
    )


def _is_domain_specifier(spec: str) -> bool:
    normalised = spec.replace("\\", "/")
    if any(marker in normalised for marker in _DOMAIN_SPECIFIERS):
        return True
    # Relative traversal into a domain directory: `../../features/plans/api`.
    return bool(set(normalised.split("/")) & _DOMAIN_SEGMENTS)


# ─── Reporting ───────────────────────────────────────────────────────────


def report(violations: Sequence[Violation], root: Path) -> str:
    """Group by rule and state the rule, so the failure teaches the rule."""
    if not violations:
        return ""

    lines: list[str] = ["", "Architectural boundary violations", "=" * 72]
    by_rule: dict[str, list[Violation]] = {}
    for violation in violations:
        by_rule.setdefault(violation.rule, []).append(violation)

    for rule in sorted(by_rule):
        statement = RULES.get(rule, "Source could not be analysed")
        found = by_rule[rule]
        lines.append("")
        lines.append(f"{rule} — {statement}   [{len(found)}]")
        lines.extend(v.render(root) for v in found)

    lines.extend(
        [
            "",
            "=" * 72,
            f"{len(violations)} violation(s). See Arch §3 for the rules and §3.4 for "
            "the permitted alternatives.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Enforce the architectural boundaries of Arch §3.",
    )
    parser.add_argument(
        "--backend",
        type=Path,
        default=here.parent / "app",
        help="Python package root (default: backend/app)",
    )
    parser.add_argument(
        "--frontend",
        type=Path,
        default=here.parent.parent / "frontend",
        help="Frontend workspace root (default: frontend/)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print nothing when clean.",
    )
    args = parser.parse_args(argv)

    backend: Path = args.backend.resolve()
    frontend: Path = args.frontend.resolve()

    violations: list[Violation] = []
    scanned = 0

    if backend.is_dir():
        sources, failures = parse_backend(backend)
        scanned = len(sources)
        violations.extend(failures)
        violations.extend(check_imports(sources, backend.name))
        violations.extend(check_tables(sources))
    else:
        print(f"warning: backend root not found: {backend}", file=sys.stderr)

    violations.extend(check_frontend_dependencies(frontend))
    violations.extend(check_components(frontend))

    if violations:
        root = backend.parent.parent
        print(report(violations, root), file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"Boundaries OK — {scanned} Python file(s), 8 rules, 0 violations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
