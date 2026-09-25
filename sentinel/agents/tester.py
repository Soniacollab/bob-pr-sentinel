"""
sentinel/agents/tester.py

Read-only Tester agent.

Responsibility: translate suspected behavioral changes (from an ImpactReport
and a CodeMap) into a focused, executable TestPlan.

Principle: "Impact predicts. Tests prove."

The Tester does NOT declare that a regression exists. It produces the minimal
set of test specifications required to establish evidence — reusing existing
tests wherever they already cover the suspected behavior, and generating new
TestCaseSpecs only where genuine coverage gaps exist.

No repository files are written here. The Orchestrator decides whether to
materialise the TestPlan into actual test code and hand it to runner.py.

TODO (Bob 2.0 integration): This module is designed to be driven by a Bob
subagent in Agent mode. When the agent layer is wired up, the subagent will
call `run_tester()` with the CodeMap/ImpactReport payload and return the
resulting TestPlan to the Orchestrator.
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel.models import CodeMap, ImpactReport, TestCaseSpec, TestPlan


# ── Internal coverage record ──────────────────────────────────────────────────

@dataclass
class _CoverageRecord:
    """Tracks whether a suspected input is already covered by an existing test."""

    input_value: str          # e.g. "None", '""'
    exception_type: str       # e.g. "TypeError"
    covered_by: str = ""      # test function name if coverage found, else ""
    covered: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _locate_test_files(repo_root: Path) -> list[Path]:
    """Return all test_*.py files, skipping infrastructure directories."""
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".bob", "sentinel"}
    return [
        p for p in repo_root.rglob("test_*.py")
        if not any(part in skip_dirs for part in p.parts)
    ]


def _parse_test_assertions(func_node: ast.FunctionDef, source: str) -> list[str]:
    """Extract the text of all assert statements in a test function."""
    assertions: list[str] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            segment = ast.get_source_segment(source, node) or ""
            assertions.append(segment.strip())
    return assertions


def _function_covers_input(
    func_node: ast.FunctionDef,
    source: str,
    input_value: str,
    guarded_key: str = "",
) -> bool:
    """Return True when the test function passes *input_value* to the system
    under test in a way that exercises the guarded code path.

    Conservative definition of coverage:
      The literal value (None or "") must appear as a **dict value** in the
      function body, not merely as a dict key, a comparison operand in an
      assertion, or as a standalone expression.  When *guarded_key* is known,
      the literal must be the value paired with that specific key inside a dict
      literal, ruling out false positives where the same literal is the value
      for an unrelated key.

    Variable indirection (``score = None; {"user_score": score}``) is handled:
      if a Name node whose assignment resolves to the target constant is used
      as the dict value, it counts.

    Cases explicitly excluded:
      - ``assert result.get("x") is None``  (None in comparison, not dict value)
      - ``{"username": None, "user_score": "85"}``  (None under a different key)
      - ``assert result == ""``  (empty string in comparison only)
    """
    if input_value == "None":
        target_const: Any = None
    elif input_value == '""':
        target_const = ""
    else:
        # Fallback for other value types: source-text presence is good enough.
        func_source = ast.get_source_segment(source, func_node) or ""
        return input_value in func_source

    # Build a map of simple variable assignments to constant values within the
    # function body so we can resolve one level of variable indirection.
    const_vars: dict[str, Any] = {}
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
            ):
                const_vars[node.targets[0].id] = node.value.value

    # Walk every Dict literal in the function and check whether the target
    # constant (or a variable resolving to it) is a *value* under an
    # appropriate key.
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, val_node in zip(node.keys, node.values):
            # Determine what value this dict slot holds.
            if isinstance(val_node, ast.Constant):
                slot_value = val_node.value
            elif isinstance(val_node, ast.Name) and val_node.id in const_vars:
                slot_value = const_vars[val_node.id]
            else:
                continue

            if slot_value != target_const:
                continue

            # The target value is in this slot.  If we know the guarded key,
            # require that this slot's key matches it.
            if guarded_key:
                if isinstance(key_node, ast.Constant) and key_node.value == guarded_key:
                    return True
                # key is a variable — conservative: skip (cannot verify)
            else:
                # No guarded key known: accept any dict-value match.
                return True

    return False


def _check_coverage(
    affected_inputs: list[dict[str, Any]],
    test_files: list[Path],
    repo_root: Path,
    guarded_key: str = "",
) -> list[_CoverageRecord]:
    """For each affected input, determine whether an existing test covers it.

    *guarded_key* is the dict key whose value is being guarded (e.g.
    ``"user_score"``).  When provided, coverage checking requires that the
    target literal appears as the value for *that specific key* in a dict
    literal, preventing false positives from unrelated fields.

    Returns one _CoverageRecord per affected input.
    """
    records: list[_CoverageRecord] = []

    for inp in affected_inputs:
        value = inp.get("value", "")
        exception = inp.get("exception", "")
        record = _CoverageRecord(input_value=value, exception_type=exception)

        for test_file in test_files:
            try:
                source = test_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(test_file))
            except (OSError, SyntaxError):
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if not node.name.startswith("test_"):
                    continue
                if _function_covers_input(node, source, value, guarded_key=guarded_key):
                    record.covered = True
                    record.covered_by = node.name
                    break

            if record.covered:
                break

        records.append(record)

    return records


def _infer_target_test_file(
    code_map: CodeMap,
    repo_root: Path,
) -> str:
    """Determine the most appropriate test file for new regression tests.

    Priority:
    1. A test file in referencing_files that already imports the changed symbol.
    2. The first test_*.py file discovered in the repo.
    3. A safe fallback path.
    """
    for ref in code_map.referencing_files:
        ref_path = repo_root / ref
        if ref_path.name.startswith("test_") and ref_path.exists():
            return ref

    test_files = _locate_test_files(repo_root)
    if test_files:
        return str(test_files[0].relative_to(repo_root))

    return "tests/test_regression.py"


def _make_test_case_spec(
    record: _CoverageRecord,
    code_map: CodeMap,
    target_file: str,
) -> TestCaseSpec:
    """Build a TestCaseSpec for an uncovered affected input."""
    symbol = code_map.changed_symbols[0] if code_map.changed_symbols else "unknown"

    # Derive a clean test name from the symbol and the input value.
    safe_value = (
        record.input_value
        .replace('"', "")
        .replace("'", "")
        .replace(" ", "_")
        .lower()
        .strip("_")
    )
    safe_value = safe_value or "empty"
    name = f"test_{symbol}_{safe_value}_input"

    # Represent the input as a dict where the symbol's first parameter gets
    # the sentinel value. The key is inferred from the impact description;
    # fall back to "input" when it cannot be determined.
    param_key = _infer_input_param(code_map)
    raw_value: Any = None if record.input_value == "None" else (
        "" if record.input_value == '""' else record.input_value
    )

    return TestCaseSpec(
        name=name,
        input={param_key: raw_value},
        expected=0.0,   # guard-removal patterns default to a zero/safe value
        target_file=target_file,
    )


def _infer_input_param(code_map: CodeMap) -> str:
    """Best-effort inference of the first non-self parameter of the changed
    symbol by inspecting its definition in the repository.

    Falls back to "input" if the definition cannot be parsed.
    """
    repo_root = Path.cwd()
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".bob"}

    for symbol in code_map.changed_symbols:
        for py_file in repo_root.rglob("*.py"):
            if any(part in skip_dirs for part in py_file.parts):
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                continue

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == symbol:
                        args = node.args.args
                        non_self = [a.arg for a in args if a.arg != "self"]
                        if non_self:
                            return non_self[0]

    return "input"


def _infer_guarded_key(code_map: CodeMap) -> str:
    """Infer the dict key that is subscript-accessed on the first parameter of
    the changed symbol, i.e. the key whose value is being guarded.

    For ``clean_user_input(data)`` that does ``data["user_score"]``, this
    returns ``"user_score"``.

    Falls back to an empty string when it cannot be determined, which causes
    coverage checking to accept any dict-value match (safe but less precise).
    """
    repo_root = Path.cwd()
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".bob"}

    for symbol in code_map.changed_symbols:
        for py_file in repo_root.rglob("*.py"):
            if any(part in skip_dirs for part in py_file.parts):
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                continue

            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if func.name != symbol:
                    continue
                args = func.args.args
                non_self = [a.arg for a in args if a.arg != "self"]
                if not non_self:
                    continue
                param = non_self[0]
                # Find the first subscript data["key"] on the parameter.
                for node in ast.walk(func):
                    if (
                        isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == param
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)
                    ):
                        return node.slice.value

    return ""



def _describe_rationale(
    records: list[_CoverageRecord],
    impact: ImpactReport,
) -> str:
    """Compose a human-readable rationale for the TestPlan."""
    parts: list[str] = []

    if impact.confidence != "low":
        parts.append(f"Impact confidence: {impact.confidence}.")
    if impact.changed_behaviour:
        parts.append(f"Suspected change: {impact.changed_behaviour}.")

    covered = [r for r in records if r.covered]
    uncovered = [r for r in records if not r.covered]

    if covered:
        names = ", ".join(r.covered_by for r in covered)
        values = ", ".join(r.input_value for r in covered)
        parts.append(
            f"Existing coverage for [{values}]: {names} — reused as primary proof target."
        )

    if uncovered:
        values = ", ".join(r.input_value for r in uncovered)
        parts.append(
            f"No existing coverage for [{values}] — new TestCaseSpec(s) generated."
        )

    if not records:
        parts.append("No meaningful affected inputs identified — no focused tests generated.")

    return "  ".join(parts)


# ── Public interface ──────────────────────────────────────────────────────────

def run_tester(
    changed_files: list[str],
    diff: str,
    code_map: CodeMap,
    impact: ImpactReport,
) -> TestPlan:
    """Produce a TestPlan from the Explorer's CodeMap and Impact's ImpactReport.

    This is a pure, read-only function. It does not write or execute anything.

    Decision logic:
    1. If there are no changed files or the diff is empty → empty TestPlan.
    2. If ImpactReport has no meaningful affected inputs → empty TestPlan.
    3. For each affected input:
       a. Check whether an existing test already covers it.
       b. If covered  → record it as the reuse target (no new spec needed).
       c. If uncovered → generate a TestCaseSpec.
    4. Attach rationale describing the evidence each test would establish.

    Args:
        changed_files: Relative paths of files with uncommitted changes.
        diff:          Raw output of `git diff`.
        code_map:      CodeMap from the Explorer agent.
        impact:        ImpactReport from the Impact agent.

    Returns:
        A TestPlan. new_tests is empty when existing coverage is sufficient or
        when no regression evidence warrants new test generation.
    """
    repo_root = Path.cwd()

    # ── Guard: nothing to test ────────────────────────────────────────────────
    if not changed_files or not diff.strip():
        return TestPlan(
            target_file="",
            new_tests=[],
            rationale="No changed files or empty diff — no focused tests required.",
        )

    # ── Guard: no meaningful impact ───────────────────────────────────────────
    if not impact.affected_inputs and impact.confidence == "low":
        return TestPlan(
            target_file="",
            new_tests=[],
            rationale=(
                "ImpactReport contains no affected inputs and confidence is low — "
                "no focused regression test is justified."
            ),
        )

    # ── Locate test files and check coverage ──────────────────────────────────
    test_files = _locate_test_files(repo_root)
    # Infer the dict key being guarded (e.g. "user_score") so coverage checking
    # can distinguish the target field from unrelated dict keys in test bodies.
    guarded_key = _infer_guarded_key(code_map)
    coverage_records = _check_coverage(
        impact.affected_inputs, test_files, repo_root, guarded_key=guarded_key
    )

    target_file = _infer_target_test_file(code_map, repo_root)

    # ── Build new TestCaseSpecs only for uncovered inputs ─────────────────────
    new_tests: list[TestCaseSpec] = []
    for record in coverage_records:
        if not record.covered:
            new_tests.append(_make_test_case_spec(record, code_map, target_file))

    rationale = _describe_rationale(coverage_records, impact)

    return TestPlan(
        target_file=target_file,
        new_tests=new_tests,
        rationale=rationale,
    )
