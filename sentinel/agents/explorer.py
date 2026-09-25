"""
sentinel/agents/explorer.py

Read-only Explorer agent.

Responsibility: given the raw git diff and the list of changed files, produce
a CodeMap that describes:
  - which symbols were modified
  - their direct call chains (up to ~3 hops)
  - which other files reference those symbols
  - which existing tests are relevant

All analysis is performed with standard-library tools (re, ast, pathlib).
No files are written.  No external dependencies are required.

TODO (Bob 2.0 integration): This module is designed to be driven by a Bob
subagent in Agent mode.  When the agent layer is wired up, the subagent will
call `run_explorer()` with the diff/changed-files payload and return the
resulting CodeMap to the Orchestrator.
"""

import ast
import re
from pathlib import Path

from sentinel.models import CodeMap


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_changed_symbols(diff: str) -> list[str]:
    """Return function/class names that appear in diff hunk headers or whose
    definitions are touched by the diff.

    Covers two common diff formats:
      @@ -n,m +n,m @@ def foo(...)     (git -p context lines)
      -def foo(  /  +def foo(           (added/removed definition lines)

    Test functions (names beginning with ``test_``) are excluded; they are
    discovered separately by _find_relevant_tests.
    """
    symbols: list[str] = []

    # Names embedded in unified diff hunk headers, e.g. @@ ... @@ def foo
    for match in re.finditer(r"@@[^@]*@@\s+(?:def|class)\s+(\w+)", diff):
        symbols.append(match.group(1))

    # Added or removed definition lines
    for match in re.finditer(r"^[+-]\s*(?:def|class)\s+(\w+)", diff, re.MULTILINE):
        symbols.append(match.group(1))

    # Deduplicate while preserving order; drop test_ names
    seen: set[str] = set()
    unique: list[str] = []
    for s in symbols:
        if s not in seen and not s.startswith("test_"):
            seen.add(s)
            unique.append(s)
    return unique


def _extract_all_diff_symbols(diff: str) -> list[str]:
    """Like _extract_changed_symbols but includes test_ names.

    Used only to feed _find_relevant_tests so that test functions added in
    the diff are still surfaced in CodeMap.existing_tests.
    """
    symbols: list[str] = []
    for match in re.finditer(r"@@[^@]*@@\s+(?:def|class)\s+(\w+)", diff):
        symbols.append(match.group(1))
    for match in re.finditer(r"^[+-]\s*(?:def|class)\s+(\w+)", diff, re.MULTILINE):
        symbols.append(match.group(1))
    seen: set[str] = set()
    unique: list[str] = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def _find_references(symbol: str, repo_root: Path) -> list[str]:
    """Search Python source files for AST-level references to *symbol*.

    Only counts a file as a reference when the symbol appears as an actual
    identifier (Name node) or attribute in the AST — not inside strings,
    comments, docstrings, or regex patterns.

    Files under the ``sentinel/`` package are excluded so that the sentinel
    implementation itself does not pollute application-level results.
    """
    referencing: list[str] = []
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".bob", "sentinel"}

    for py_file in repo_root.rglob("*.py"):
        if any(part in skip_dirs for part in py_file.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (OSError, SyntaxError):
            continue

        # Walk every AST node; match Name or Attribute uses of the symbol,
        # but skip nodes that are themselves definitions of it.
        found = False
        for node in ast.walk(tree):
            # Skip the definition of the symbol itself
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol:
                    continue
            if isinstance(node, ast.Name) and node.id == symbol:
                found = True
                break
            if isinstance(node, ast.Attribute) and node.attr == symbol:
                found = True
                break

        if found:
            referencing.append(str(py_file.relative_to(repo_root)))

    return sorted(referencing)


def _build_call_chain(symbols: list[str], repo_root: Path, max_hops: int = 3) -> list[str]:
    """Trace a shallow call chain for each symbol.

    Uses AST-level analysis to find callers within the repository up to
    *max_hops* levels deep.  Returns human-readable chain strings like:
      "clean_user_input → process_registration → (boundary)"
    """
    chains: list[str] = []

    for root_symbol in symbols:
        chain: list[str] = [root_symbol]
        current = root_symbol
        hops = 0

        while hops < max_hops:
            callers = _find_ast_callers(current, repo_root)
            if not callers:
                chain.append("(boundary)")
                break
            # Take the first caller for simplicity; a more complete
            # implementation would fan out into a graph.
            current = callers[0]
            chain.append(current)
            hops += 1

        chains.append(" → ".join(chain))

    return chains


def _find_ast_callers(symbol: str, repo_root: Path) -> list[str]:
    """Return the names of functions that call *symbol* anywhere in the repo."""
    callers: list[str] = []
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".bob"}

    for py_file in repo_root.rglob("*.py"):
        if any(part in skip_dirs for part in py_file.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for child in ast.walk(node):
                call_name = None
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name):
                        call_name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        call_name = child.func.attr
                if call_name == symbol and node.name != symbol:
                    if node.name not in callers:
                        callers.append(node.name)

    return callers


def _find_relevant_tests(symbols: list[str], repo_root: Path) -> list[str]:
    """Return test function names whose source references any of *symbols*."""
    relevant: list[str] = []
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".bob"}

    for py_file in repo_root.rglob("test_*.py"):
        if any(part in skip_dirs for part in py_file.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            func_source = ast.get_source_segment(source, node) or ""
            if any(re.search(rf"\b{re.escape(sym)}\b", func_source) for sym in symbols):
                relevant.append(node.name)

    return sorted(set(relevant))


# ── Public interface ───────────────────────────────────────────────────────────

def run_explorer(changed_files: list[str], diff: str) -> CodeMap:
    """Produce a CodeMap for the given changed files and diff.

    This is a pure, read-only function.  It does not write any files.

    Args:
        changed_files: Relative paths of files with uncommitted changes.
        diff:          Raw output of `git diff`.

    Returns:
        A populated CodeMap.
    """
    repo_root = Path.cwd()

    # All symbols touched by the diff (test functions excluded).
    changed_symbols = _extract_changed_symbols(diff)

    # existing_tests uses the full diff (including test_ names) so that newly
    # added regression tests are also discovered.
    all_diff_symbols = _extract_all_diff_symbols(diff)

    referencing_files: list[str] = []
    for symbol in changed_symbols:
        for ref in _find_references(symbol, repo_root):
            if ref not in referencing_files:
                referencing_files.append(ref)

    call_chain = _build_call_chain(changed_symbols, repo_root)
    existing_tests = _find_relevant_tests(changed_symbols + all_diff_symbols, repo_root)

    return CodeMap(
        changed_symbols=changed_symbols,
        call_chain=call_chain,
        referencing_files=referencing_files,
        existing_tests=existing_tests,
    )
