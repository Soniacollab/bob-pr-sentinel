"""
sentinel/agents/impact.py

Read-only Impact agent.

Responsibility: given the raw git diff and a CodeMap from the Explorer,
produce an ImpactReport that describes:
  - what behaviour changed
  - which input values are now affected
  - which components downstream are impacted
  - confidence level (low / medium / high)
  - human-readable reasoning

The analysis is entirely evidence-driven: only facts present in the diff and
CodeMap are used.  No facts are invented.  No files are written.

TODO (Bob 2.0 integration): This module is designed to be driven by a Bob
subagent in Agent mode.  When the agent layer is wired up, the subagent will
call `run_impact()` with the diff/CodeMap payload and return the resulting
ImpactReport to the Orchestrator.
"""

import re
from sentinel.models import CodeMap, ImpactReport


# ── Pattern library ────────────────────────────────────────────────────────────
# Each entry describes a recognizable class of defensive removal.
# "pattern"   : regex matched against the removed lines of the diff (-lines)
# "behaviour" : human-readable description of what was removed
# "inputs"    : affected input values with the exception that will be raised

_GUARD_PATTERNS: list[dict] = [
    {
        "pattern": re.compile(
            r"^\-\s*if\s+\w+\s+is\s+None\b", re.MULTILINE
        ),
        "behaviour": "None guard removed — None input is no longer handled",
        "inputs": [{"value": "None", "exception": "TypeError"}],
    },
    {
        "pattern": re.compile(
            r'^\-\s*if\s+\w+\s*==\s*""', re.MULTILINE
        ),
        "behaviour": "Empty-string guard removed — empty-string input is no longer handled",
        "inputs": [{"value": '""', "exception": "ValueError"}],
    },
    {
        "pattern": re.compile(
            r"^\-\s*if\s+.+is\s+None.+==\s*\"\"", re.MULTILINE
        ),
        "behaviour": "Combined None/empty-string guard removed",
        "inputs": [
            {"value": "None", "exception": "TypeError"},
            {"value": '""', "exception": "ValueError"},
        ],
    },
    {
        "pattern": re.compile(
            r"^\-\s*if\s+\w+\s*<\s*0", re.MULTILINE
        ),
        "behaviour": "Negative-value guard removed",
        "inputs": [{"value": "negative number", "exception": "ValueError or logic error"}],
    },
    {
        "pattern": re.compile(
            r"^\-\s*(?:try|except)\b", re.MULTILINE
        ),
        "behaviour": "Exception handler removed — previously caught exceptions now propagate",
        "inputs": [{"value": "(any previously caught input)", "exception": "propagated"}],
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _removed_lines(diff: str) -> str:
    """Return only the lines removed by the diff (lines starting with -)."""
    return "\n".join(
        line for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )


def _added_lines(diff: str) -> str:
    """Return only the lines added by the diff (lines starting with +)."""
    return "\n".join(
        line for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _detect_guard_removal(diff: str) -> tuple[list[str], list[dict], str]:
    """Scan the diff for removal of known defensive patterns.

    Returns:
        behaviours  : list of human-readable behaviour descriptions
        inputs      : aggregated list of affected input dicts
        confidence  : "high" | "medium" | "low"
    """
    removed = _removed_lines(diff)
    behaviours: list[str] = []
    inputs: list[dict] = []

    for entry in _GUARD_PATTERNS:
        if entry["pattern"].search(removed):
            behaviours.append(entry["behaviour"])
            for inp in entry["inputs"]:
                if inp not in inputs:
                    inputs.append(inp)

    if behaviours:
        confidence = "high"
    elif _removed_lines(diff).strip():
        confidence = "medium"
    else:
        confidence = "low"

    return behaviours, inputs, confidence


def _build_reasoning(
    behaviours: list[str],
    affected_inputs: list[dict],
    code_map: CodeMap,
    confidence: str,
) -> str:
    """Compose a concise reasoning string from evidence."""
    parts: list[str] = []

    if behaviours:
        parts.append("Removed guards: " + "; ".join(behaviours) + ".")

    if affected_inputs:
        inp_strs = [
            f"{i['value']} → {i['exception']}" for i in affected_inputs
        ]
        parts.append("Unhandled inputs: " + ", ".join(inp_strs) + ".")

    if code_map.call_chain:
        parts.append("Call chain: " + "; ".join(code_map.call_chain) + ".")

    if code_map.referencing_files:
        parts.append(
            "Affected files: " + ", ".join(code_map.referencing_files) + "."
        )

    if not parts:
        parts.append("No guard removals detected in the diff.")

    parts.append(f"Confidence: {confidence}.")
    return "  ".join(parts)


# ── Public interface ───────────────────────────────────────────────────────────

def run_impact(changed_files: list[str], diff: str, code_map: CodeMap) -> ImpactReport:
    """Produce an ImpactReport from the diff and the Explorer's CodeMap.

    This is a pure, read-only function.  It does not write any files and does
    not traverse the repository independently.

    Args:
        changed_files: Relative paths of files with uncommitted changes.
        diff:          Raw output of `git diff`.
        code_map:      CodeMap produced by the Explorer agent.

    Returns:
        A populated ImpactReport.
    """
    behaviours, affected_inputs, confidence = _detect_guard_removal(diff)

    # Affected components are the files/symbols downstream of the changed code,
    # taken directly from the CodeMap rather than re-derived here.
    affected_components: list[str] = list(code_map.referencing_files)
    if code_map.changed_symbols:
        affected_components = code_map.changed_symbols + [
            c for c in affected_components if c not in code_map.changed_symbols
        ]

    changed_behaviour = (
        "; ".join(behaviours)
        if behaviours
        else "No recognizable guard removal detected."
    )

    reasoning = _build_reasoning(behaviours, affected_inputs, code_map, confidence)

    return ImpactReport(
        changed_behaviour=changed_behaviour,
        affected_inputs=affected_inputs,
        affected_components=affected_components,
        confidence=confidence,
        reasoning=reasoning,
    )
