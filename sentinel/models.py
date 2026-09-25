from dataclasses import dataclass, field
from typing import Any


@dataclass
class CodeMap:
    """Output of the Explorer subagent: structural description of what changed."""

    changed_symbols: list[str] = field(default_factory=list)
    call_chain: list[str] = field(default_factory=list)
    referencing_files: list[str] = field(default_factory=list)
    existing_tests: list[str] = field(default_factory=list)


@dataclass
class ImpactReport:
    """Output of the Impact subagent: behavioral analysis of the change."""

    changed_behaviour: str = ""
    affected_inputs: list[dict[str, Any]] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    confidence: str = "low"   # "low" | "medium" | "high"
    reasoning: str = ""


@dataclass
class TestCaseSpec:
    """A single regression test case as designed by the Test subagent."""

    name: str
    input: dict[str, Any]
    expected: Any
    target_file: str


@dataclass
class TestPlan:
    """Output of the Test subagent: regression tests to be written."""

    target_file: str
    new_tests: list[TestCaseSpec] = field(default_factory=list)
    rationale: str = ""


@dataclass
class TestResult:
    """Outcome of a pytest run produced by sentinel.runner."""

    passed: bool
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class EvidenceReport:
    """Final report produced by the Orchestrator after the full pipeline."""

    changed_files: list[str] = field(default_factory=list)
    changed_behaviour: str = ""
    affected_components: list[str] = field(default_factory=list)
    suspected_issue: str = ""
    reproduction_result: TestResult | None = None
    fix_applied: str = ""
    final_test_result: TestResult | None = None
    status: str = "pending"   # "pending" | "clean" | "regression_confirmed" | "fixed"
