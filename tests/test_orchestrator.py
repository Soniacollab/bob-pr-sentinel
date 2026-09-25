"""
tests/test_orchestrator.py

Adversarial validation suite for sentinel/orchestrator.py.

Covers all scenarios A–L from the quality specification:
  A. Clean path (no regression)
  B. Confirmed regression → fixed
  C. Speculative / unconfirmed issue
  D. Fix failure
  E. Full-suite failure after focused tests pass
  F. Invalid / empty inputs
  G. Agent isolation (read-only enforcement)
  H. No false positives
  I. No false negatives
  J. State transitions
  K. Regression safety invariants
  L. Orchestrator self-tests

Tests use unittest.mock to replace the agent and runner calls so that:
  - No production files are modified by the test suite itself.
  - Each scenario is deterministic and isolated.
  - The real regression scenario (B) uses the live pipeline.
"""

import subprocess
from unittest.mock import patch, MagicMock
import pytest

from sentinel.orchestrator import orchestrate_investigation
from sentinel.models import (
    CodeMap, ImpactReport, TestPlan, TestResult, EvidenceReport,
)
from sentinel.agents.fixer import FixReport


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


def _fixed_report(**kw) -> FixReport:
    defaults = dict(
        fix_justified=True,
        files_modified=["app/preprocessing.py"],
        original_failure="TypeError",
        root_cause="guard removed",
        fix_applied="restored guard",
        fix_rationale="minimal",
        focused_test_result=_passing_result(),
        full_test_result=_passing_result(),
        status="fixed",
        remaining_uncertainty="",
    )
    defaults.update(kw)
    return FixReport(**defaults)


