"""
sentinel/agents/fixer.py

Fixer agent.

Principle: "No fix without reproduction."

Responsibility: given confirmed regression evidence (an EvidenceReport whose
reproduction_result shows a test failure), apply the SMALLEST production-code
fix that restores the behaviour established by the failing tests.

The Fixer:
  - refuses to act on unconfirmed or speculative issues
  - inspects the failing source context before modifying anything
  - applies only what is strictly necessary to make the failing tests pass
  - reruns the focused tests to verify the fix
  - then runs the full suite to verify no regressions were introduced
  - returns a FixReport regardless of outcome

No tests are modified. No unrelated code is touched.

TODO (Bob 2.0 integration): This module is designed to be driven by a Bob
subagent in Agent mode. When the agent layer is wired up, the subagent will
call `run_fixer()` with the EvidenceReport payload and return the resulting
FixReport to the Orchestrator.
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel.models import EvidenceReport, TestResult
from sentinel.runner import run_tests


# ── FixReport ─────────────────────────────────────────────────────────────────

@dataclass
class FixReport:
    """Structured result produced by the Fixer agent."""

    fix_justified: bool = False
    files_modified: list[str] = field(default_factory=list)
    original_failure: str = ""
    root_cause: str = ""
    fix_applied: str = ""
    fix_rationale: str = ""
    focused_test_result: TestResult | None = None
    full_test_result: TestResult | None = None
    status: str = "no_fix_applied"   # "no_fix_applied" | "fixed" | "fix_failed"
    remaining_uncertainty: str = ""


# ── Guard-pattern library ─────────────────────────────────────────────────────
#
# Each entry describes a class of missing guard that the Fixer knows how to
# restore.  The Fixer only acts on patterns it recognises — it does not
# attempt generic code synthesis.
#
# "trigger_exceptions" : exception names that signal this pattern is missing
# "unguarded_re"       : matches the unguarded call that must be wrapped
# "build_replacement"  : callable(original_line, indent) -> replacement lines

def _build_none_and_empty_guard(original_line: str, indent: str) -> str:
    """Return the guarded replacement for a bare float() assignment.

    Transforms:
        cleaned_data["user_score"] = float(data["user_score"])
    into:
        value = data["user_score"]
        if value is None or value == "":
            cleaned_data["user_score"] = 0.0
        else:
            cleaned_data["user_score"] = float(value)
    """
    # Extract the dict key being assigned to (e.g. "user_score") and the
    # source expression (e.g. data["user_score"]).
    assign_match = re.search(
        r'(\w+)\[(["\'])(\w+)\2\]\s*=\s*float\((.+)\)', original_line.strip()
    )
    if not assign_match:
        return original_line  # cannot parse — leave unchanged (safe fallback)

    target_dict = assign_match.group(1)     # e.g. cleaned_data
    key_quote   = assign_match.group(2)     # ' or "
    key_name    = assign_match.group(3)     # e.g. user_score
    source_expr = assign_match.group(4)     # e.g. data["user_score"]

    lines = [
        f"{indent}value = {source_expr}",
        f"{indent}if value is None or value == \"\":",
        f"{indent}    {target_dict}[{key_quote}{key_name}{key_quote}] = 0.0",
        f"{indent}else:",
        f"{indent}    {target_dict}[{key_quote}{key_name}{key_quote}] = float(value)",
    ]
    return "\n".join(lines)


_GUARD_PATTERNS = [
    {
        # Matches: cleaned_data["user_score"] = float(data["user_score"])
        # Triggered by: TypeError (None) or ValueError (empty string)
        "trigger_exceptions": {"TypeError", "ValueError"},
        "unguarded_re": re.compile(
            r'^\s*\w+\[["\'][\w]+["\']\]\s*=\s*float\(.+\)\s*$'
        ),
        "build_replacement": _build_none_and_empty_guard,
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_exception_names(failure_output: str) -> set[str]:
    """Pull exception class names out of a pytest failure traceback string."""
    names: set[str] = set()
    for match in re.finditer(r'\bE\s+([\w]+Error|[\w]+Exception)\b', failure_output):
        names.add(match.group(1))
    # Also catch bare "TypeError:" / "ValueError:" lines
    for match in re.finditer(r'\b([\w]+Error|[\w]+Exception):', failure_output):
        names.add(match.group(1))
    return names


def _extract_failing_file(failure_output: str, changed_files: list[str]) -> str | None:
    """Return the source file most likely responsible for the failure.

    Prefers changed files that appear in the traceback; falls back to the
    first changed file.
    """
    for cf in changed_files:
        if cf in failure_output:
            return cf
    return changed_files[0] if changed_files else None


def _find_unguarded_line(
    source: str,
    pattern: re.Pattern,
) -> tuple[int, str] | None:
    """Return (line_number_0indexed, line_text) for the first line matching
    *pattern* in *source*, or None if not found.
    """
    for idx, line in enumerate(source.splitlines()):
        if pattern.search(line):
            return idx, line
    return None


def _apply_guard(source: str, line_idx: int, original_line: str, build_fn: Any) -> str:
    """Replace the unguarded line at *line_idx* with the guarded equivalent."""
    lines = source.splitlines(keepends=True)
    indent = len(original_line) - len(original_line.lstrip())
    indent_str = " " * indent
    replacement = build_fn(original_line, indent_str) + "\n"
    lines[line_idx] = replacement
    return "".join(lines)


# ── Public interface ──────────────────────────────────────────────────────────

def run_fixer(evidence: EvidenceReport) -> FixReport:
    """Attempt to apply the minimal fix implied by the confirmed regression
    evidence.

    Args:
        evidence: An EvidenceReport produced by the Orchestrator.  The Fixer
                  only acts when evidence.status == "regression_confirmed" and
                  evidence.reproduction_result shows a test failure.

    Returns:
        A FixReport describing what (if anything) was done and the test
        results after the attempted fix.
    """
    # ── Guard: fix must be justified by confirmed evidence ────────────────────
    if evidence.status != "regression_confirmed":
        return FixReport(
            fix_justified=False,
            status="no_fix_applied",
            remaining_uncertainty=(
                f"Evidence status is '{evidence.status}', not 'regression_confirmed'. "
                "No fix was applied."
            ),
        )

    if evidence.reproduction_result is None or evidence.reproduction_result.passed:
        return FixReport(
            fix_justified=False,
            status="no_fix_applied",
            remaining_uncertainty=(
                "Reproduction result is absent or passing. "
                "A fix requires executable failing evidence."
            ),
        )

    report = FixReport(fix_justified=True)
    failure_output = (
        evidence.reproduction_result.stdout + evidence.reproduction_result.stderr
    )
    report.original_failure = failure_output

    # ── Identify which exception(s) drove the failure ─────────────────────────
    triggered_exceptions = _extract_exception_names(failure_output)

    # ── Identify the source file to patch ─────────────────────────────────────
    failing_file = _extract_failing_file(
        failure_output, evidence.changed_files
    )
    if not failing_file:
        report.status = "no_fix_applied"
        report.remaining_uncertainty = (
            "Could not identify a source file to patch from the failure output."
        )
        return report

    source_path = Path.cwd() / failing_file
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.status = "no_fix_applied"
        report.remaining_uncertainty = f"Could not read {failing_file}: {exc}"
        return report

    # ── Match a known guard pattern ───────────────────────────────────────────
    matched_pattern = None
    for entry in _GUARD_PATTERNS:
        if triggered_exceptions & entry["trigger_exceptions"]:
            hit = _find_unguarded_line(source, entry["unguarded_re"])
            if hit is not None:
                matched_pattern = entry
                line_idx, original_line = hit
                break

    if matched_pattern is None:
        report.status = "no_fix_applied"
        report.remaining_uncertainty = (
            "No recognised guard pattern matched the failing source. "
            "Manual review required."
        )
        return report

    # ── Apply the patch ───────────────────────────────────────────────────────
    patched_source = _apply_guard(
        source, line_idx, original_line, matched_pattern["build_replacement"]
    )

    # Describe what the fix does before writing anything.
    report.root_cause = evidence.suspected_issue or (
        "Unguarded float() conversion raises on None and empty-string inputs."
    )
    report.fix_applied = (
        f"Restored None/empty-string guard around float() at "
        f"{failing_file}:{line_idx + 1}"
    )
    report.fix_rationale = (
        "The guard is the minimal change that restores the contracted behaviour "
        "(None and '' normalise to 0.0) without altering any other code path."
    )

    source_path.write_text(patched_source, encoding="utf-8")
    report.files_modified.append(failing_file)

    # ── Run focused tests ─────────────────────────────────────────────────────
    focused_target = (
        evidence.reproduction_result.stdout
        # derive the test file from the first FAILED line in the output
    )
    # Extract target test file from failure output; fall back to full suite
    test_file_match = re.search(r'(tests/\S+\.py)', failure_output)
    focused_file = test_file_match.group(1) if test_file_match else None

    focused_result = run_tests(focused_file)
    report.focused_test_result = focused_result

    if not focused_result.passed:
        report.status = "fix_failed"
        report.remaining_uncertainty = (
            "Focused tests still fail after the patch. "
            "The applied fix did not fully resolve the regression."
        )
        return report

    # ── Run full suite ────────────────────────────────────────────────────────
    full_result = run_tests()
    report.full_test_result = full_result

    if not full_result.passed:
        report.status = "fix_failed"
        report.remaining_uncertainty = (
            "Focused tests pass but the full suite reveals a new failure. "
            "The patch introduced a regression elsewhere."
        )
        return report

    report.status = "fixed"
    report.remaining_uncertainty = ""
    return report
