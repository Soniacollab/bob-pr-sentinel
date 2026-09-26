"""
tests/test_orchestrator.py

Adversarial validation suite for sentinel/orchestrator.py.

Updated to match the new orchestrator contract:

  The orchestrator runs: Explorer → Impact → Tester → Runner
  If focused tests fail → status="regression_confirmed", STOP.
  The orchestrator does NOT call the Fixer.

Scenarios covered:
  A. Clean path (no regression)
  B. Confirmed regression → regression_confirmed (Fixer is NOT called)
  C. Speculative / unconfirmed issue
  D. Regression confirmed, no fix applied (Fixer not the orchestrator's job)
  E. Full-suite invariant (orchestrator stops at regression_confirmed)
  F. Invalid / empty inputs
  G. Agent isolation (Fixer is never called by the orchestrator)
  H. No false positives
  I. No false negatives
  J. State transitions
  K. Regression safety invariants
  L. Orchestrator self-tests
"""

import subprocess
from unittest.mock import patch, MagicMock
import pytest

from sentinel.orchestrator import orchestrate_investigation
from sentinel.models import (
    CodeMap, ImpactReport, TestPlan, TestResult, EvidenceReport,
)


# ── Fixtures / builders ───────────────────────────────────────────────────────

def _clean_impact(**overrides) -> ImpactReport:
    defaults = dict(
        changed_behaviour="No recognizable guard removal detected.",
        affected_inputs=[],
        affected_components=[],
        confidence="low",
        reasoning="",
    )
    defaults.update(overrides)
    return ImpactReport(**defaults)


def _high_impact(**overrides) -> ImpactReport:
    defaults = dict(
        changed_behaviour="None/empty-string guard removed",
        affected_inputs=[
            {"value": "None", "exception": "TypeError"},
            {"value": '""',   "exception": "ValueError"},
        ],
        affected_components=["clean_user_input"],
        confidence="high",
        reasoning="Guard removed; float() raises on None and ''.",
    )
    defaults.update(overrides)
    return ImpactReport(**defaults)


def _passing_result(**kw) -> TestResult:
    return TestResult(passed=True, exit_code=0, stdout="2 passed", stderr="", **kw)


def _failing_result(**kw) -> TestResult:
    return TestResult(
        passed=False, exit_code=1,
        stdout="FAILED tests/test_preprocessing.py::test_x\nE TypeError",
        stderr="",
        **kw,
    )


def _empty_code_map() -> CodeMap:
    return CodeMap(
        changed_symbols=["clean_user_input"],
        call_chain=[],
        referencing_files=["tests/test_preprocessing.py"],
        existing_tests=["test_clean_user_input_none_score"],
    )


def _empty_test_plan() -> TestPlan:
    return TestPlan(target_file="tests/test_preprocessing.py", new_tests=[])


# ── A. CLEAN PATH ─────────────────────────────────────────────────────────────