def _fix_failed_report(**kw) -> FixReport:
    base = _fixed_report(
        status="fix_failed",
        focused_test_result=_failing_result(),
        full_test_result=None,
        remaining_uncertainty="Focused tests still fail.",
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


# ── A. CLEAN PATH ─────────────────────────────────────────────────────────────

class TestCleanPath:
    """No regression, pipeline must stop without invoking Fixer."""

    def test_no_changes_returns_clean(self):
        result = orchestrate_investigation([], "")
        assert result.status == "clean"

    def test_empty_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], "")
        assert result.status == "clean"

    def test_whitespace_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], "   \n  ")
        assert result.status == "clean"

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_low_confidence_no_inputs_stops_early(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- comment\n")
        assert result.status == "clean"
        mock_fixer.assert_not_called()
        # Tester and Runner also not called (early exit before them)
        mock_tester.assert_not_called()
        mock_runner.assert_not_called()

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_medium_confidence_but_passing_tests_returns_clean(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        """Impact suspects something, but focused tests all pass → no regression."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"
        assert result.reproduction_result.passed is True
        mock_fixer.assert_not_called()


# ── B. CONFIRMED REGRESSION (live pipeline) ───────────────────────────────────

class TestConfirmedRegression:
    """Real regression: None/empty-string guard removed in preprocessing.py.
    Uses the actual agents and runner against the live repository.
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

    def test_confirmed_regression_reaches_fixed(self):
        """End-to-end: broken guard → regression_confirmed → fixed."""
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
        # After the Fixer runs, the file will be repaired.
        assert result.status in ("fixed", "regression_confirmed"), (
            f"Unexpected status: {result.status}"
        )
        assert result.reproduction_result is not None
        assert result.reproduction_result.passed is False, (
            "Reproduction should have failed — tests must detect the regression."
        )


# ── C. SPECULATIVE / UNCONFIRMED ──────────────────────────────────────────────

class TestSpeculativeIssue:
    """Impact reports a concern but focused tests pass → Fixer MUST NOT run."""

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_not_called_when_tests_pass(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"
        mock_fixer.assert_not_called()

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_status_not_regression_confirmed_when_tests_pass(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status != "regression_confirmed"


# ── D. FIX FAILURE ────────────────────────────────────────────────────────────

class TestFixFailure:
    """Fixer cannot repair the issue → status must stay regression_confirmed."""

    @patch("sentinel.orchestrator.run_fixer",
           return_value=_fix_failed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fix_failed_preserves_regression_confirmed(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"
        assert result.status != "fixed"

    @patch("sentinel.orchestrator.run_fixer",
           return_value=_fix_failed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_reproduction_result_preserved_on_fix_failure(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.reproduction_result is not None
        assert result.reproduction_result.passed is False


# ── E. FULL-SUITE FAILURE AFTER FOCUSED TESTS PASS ───────────────────────────

class TestFullSuiteFailureAfterFocused:
    """Focused tests pass post-fix but full suite fails → NOT fixed."""

    @patch("sentinel.orchestrator.run_fixer",
           return_value=_fix_failed_report(
               focused_test_result=_passing_result(),
               full_test_result=_failing_result(),
               status="fix_failed",
               remaining_uncertainty="Full suite failed after focused tests passed.",
           ))
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_full_suite_failure_is_not_fixed(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"
        assert result.status != "fixed"

    @patch("sentinel.orchestrator.run_fixer",
           return_value=_fix_failed_report(
               focused_test_result=_passing_result(),
               full_test_result=_failing_result(),
               status="fix_failed",
               remaining_uncertainty="Full suite failed after focused tests passed.",
           ))
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_full_suite_failure_evidence_preserved(
        self, mock_explorer, mock_impact, mock_tester, mock_runner, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.final_test_result is not None
        assert result.final_test_result.passed is False


# ── F. INVALID / EMPTY INPUTS ────────────────────────────────────────────────

class TestInvalidInputs:

    def test_empty_changed_files_returns_clean(self):
        result = orchestrate_investigation([], "some diff")
        assert result.status == "clean"

    def test_empty_diff_returns_clean(self):
        result = orchestrate_investigation(["app/foo.py"], "")
        assert result.status == "clean"

    def test_none_diff_returns_clean(self):
        # diff=None: the guard checks `not diff` which is True for None
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

    @patch("sentinel.orchestrator.run_fixer",   side_effect=RuntimeError("boom"))
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_exception_preserves_regression_confirmed(
        self, me, mi, mt, mr, mf
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "regression_confirmed"
        assert "Fixer" in result.suspected_issue


# ── G. AGENT ISOLATION ───────────────────────────────────────────────────────

class TestAgentIsolation:
    """Explorer, Impact, Tester must not modify files.
    Fixer is the only stage allowed to write production code.
    These are enforced structurally — we verify the Fixer is NOT called
    until status == regression_confirmed."""

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_not_called_on_clean_path(
        self, me, mi, mt, mr, mock_fixer
    ):
        orchestrate_investigation(["app/foo.py"], "  ")
        mock_fixer.assert_not_called()

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_not_called_when_focused_tests_pass(
        self, me, mi, mt, mr, mock_fixer
    ):
        orchestrate_investigation(["app/foo.py"], "- guard\n")
        mock_fixer.assert_not_called()

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_called_exactly_once_on_confirmed_regression(
        self, me, mi, mt, mr, mock_fixer
    ):
        orchestrate_investigation(["app/foo.py"], "- guard\n")
        mock_fixer.assert_called_once()

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_receives_regression_confirmed_evidence(
        self, me, mi, mt, mr, mock_fixer
    ):
        orchestrate_investigation(["app/foo.py"], "- guard\n")
        call_args = mock_fixer.call_args
        evidence: EvidenceReport = call_args[0][0]
        assert evidence.status == "regression_confirmed"
        assert evidence.reproduction_result is not None
        assert evidence.reproduction_result.passed is False


# ── H. NO FALSE POSITIVES ────────────────────────────────────────────────────

class TestNoFalsePositives:

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",
           return_value=_high_impact())   # Impact PREDICTS, but tests pass
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_impact_prediction_not_treated_as_proof(
        self, me, mi, mt, mr, mock_fixer
    ):
        """High-confidence impact + passing tests → clean, not regression_confirmed."""
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.status == "clean"
        mock_fixer.assert_not_called()

    @patch("sentinel.orchestrator.run_fixer")
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
        self, me, mi, mt, mr, mock_fixer
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.reproduction_result.passed is True
        assert result.status == "clean"


# ── I. NO FALSE NEGATIVES ────────────────────────────────────────────────────

class TestNoFalseNegatives:

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_genuine_regression_reaches_regression_confirmed(
        self, me, mi, mt, mr, mf
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        # Fixer fixed it; final status is fixed, but regression was confirmed along the way.
        assert result.fix_applied  # evidence preserved in report

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_failing_test_evidence_preserved_in_report(
        self, me, mi, mt, mr, mf
    ):
        result = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert result.reproduction_result is not None
        assert result.reproduction_result.passed is False

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_receives_complete_evidence(
        self, me, mi, mt, mr, mock_fixer
    ):
        orchestrate_investigation(["app/foo.py"], "- guard\n")
        ev: EvidenceReport = mock_fixer.call_args[0][0]
        assert ev.changed_files == ["app/foo.py"]
        assert ev.reproduction_result.passed is False
        assert ev.status == "regression_confirmed"
        assert ev.changed_behaviour  # populated from impact


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

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_regression_confirmed_to_fixed(self, me, mi, mt, mr, mf):
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status == "fixed"

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fix_failed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_regression_confirmed_plus_fix_failed_stays_regression_confirmed(
        self, me, mi, mt, mr, mf
    ):
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status == "regression_confirmed"

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_no_skip_from_potential_to_fixed(self, me, mi, mt, mr, mock_fixer):
        """Passing focused tests → clean.  Must not skip to fixed."""
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status != "fixed"
        mock_fixer.assert_not_called()


# ── K. REGRESSION SAFETY INVARIANTS ─────────────────────────────────────────

class TestRegressionSafety:

    @patch("sentinel.orchestrator.run_fixer")
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_passing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_clean_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixer_never_called_speculatively(self, me, mi, mt, mr, mock_fixer):
        orchestrate_investigation(["f.py"], "  ")
        mock_fixer.assert_not_called()

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_exit_codes_not_swallowed(self, me, mi, mt, mr, mf):
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.reproduction_result.exit_code != 0

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fixed_only_when_fixer_reports_fixed(self, me, mi, mt, mr, mf):
        r = orchestrate_investigation(["f.py"], "- guard\n")
        assert r.status == "fixed"

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fix_failed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_not_fixed_when_fixer_reports_fix_failed(self, me, mi, mt, mr, mf):
        r = orchestrate_investigation(["f.py"], "- guard\n")
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

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_fix_applied_propagated_to_report(self, me, mi, mt, mr, mf):
        r = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert r.fix_applied == "restored guard"

    @patch("sentinel.orchestrator.run_fixer",   return_value=_fixed_report())
    @patch("sentinel.orchestrator._safe_run_tests", return_value=_failing_result())
    @patch("sentinel.orchestrator.run_tester",  return_value=_empty_test_plan())
    @patch("sentinel.orchestrator.run_impact",  return_value=_high_impact())
    @patch("sentinel.orchestrator.run_explorer", return_value=_empty_code_map())
    def test_final_test_result_populated_after_fix(self, me, mi, mt, mr, mf):
        r = orchestrate_investigation(["app/foo.py"], "- guard\n")
        assert r.final_test_result is not None
        assert r.final_test_result.passed is True
