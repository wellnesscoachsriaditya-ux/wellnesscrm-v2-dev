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
    ordered = [
        step.get("name", "")
        for step in steps_of(workflow, "boundaries")
        if step.get("run")
    ]
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
    negative = next(
        i for i, step in enumerate(steps) if "tests/test_boundaries.py" in step["run"]
    )

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

    ``check-client-freshness.mjs`` is deliberately excluded: the generator is a
    separate S0 task and the workflow guards its absence with a warning. Every
    script invoked unconditionally must be present.
    """
    commands = all_run_commands(workflow)
    assert "check-bundle-budget.mjs" in commands
    assert (_REPO / "frontend" / "scripts" / "check-bundle-budget.mjs").is_file()
    assert (_REPO / "backend" / "tools" / "check_boundaries.py").is_file()


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
