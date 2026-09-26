"""
sentinel/orchestrator.py

Entry point for the Dev Sentinel investigation pipeline.

State machine:

  no_changes          → status="clean", stop
  pending             → Explorer + Impact
  pending + low conf  → status="clean", stop (no executable evidence)
  pending + med/high  → Tester → Runner (focused)
  focused pass        → status="clean", stop (suspicion unfounded)
  focused fail        → status="regression_confirmed", stop

Invariants enforced:
  - Explorer, Impact, Tester are read-only (never write files).
  - Runner only executes tests (never modifies source).
  - The Orchestrator never invokes the Fixer; fix decisions are made externally.
  - No stage may jump directly from "potential_regression" to "fixed".
  - Exceptions from any stage are caught, recorded, and reported rather than
    crashing silently.

TODO (Bob 2.0 integration): Each agent call below is designed to be replaced
by a Bob subagent invocation when the agent layer is wired up.
"""

import traceback as _traceback
from sentinel.models import EvidenceReport, TestResult
from sentinel.agents.explorer import run_explorer
from sentinel.agents.impact   import run_impact
from sentinel.agents.tester   import run_tester
from sentinel.runner          import run_tests


# ── Internal helpers ──────────────────────────────────────────────────────────

def _safe_run_tests(target: str | None = None) -> TestResult:
    """Thin wrapper around run_tests that never propagates exceptions.

    If the test runner itself crashes (missing pytest, import error, etc.) the
    result is treated as a failure with the exception text in stderr.
    """
    try:
        return run_tests(target)
    except Exception as exc:
        return TestResult(
            passed=False,
            exit_code=-1,
            stdout="",
            stderr=f"Test runner raised an exception: {exc}\n{_traceback.format_exc()}",
        )


# ── Public interface ──────────────────────────────────────────────────────────

def orchestrate_investigation(changed_files: list[str], diff: str) -> EvidenceReport:
    """Coordinate the Dev Sentinel investigation pipeline.

    Performs read-only investigation (Explorer, Impact, Tester) and focused
    test reproduction (Runner).  Does NOT invoke the Fixer.

    Args:
        changed_files: List of file paths that have uncommitted local changes.
        diff:          Raw output of `git diff` for those files.

    Returns:
        An EvidenceReport whose `status` field communicates the outcome:
          "clean"                — no regression found or suspicion not reproduced
          "regression_confirmed" — executable test failure confirms the regression
    """

    # ── F / invalid inputs ────────────────────────────────────────────────────
    if not isinstance(changed_files, list):
        return EvidenceReport(
            status="clean",
            suspected_issue="Invalid input: changed_files must be a list.",
        )

    # ── A / no changes ────────────────────────────────────────────────────────
    if not changed_files or not diff or not diff.strip():
        return EvidenceReport(
            changed_files=changed_files or [],
            status="clean",
            suspected_issue="",
        )

    report = EvidenceReport(
        changed_files=changed_files,
        status="pending",
    )

    # ── Stage 1: Explorer (read-only) ─────────────────────────────────────────
    try:
        code_map = run_explorer(changed_files, diff)
    except Exception as exc:
        report.status = "clean"
        report.suspected_issue = f"Explorer raised an exception: {exc}"
        return report

    # ── Stage 2: Impact (read-only) ───────────────────────────────────────────
    try:
        impact = run_impact(changed_files, diff, code_map)
    except Exception as exc:
        report.status = "clean"
        report.suspected_issue = f"Impact raised an exception: {exc}"
        return report

    report.changed_behaviour   = impact.changed_behaviour
    report.affected_components = impact.affected_components
    report.suspected_issue     = impact.reasoning

    # ── A / C / clean exit: no meaningful evidence ───────────────────────────
    # "low" confidence with no affected inputs → nothing actionable.
    if impact.confidence == "low" and not impact.affected_inputs:
        report.status = "clean"
        return report

    # ── Stage 3: Tester (read-only) ───────────────────────────────────────────
    try:
        test_plan = run_tester(changed_files, diff, code_map, impact)
    except Exception as exc:
        report.status = "clean"
        report.suspected_issue += f"  Tester raised an exception: {exc}"
        return report

    # Determine which test target to exercise.
    test_target = test_plan.target_file or None

    # ── Stage 4: Runner — focused reproduction ────────────────────────────────
    reproduction_result = _safe_run_tests(test_target)
    report.reproduction_result = reproduction_result

    # ── C / suspicion unfounded: focused tests pass ───────────────────────────
    # Impact predicted a concern but actual test execution shows no failure.
    # This is NOT a confirmed regression.
    if reproduction_result.passed:
        report.status = "clean"
        return report

    # ── Regression confirmed by executable evidence ───────────────────────────
    report.status = "regression_confirmed"
    return report
