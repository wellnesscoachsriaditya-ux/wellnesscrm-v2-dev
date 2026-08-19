"""The CI workflow must be internally consistent — S0-4.

🔒 DoD §4.1 gate 1 names six checks: lint, types, boundary checker, tests,
bundle budget, API-client freshness. A workflow that silently stops running one
of them is worse than no workflow, because the badge still says green.

These tests are cheap insurance against the failure mode that actually happens:
a YAML edit that looks fine, breaks a gate, and is not noticed until something
ships. They assert the *contract* — which gates exist, which commands they run,
which versions they pin — not the formatting.

⚠️ These tests cannot prove the pipeline works on GitHub's runners; only a real
run can do that (an explicit S0 testing-strategy item: "CI verified on a scratch
branch"). They prove the configuration says what we think it says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip(
    "yaml",
    reason="PyYAML is not a runtime dependency; these tests run where it is available.",
)

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    """The parsed workflow. A parse failure here is itself the bug."""
    if not _WORKFLOW.is_file():
        pytest.fail(f"CI workflow is missing: {_WORKFLOW}")
    loaded = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "workflow must parse to a mapping"
    return loaded


def steps_of(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return list(workflow["jobs"][job]["steps"])


def run_commands(workflow: dict[str, Any], job: str) -> str:
    """Every shell command in a job, concatenated. For substring assertions."""
    return "\n".join(step.get("run", "") for step in steps_of(workflow, job))


def all_run_commands(workflow: dict[str, Any]) -> str:
    return "\n".join(run_commands(workflow, job) for job in workflow["jobs"])


# ─── Structure ───────────────────────────────────────────────────────────


def test_workflow_is_valid_yaml(workflow: dict[str, Any]) -> None:
    assert workflow["name"] == "CI"
    assert "jobs" in workflow


def test_triggers_cover_push_pr_and_manual(workflow: dict[str, Any]) -> None:
    """`workflow_dispatch` is what makes "verified on a scratch branch" possible.

    ⚠️ ``on`` is the YAML 1.1 boolean ``True`` once parsed — quoting it in the
    file would change the key GitHub reads, so the test accommodates the parser
    rather than the file accommodating the test.
    """
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers is not None, "workflow declares no triggers"
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]


def test_permissions_are_read_only(workflow: dict[str, Any]) -> None:
    """🔒 NFR-034 — CI reads code. A token that can write is a token that can leak."""
    assert workflow["permissions"] == {"contents": "read"}


def test_main_runs_are_never_cancelled(workflow: dict[str, Any]) -> None:
    """`main`'s result gates deployment, so it must not be superseded mid-run."""
    assert "github.ref != 'refs/heads/main'" in str(workflow["concurrency"]["cancel-in-progress"])


def test_every_job_has_a_timeout(workflow: dict[str, Any]) -> None:
    """A hung job on a free tier consumes the month's minutes silently."""
    missing = [name for name, job in workflow["jobs"].items() if "timeout-minutes" not in job]
    assert missing == [], f"jobs without a timeout: {missing}"


# ─── The six DoD §4.1 gates ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "gate"),
    [
        ("ruff format --check .", "formatting"),
        ("ruff check", "lint"),
        ("mypy app tools", "types"),
        ("pytest", "tests"),
    ],
)
def test_backend_job_runs_every_backend_gate(
    workflow: dict[str, Any], command: str, gate: str
) -> None:
    assert command in run_commands(workflow, "backend"), f"backend CI does not run {gate}"


def test_boundary_checker_runs_in_ci(workflow: dict[str, Any]) -> None:
    """🔒 Arch §3.5 and the S0 DoD. The one gate this sprint exists to install."""
    commands = run_commands(workflow, "boundaries")
    assert "python tools/check_boundaries.py" in commands


def test_boundary_negative_tests_run_in_ci(workflow: dict[str, Any]) -> None:
    """A checker never seen to fail is indistinguishable from one that cannot."""
    assert "tests/test_boundaries.py" in run_commands(workflow, "boundaries")