class TestCleanPath:
    """No regression, pipeline must stop without ever invoking the Fixer."""

    def test_no_changes_returns_clean(self):
        result = orchestrate_investigation([], "")
        assert result.status == "clean"

    def test_empty_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], "")
        assert result.status == "clean"

    def test_whitespace_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], "   \n  ")
        assert result.status == "clean"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_low_confidence_no_inputs_stops_early(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- comment\n")
        assert result.status == "clean"
        # Tester and Runner not called (early exit before them)
        mock_tester.assert_not_called()
        mock_runner.assert_not_called()

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_medium_confidence_but_passing_tests_returns_clean(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        """Impact suspects something, but focused tests all pass → no regression."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"
        assert result.reproduction_result.passed is True


# ── B. CONFIRMED REGRESSION (live pipeline) ───────────────────────────────────

class TestConfirmedRegression:
    """Real regression: None/empty-string guard removed in preprocessing.py.
    The orchestrator must reach regression_confirmed and STOP there.
    The Fixer is NOT called.
    Skipped when the guard is currently present (healthy tree).
    """

    def _get_live_diff(self):
        r = subprocess.run(["git", "diff"], capture_output=True, text=True)
        return r.stdout

    def _get_live_files(self):
        r = subprocess.run(
            ["git", "diff", "--name-only"], capture_output=True, text=True
        )
        return r.stdout.splitlines()

    def test_confirmed_regression_stops_at_regression_confirmed(self):
        """End-to-end: broken guard → regression_confirmed, no further action."""
        diff    = self._get_live_diff()
        changed = self._get_live_files()

        if "app/preprocessing.py" not in changed:
            pytest.skip(
                "app/preprocessing.py not in current diff — "
                "regression scenario not active in this working tree."
            )
        if "float(data[" not in diff or "is None" in diff:
            pytest.skip(
                "The guard does not appear to be removed in the current diff — "
                "regression scenario not active."
            )

        result = orchestrate_investigation(changed, diff)
        assert result.status == "regression_confirmed", (
            f"Expected regression_confirmed, got: {result.status}"
        )
        assert result.reproduction_result is not None
        assert result.reproduction_result.passed is False, (
            "Reproduction should have failed — tests must detect the regression."
        )
        # The orchestrator must NOT have applied a fix.
        assert result.fix_applied == "", (
            "The orchestrator must not apply a fix — that is the Fixer's job."
        )


# ── C. SPECULATIVE / UNCONFIRMED ──────────────────────────────────────────────

class TestSpeculativeIssue:
    """Impact reports a concern but focused tests pass.
    The Fixer must never be invoked — and now it structurally cannot be."""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_passing_focused_tests_produce_clean(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_status_not_regression_confirmed_when_tests_pass(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status != "regression_confirmed"


# ── D. REGRESSION CONFIRMED — NO FIX APPLIED ─────────────────────────────────

class TestRegressionConfirmedNoFix:
    """When focused tests fail the orchestrator stops at regression_confirmed.
    No fix is applied. No fix_applied field is set. Reproduction evidence
    is preserved for the caller (e.g. `dev-sentinel fix`)."""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_failing_tests_produce_regression_confirmed(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_no_fix_applied_by_orchestrator(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        """The orchestrator must never set fix_applied — that is the Fixer's job."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.fix_applied == ""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_reproduction_result_preserved(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.reproduction_result is not None
        assert result.reproduction_result.passed is False


# ── E. ORCHESTRATOR NEVER PRODUCES "fixed" ───────────────────────────────────

class TestOrchestratorNeverFixed:
    """The orchestrator must never produce status="fixed".
    That transition belongs to the Fixer, which is external."""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_orchestrator_cannot_produce_fixed_status(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status != "fixed"

    def test_clean_path_never_fixed(self):
        result = orchestrate_investigation([], "")
        assert result.status != "fixed"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_passing_tests_never_fixed(
        self, mock_explorer, mock_impact, mock_tester, mock_runner
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status != "fixed"


# ── F. INVALID / EMPTY INPUTS ────────────────────────────────────────────────

class TestInvalidInputs:

    def test_empty_changed_files_returns_clean(self):
        result = orchestrate_investigation([], "some diff")
        assert result.status == "clean"

    def test_empty_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], "")
        assert result.status == "clean"

    def test_none_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], None)
        assert result.status == "clean"

    def test_wrong_type_changed_files_returns_clean(self):
        result = orchestrate_investigation("not-a-list", "diff")
        assert result.status == "clean"

    @patch("sentinel.orchestrator.run_explorer", side_effect=RuntimeError("boom"))
    def test_explorer_exception_returns_clean(self, mock_explorer):
        result = orchestrate_investigation(["app/foo.py"], "- x\n")
        assert result.status == "clean"
        assert "Explorer" in result.suspected_issue

    @patch("sentinel.orchestrator.run_impact",  side_effect=RuntimeError("boom"))
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_impact_exception_returns_clean(self, me, mi):
        result = orchestrate_investigation(["app/foo.py"], "- x\n")
        assert result.status == "clean"
        assert "Impact" in result.suspected_issue

    @patch("sentinel.orchestrator.run_tester",  side_effect=RuntimeError("boom"))
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_tester_exception_returns_clean(self, me, mi, mt):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"
        assert "Tester" in result.suspected_issue

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_confirmed_regression_with_no_fixer_in_pipeline(
        self, me, mi, mt, mr
    ):
        """Replaces the old test_fixer_exception_preserves_regression_confirmed.
        The orchestrator now stops at regression_confirmed without touching the
        Fixer at all, so a Fixer exception is simply impossible here."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"


# ── G. AGENT ISOLATION ───────────────────────────────────────────────────────

class TestAgentIsolation:
    """The Fixer is never part of the orchestrator pipeline.
    Explorer, Impact, and Tester are read-only. Runner only executes tests."""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_orchestrator_does_not_import_run_fixer(self, me, mi, mt, mr):
        """run_fixer must not exist as an attribute of the orchestrator module."""
        import sentinel.orchestrator as orch_module
        assert not hasattr(orch_module, "run_fixer"), (
            "run_fixer must not be imported into sentinel.orchestrator"
        )

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_clean_path_no_fixer(self, me, mi, mt, mr):
        """Clean path: orchestrator runs and returns without any Fixer involvement."""
        result = orchestrate_investigation(["app/foo.py"], "  ")
        assert result.status == "clean"
        assert result.fix_applied == ""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_passing_focused_tests_no_fixer(self, me, mi, mt, mr):
        """Passing focused tests: pipeline stops at clean without touching Fixer."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"
        assert result.fix_applied == ""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_confirmed_regression_no_fixer_invoked(self, me, mi, mt, mr):
        """Confirmed regression: orchestrator stops at regression_confirmed.
        The fix_applied field must remain empty — the Fixer was never called."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"
        assert result.fix_applied == "", (
            "Orchestrator must not invoke the Fixer or populate fix_applied."
        )


# ── H. NO FALSE POSITIVES ────────────────────────────────────────────────────

class TestNoFalsePositives:

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_impact_prediction_not_treated_as_proof(
        self, me, mi, mt, mr
    ):
        """High-confidence impact + passing tests → clean, not regression_confirmed."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"

    @patch("sentinel.orchestrator._safe_run_tests",
           return_value=TestResult(
               passed=True, exit_code=0,
               stdout="1 passed",
               stderr="",
           ))
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_passing_test_not_reported_as_failure(
        self, me, mi, mt, mr
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.reproduction_result.passed is True
        assert result.status == "clean"


# ── I. NO FALSE NEGATIVES ────────────────────────────────────────────────────

class TestNoFalseNegatives:

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_genuine_regression_reaches_regression_confirmed(
        self, me, mi, mt, mr
    ):
        """A genuine reproducible failure must reach regression_confirmed."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_failing_test_evidence_preserved_in_report(
        self, me, mi, mt, mr
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.reproduction_result is not None
        assert result.reproduction_result.passed is False

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_report_carries_complete_evidence_for_fixer(
        self, me, mi, mt, mr
    ):
        """The EvidenceReport returned to the caller must contain everything
        the external Fixer (`dev-sentinel fix`) will need."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.changed_files == ["app/foo.py"]
        assert result.reproduction_result.passed is False
        assert result.status == "regression_confirmed"
        assert result.changed_behaviour  # populated from Impact


# ── J. STATE TRANSITIONS ─────────────────────────────────────────────────────

class TestStateTransitions:

    def test_no_changes_to_clean(self):
        r = orchestrate_investigation([], "")
        assert r.status == "clean"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_no_regression_to_clean(self, me, mi, mt, mr):
        r = orchestrate_investigation(["f.py"], "- x\n")
        assert r.status == "clean"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_failing_reproduction_to_regression_confirmed(self, me, mi, mt, mr):
        """Replaces the old test_regression_confirmed_to_fixed.
        The orchestrator's terminal state for a failing reproduction is now
        regression_confirmed, not fixed."""
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status == "regression_confirmed"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_regression_confirmed_is_terminal_state(self, me, mi, mt, mr):
        """Replaces test_regression_confirmed_plus_fix_failed_stays_regression_confirmed.
        Without the Fixer, regression_confirmed is always the terminal state
        when reproduction fails."""
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status == "regression_confirmed"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_no_skip_from_potential_to_fixed(self, me, mi, mt, mr):
        """Passing focused tests → clean. Must not skip to fixed."""
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status != "fixed"


# ── K. REGRESSION SAFETY INVARIANTS ─────────────────────────────────────────

class TestRegressionSafety:

    def test_fixer_never_called_speculatively(self):
        """The orchestrator structurally cannot call the Fixer (not imported)."""
        import sentinel.orchestrator as orch_module
        assert not hasattr(orch_module, "run_fixer")

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_exit_codes_not_swallowed(self, me, mi, mt, mr):
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.reproduction_result.exit_code != 0

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_orchestrator_never_produces_fixed(self, me, mi, mt, mr):
        """Replaces test_fixed_only_when_fixer_reports_fixed.
        The orchestrator can no longer produce 'fixed' under any circumstances."""
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status != "fixed"

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_regression_confirmed_never_becomes_fixed_without_fixer(
        self, me, mi, mt, mr
    ):
        """Replaces test_not_fixed_when_fixer_reports_fix_failed.
        Without the Fixer in the pipeline, regression_confirmed can never
        become fixed inside orchestrate_investigation."""
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status == "regression_confirmed"
        assert r.status != "fixed"


# ── L. ORCHESTRATOR SELF-TESTS ───────────────────────────────────────────────

class TestOrchestratorSelf:

    def test_returns_evidence_report_instance(self):
        r = orchestrate_investigation([], "")
        assert isinstance(r, EvidenceReport)

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_changed_files_propagated_to_report(self, me, mi, mt, mr):
        r = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert "app/foo.py" in r.changed_files

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_impact_behaviour_propagated_to_report(self, me, mi, mt, mr):
        r = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert r.changed_behaviour == _high_impact().changed_behaviour

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fix_applied_empty_after_orchestrator(self, me, mi, mt, mr):
        """Replaces test_fix_applied_propagated_to_report.
        The orchestrator never sets fix_applied — that field is only
        populated by the external Fixer."""
        r = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert r.fix_applied == ""

    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_final_test_result_none_after_orchestrator(self, me, mi, mt, mr):
        """Replaces test_final_test_result_populated_after_fix.
        The orchestrator never runs the full suite — final_test_result is None.
        It is only populated after a successful external Fixer run."""
        r = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert r.final_test_result is None