def test_boundary_check_does_not_depend_on_installed_packages(
    workflow: dict[str, Any],
) -> None:
    """🔒 The checker is stdlib-only (Arch §3.5), and CI must keep proving it.

    The property worth protecting is that ``check_boundaries.py`` runs with
    nothing installed, so it still works when the dependency tree is broken —
    which is exactly when someone is crossing a boundary in a hurry.

    That is an ordering claim, not a job-wide one. The job *does* install the
    backend, because the negative tests below it need pytest. What must not
    happen is the boundary check drifting to *after* that install, because then
    an accidental third-party import in the checker would go unnoticed until the
    day the install is what's failing.
    """
    ordered = [step.get("name", "") for step in steps_of(workflow, "boundaries") if step.get("run")]
    check = next(i for i, name in enumerate(ordered) if name == "Check boundaries")
    installs = [i for i, name in enumerate(ordered) if "Install" in name]

    assert check < min(installs, default=len(ordered)), (
        "`Check boundaries` must run before any install step, or the "
        "stdlib-only guarantee of Arch §3.5 stops being verified"
    )


def test_boundary_negative_tests_may_run_after_install(workflow: dict[str, Any]) -> None:
    """The negative tests need pytest, so they may — and must — run after install.

    Asserted explicitly because the previous version of this file forbade any
    install in this job, which made the suite fail the moment the job was
    corrected. The distinction is the point: the *checker* is dependency-free,
    the *test suite exercising it* is not.
    """
    steps = [step for step in steps_of(workflow, "boundaries") if step.get("run")]
    names = [step.get("name", "") for step in steps]

    install = next(i for i, name in enumerate(names) if "Install" in name)
    negative = next(i for i, step in enumerate(steps) if "tests/test_boundaries.py" in step["run"])

    assert install < negative, "the negative tests need pytest installed first"


@pytest.mark.parametrize(
    ("command", "gate"),
    [
        ("npm run lint", "frontend lint"),
        ("npm run typecheck", "frontend types"),
        ("npm run build", "frontend build"),
        ("check-bundle-budget.mjs", "bundle budget"),
        ("check-client-freshness.mjs", "API-client freshness"),
    ],
)
def test_frontend_job_runs_every_frontend_gate(
    workflow: dict[str, Any], command: str, gate: str
) -> None:
    assert command in run_commands(workflow, "frontend"), f"frontend CI does not run {gate}"


def test_frontend_gates_are_skipped_only_while_unscaffolded(
    workflow: dict[str, Any],
) -> None:
    """⚠️ The frontend workspace lands later in S0.

    Every gate step must be guarded by the scaffolding check, so the job is
    honest about being inactive — and starts enforcing automatically on the
    commit that adds the first package, with nobody having to remember.
    """
    gated = [
        step
        for step in steps_of(workflow, "frontend")
        if step.get("run") and "detect" not in step.get("id", "")
    ]
    ungated = [
        step["name"]
        for step in gated
        if "steps.detect.outputs.scaffolded" not in str(step.get("if", ""))
    ]
    assert ungated == [], f"frontend steps not guarded by the scaffolding check: {ungated}"


# ─── Gate independence ───────────────────────────────────────────────────


def test_backend_gates_do_not_short_circuit(workflow: dict[str, Any]) -> None:
    """A formatting failure must not hide a type error.

    Without ``if: !cancelled()`` the first failing step ends the job, so each
    push reveals exactly one problem and fixing three takes three round trips.
    """
    gates = [
        step
        for step in steps_of(workflow, "backend")
        if step.get("run") and "pip install" not in step["run"]
    ]
    assert len(gates) >= 4
    for step in gates:
        assert "cancelled()" in str(
            step.get("if", "")
        ), f"step {step.get('name')!r} short-circuits the remaining gates"


# ─── Aggregate gate ──────────────────────────────────────────────────────


def test_aggregate_job_depends_on_every_other_job(workflow: dict[str, Any]) -> None:
    """One required status check, so adding a job cannot bypass branch protection."""
    jobs = workflow["jobs"]
    aggregate = jobs["ci"]
    assert set(aggregate["needs"]) == set(jobs) - {"ci"}


def test_aggregate_job_fails_when_a_dependency_fails(workflow: dict[str, Any]) -> None:
    """🔒 ``if: always()`` means it also runs when a dependency failed — so it
    must inspect the results rather than merely existing, or a failed gate would
    report success.
    """
    aggregate = workflow["jobs"]["ci"]
    assert str(aggregate["if"]).strip() == "always()"
    body = run_commands(workflow, "ci")
    assert "needs.*.result" in body
    assert "exit 1" in body


# ─── Version pinning ─────────────────────────────────────────────────────


def test_python_version_matches_pyproject(workflow: dict[str, Any]) -> None:
    """CI must test the version we target, or it tests something else."""
    pyproject = (_REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.12"' in pyproject
    assert workflow["env"]["PYTHON_VERSION"] == "3.12"


def test_node_version_matches_frontend_engines(workflow: dict[str, Any]) -> None:
    import json

    manifest = json.loads((_REPO / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert manifest["engines"]["node"] == ">=20.0.0"
    assert workflow["env"]["NODE_VERSION"] == "20"


def test_actions_are_pinned_to_a_major_version(workflow: dict[str, Any]) -> None:
    """Floating actions change under you; a major version is the sane middle."""
    unpinned = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step and "@v" not in step["uses"]
    ]
    assert unpinned == [], f"actions without a version: {unpinned}"


# ─── Referenced files must exist ─────────────────────────────────────────


def test_referenced_scripts_exist(workflow: dict[str, Any]) -> None:
    """A workflow referencing a missing script fails at runtime, not at review.

    Every script CI invokes must be present. This became fully enforceable at
    S0-7, when `check-client-freshness.mjs` landed; before that it was
    deliberately excluded, because the workflow guarded its absence.
    """
    commands = all_run_commands(workflow)
    assert "check-bundle-budget.mjs" in commands
    assert (_REPO / "frontend" / "scripts" / "check-bundle-budget.mjs").is_file()
    assert (_REPO / "frontend" / "scripts" / "check-client-freshness.mjs").is_file()
    assert (_REPO / "backend" / "tools" / "check_boundaries.py").is_file()
    assert (_REPO / "backend" / "tools" / "export_openapi.py").is_file()


# ─── The NFR-079 chain ───────────────────────────────────────────────────


def test_openapi_schema_freshness_runs_in_the_backend_job(workflow: dict[str, Any]) -> None:
    """🔒 NFR-079 link ① — the committed schema must match the backend code.

    ⚠️ This half cannot live in the frontend job: that job installs no Python and
    no backend, on purpose. Splitting the chain across the two jobs is what keeps
    each one's install minimal.
    """
    assert "export_openapi.py --check" in run_commands(workflow, "backend")


def test_client_freshness_runs_unconditionally(workflow: dict[str, Any]) -> None:
    """🔒 NFR-079 link ② — and it must be a gate, not a notice.

    The step previously tested for the script's existence and emitted a warning
    when it was absent, because the generator was a later S0 task. A warning does
    not fail a build; now that the script exists, the guard must be gone or the
    gate is decorative.
    """
    step = next(
        s
        for s in steps_of(workflow, "frontend")
        if "check-client-freshness.mjs" in s.get("run", "")
    )
    body = step["run"]
    assert "::warning" not in body, "the freshness check still only warns; it must fail the build"
    assert "if [[ -f" not in body, "the freshness check is still guarded by an existence test"


def test_npm_scripts_invoked_by_ci_are_declared(workflow: dict[str, Any]) -> None:
    """``npm run lint`` fails if package.json has no ``lint`` script."""
    import json
    import re

    manifest = json.loads((_REPO / "frontend" / "package.json").read_text(encoding="utf-8"))
    declared = set(manifest["scripts"])
    invoked = set(re.findall(r"npm run ([\w:-]+)", run_commands(workflow, "frontend")))

    assert invoked, "no npm scripts invoked — the frontend job would check nothing"
    assert invoked <= declared, f"CI runs undeclared npm scripts: {sorted(invoked - declared)}"


def test_ci_uses_npm_ci_when_a_lockfile_exists(workflow: dict[str, Any]) -> None:
    """🔒 NFR-040 — reproducible installs. `npm install` may resolve differently."""
    assert "npm ci" in run_commands(workflow, "frontend")


# ─── The isolation gate ──────────────────────────────────────────────────
#
# 🔒 AC-M0-003 is the S1 sprint gate, and it spent all of S0 and most of S1
# skipping. These tests assert the configuration that makes it actually run —
# and, more importantly, the configuration that stops it from silently going
# back to skipping.


def test_isolation_gate_has_a_postgres_service(workflow: dict[str, Any]) -> None:
    """The suites need a live database; without a service they skip."""
    services = workflow["jobs"]["integration"].get("services", {})
    assert "postgres" in services, "the isolation gate has no PostgreSQL service"
    assert "postgres:" in services["postgres"]["image"]


def test_isolation_gate_pins_the_postgres_major_version(workflow: dict[str, Any]) -> None:
    """🔒 RLS semantics are what this job asserts, so the version must not drift.

    `postgres:latest` would move under us, and a major-version change to row
    security is exactly the kind of thing worth catching deliberately rather
    than in a confusing red build months later.
    """
    image = workflow["jobs"]["integration"]["services"]["postgres"]["image"]
    tag = image.split(":", 1)[1]
    assert tag != "latest", "the PostgreSQL image is unpinned"
    assert tag[0].isdigit(), f"expected a version tag, got {tag!r}"


def test_isolation_gate_forbids_skipping(workflow: dict[str, Any]) -> None:
    """🔒 The single most important assertion in this file.

    A skipped test is green. If the service block broke, the env vars were
    renamed, or the database became unreachable, the suites would report
    "skipped" and CI would pass — leaving the guarantee the whole tenancy model
    rests on unverified, with no signal anywhere. `REQUIRE_LIVE_DATABASE` is
    what turns that skip into a failure (see tests/integration/conftest.py).
    """
    env = workflow["jobs"]["integration"].get("env", {})
    assert str(env.get("REQUIRE_LIVE_DATABASE", "")) == "1", (
        "REQUIRE_LIVE_DATABASE is not set on the isolation job — a database "
        "failure would silently skip the gate instead of failing the build"
    )


def test_isolation_gate_runs_both_suites(workflow: dict[str, Any]) -> None:
    """Tenant isolation (AC-M0-003) *and* audit immutability (DDR-15)."""
    commands = run_commands(workflow, "integration")
    assert "pytest tests/integration" in commands


def test_isolation_gate_provisions_roles_before_migrating(workflow: dict[str, Any]) -> None:
    """🔒 Ordering is load-bearing, so it is asserted rather than trusted.

    `alembic upgrade head` connects as `app_migrator`, and revision 0003 issues
    `REVOKE ... FROM app_user`. Both roles must therefore exist before the
    migrations run — a reordering here fails with a confusing "role does not
    exist" mid-migration.
    """
    commands = run_commands(workflow, "integration")
    assert "provision-test-db.sh" in commands, "the isolation job never provisions the database"

    provision = commands.index("ops/db/provision-test-db.sh")
    pytest_run = commands.index("pytest tests/integration")
    assert provision < pytest_run, "the gate runs before the database is provisioned"


def test_grant_check_is_proven_to_fail(workflow: dict[str, Any]) -> None:
    """🔒 A check never seen to fail is indistinguishable from one that cannot.

    The same argument the boundary job's negative tests make. DDR-15 immutability
    is enforced by the *absence* of an UPDATE grant, and nothing fails loudly
    when one is added back — so CI grants it back on purpose and asserts that
    002_verify_grants.sql aborts.
    """
    commands = run_commands(workflow, "integration")
    assert (
        "GRANT UPDATE ON TABLE audit_log TO app_user" in commands
    ), "CI never proves 002_verify_grants.sql can fail; the DDR-15 check may be inert"
    assert "002_verify_grants.sql" in commands


def test_migrations_are_proven_reversible(workflow: dict[str, Any]) -> None:
    """A migration that cannot be rolled back is a deploy that cannot be undone."""
    commands = run_commands(workflow, "integration")
    assert "alembic downgrade base" in commands
    assert "alembic upgrade head" in commands
