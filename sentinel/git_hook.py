"""
sentinel/git_hook.py

Git pre-push adapter for Dev Sentinel.

Responsibility:
  1. Parse Git's pre-push stdin protocol.
  2. Compute the correct diff for each ref being pushed.
  3. Feed the diff to the existing Orchestrator.
  4. Translate the EvidenceReport into a push decision (exit 0 / exit 1).
  5. Persist a confirmed regression incident under .git/dev-sentinel/last-incident.json
     so that `dev-sentinel fix` can later request explicit developer authorization.

This module is intentionally thin. All investigation logic lives in
sentinel/orchestrator.py and the agents beneath it.

─────────────────────────────────────────────────────────────────────────
GIT PRE-PUSH PROTOCOL
─────────────────────────────────────────────────────────────────────────

Git writes one line per ref to the hook's stdin:

    <local_ref> <local_sha> <remote_ref> <remote_sha>

Special values:
  - remote_sha == ZERO_SHA  → initial push; no base commit on remote.
  - local_sha  == ZERO_SHA  → ref deletion; skip (no code change).

─────────────────────────────────────────────────────────────────────────
DIFF STRATEGY
─────────────────────────────────────────────────────────────────────────

Normal push  (remote_sha exists):
    git diff <remote_sha>..<local_sha> -- *.py

Initial push (remote_sha is all zeros):
    Find the repository root commit with
      git rev-list --max-parents=0 HEAD
    then diff from that root commit to local_sha.
    This is conservative: the full history being introduced is analysed.

Multiple refs:
    Each ref is analysed independently. A block on any ref blocks the push.

─────────────────────────────────────────────────────────────────────────
WHY A SUCCESSFUL FIX STILL BLOCKS THE PUSH
─────────────────────────────────────────────────────────────────────────

The Fixer writes to the working tree.  The commit Git is about to push
already contains the broken code.  Allowing the push would ship the
regression.  The developer must review the Fixer's changes, stage them,
commit them, and push again.

Exit 1 is therefore returned regardless of whether the Fixer succeeded.

─────────────────────────────────────────────────────────────────────────
SAFETY INVARIANTS
─────────────────────────────────────────────────────────────────────────

This module NEVER:
  - modifies the index (no git add / git reset)
  - creates commits (no git commit)
  - pushes automatically (no git push)
  - stashes changes without user consent
  - deletes or rewrites history
  - modifies tracked source files

It ONLY:
  - reads Git metadata (read-only git commands)
  - passes data to the Orchestrator
  - persists evidence to .git/dev-sentinel/last-incident.json (untracked)
  - exits with 0 (allow) or 1 (block)
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sentinel.orchestrator import orchestrate_investigation
from sentinel.models import EvidenceReport

# The all-zeros SHA that Git uses to indicate "does not exist".
ZERO_SHA = "0" * 40


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PushRef:
    """One entry from Git's pre-push stdin."""
    local_ref:  str
    local_sha:  str
    remote_ref: str
    remote_sha: str

    @property
    def is_deletion(self) -> bool:
        """True when the push is deleting a remote ref."""
        return self.local_sha == ZERO_SHA

    @property
    def is_initial(self) -> bool:
        """True when the remote ref does not yet exist."""
        return self.remote_sha == ZERO_SHA


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(*args: str, check: bool = True) -> str:
    """Run a git command and return stripped stdout.

    Raises subprocess.CalledProcessError on non-zero exit when check=True.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _git_root() -> Path:
    """Return the absolute path of the repository root.

    Uses ``git rev-parse --show-toplevel`` so the result is correct regardless
    of the current working directory.

    Raises subprocess.CalledProcessError if not inside a Git repository.
    """
    return Path(_git("rev-parse", "--show-toplevel"))


def _root_commit() -> str:
    """Return the SHA of the repository's first-ever commit."""
    return _git("rev-list", "--max-parents=0", "HEAD")


def _diff_for_ref(ref: PushRef) -> tuple[list[str], str]:
    """Compute (changed_files, diff_text) for the commits being pushed.

    Returns ([], "") when there is nothing to analyse (e.g. deletion push).
    """
    if ref.is_deletion:
        return [], ""

    if ref.is_initial:
        # Initial push: analyse everything from the root commit to tip.
        base = _root_commit()
    else:
        base = ref.remote_sha

    tip = ref.local_sha

    # Restrict to Python source files to keep analysis focused.
    # Explorer already skips infrastructure directories, but limiting the
    # diff here avoids feeding unrelated binary blobs to the agents.
    changed_files_raw = _git(
        "diff", "--name-only", f"{base}..{tip}", "--", "*.py",
        check=False,
    )
    changed_files = [f for f in changed_files_raw.splitlines() if f.strip()]

    if not changed_files:
        return [], ""

    diff_text = _git(
        "diff", f"{base}..{tip}", "--", "*.py",
        check=False,
    )
    return changed_files, diff_text


# ── Incident persistence ──────────────────────────────────────────────────────

def _incident_path(git_root: Path) -> Path:
    """Return the path to the incident JSON file."""
    return git_root / ".git" / "dev-sentinel" / "last-incident.json"


def _persist_incident(ref: PushRef, report: EvidenceReport, git_root: Path) -> None:
    """Write a JSON incident record under .git/dev-sentinel/last-incident.json.

    Raises OSError if the file cannot be written.
    Does NOT modify tracked files, does NOT stage, commit, or push.
    """
    incident_dir = git_root / ".git" / "dev-sentinel"
    incident_dir.mkdir(parents=True, exist_ok=True)

    repro = report.reproduction_result
    incident: dict = {
        "local_ref":           ref.local_ref,
        "local_sha":           ref.local_sha,
        "remote_ref":          ref.remote_ref,
        "remote_sha":          ref.remote_sha,
        "changed_files":       report.changed_files,
        "changed_behaviour":   report.changed_behaviour,
        "affected_components": report.affected_components,
        "suspected_issue":     report.suspected_issue,
        "reproduction_result": {
            "passed":    repro.passed    if repro else None,
            "exit_code": repro.exit_code if repro else None,
            "stdout":    repro.stdout    if repro else "",
            "stderr":    repro.stderr    if repro else "",
        },
        "status": "regression_confirmed",
    }

    path = _incident_path(git_root)
    path.write_text(json.dumps(incident, indent=2), encoding="utf-8")


# ── stdin parser ──────────────────────────────────────────────────────────────

def parse_push_refs(stdin_text: str) -> list[PushRef]:
    """Parse Git's pre-push stdin into a list of PushRef objects.

    Lines have the form:
        <local_ref> <local_sha> <remote_ref> <remote_sha>
    Blank lines are ignored.
    """
    refs: list[PushRef] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        refs.append(PushRef(
            local_ref=parts[0],
            local_sha=parts[1],
            remote_ref=parts[2],
            remote_sha=parts[3],
        ))
    return refs


# ── Output helpers ────────────────────────────────────────────────────────────

def _print_header() -> None:
    print()
    print("🛡️  Dev Sentinel")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def _print_result(status: str, report: EvidenceReport) -> None:
    """Print a human-readable summary for the given pipeline status."""
    if status == "clean":
        print()
        print("✓  No regression detected.")
        print("   Push allowed.")
        print()
    elif status == "fixed":
        print()
        print("⚠  Regression confirmed.")
        print()
        print(f"🔧 Fix applied and verified: {report.fix_applied}")
        print()
        print("   Push BLOCKED.")
        print("   Review the generated fix, stage it, commit it,")
        print("   then push again.")
        print()
    elif status == "regression_confirmed":
        print()
        print("✗ PUSH BLOCKED")
        print()
        print("Regression confirmed")
        print()

        if report.changed_files:
            print("📍 Changed files")
            for f in report.changed_files:
                print(f"   {f}")
            print()

        repro = report.reproduction_result
        if repro:
            print("🧪 Evidence")
            print(f"   Tests passed: {repro.passed}")
            print(f"   Exit code:    {repro.exit_code}")
            if repro.stdout.strip():
                # Show a condensed excerpt (last 10 lines of stdout)
                lines = repro.stdout.strip().splitlines()
                excerpt = lines[-10:] if len(lines) > 10 else lines
                for line in excerpt:
                    print(f"   {line}")
            print()

        if report.affected_components:
            print("Affected:")
            for comp in report.affected_components:
                print(f"   {comp}")
            print()

        if report.suspected_issue:
            print("🔎 Suspected cause")
            # Wrap long suspected_issue text sensibly
            for line in report.suspected_issue.splitlines():
                print(f"   {line}")
            print()

        print("→ Run `dev-sentinel fix` to review and authorize a fix.")
        print()
    elif status == "analysis_failed":
        print()
        print("⚠ PUSH BLOCKED")
        print()
        print("Dev Sentinel could not complete its analysis.")
        if report.suspected_issue:
            print("Reason:")
            print(f"   {report.suspected_issue}")
            print()
            print("   No fix was applied.")
            print("   Resolve the analysis problem and push again.")
            print()
    else:
        # "pending" or any unexpected value — treat conservatively
        print()
        print(f"⚠  Dev Sentinel returned unexpected status: {status!r}")
        print("   Push BLOCKED as a safety precaution.")
        print()


# ── Exit-code decision ────────────────────────────────────────────────────────

def _exit_code(status: str) -> int:
    """Return the shell exit code for the given EvidenceReport status.

    Only "clean" allows the push (exit 0).

    "fixed" always blocks even though the Fixer succeeded, because the
    repair only exists in the working tree and has not been committed yet.
    """
    return 0 if status == "clean" else 1


# ── Main entry point ──────────────────────────────────────────────────────────

def run(stdin_text: str) -> int:
    """Full pre-push hook logic.

    Args:
        stdin_text: Raw text from Git's pre-push stdin.

    Returns:
        0 to allow the push, 1 to block it.
    """
    _print_header()

    refs = parse_push_refs(stdin_text)

    if not refs:
        # Git gave us nothing — allow push (nothing to analyse).
        print()
        print("✓  No refs to analyse.")
        print("   Push allowed.")
        print()
        return 0

    # Resolve the git root once for this run (used for incident persistence).
    try:
        git_root = _git_root()
    except subprocess.CalledProcessError:
        git_root = None  # best-effort; persistence will be skipped if None

    # Analyse each ref independently. Block on the first regression found.
    for ref in refs:
        if ref.is_deletion:
            # Deleting a remote ref introduces no code — skip.
            continue

        push_type = "initial push" if ref.is_initial else f"{ref.remote_sha[:8]}..{ref.local_sha[:8]}"
        print(f"\n   Analysing {ref.local_ref} ({push_type}) …")

        try:
            changed_files, diff_text = _diff_for_ref(ref)
        except subprocess.CalledProcessError as exc:
            print(f"\n✗  Dev Sentinel: git command failed: {exc}")
            print("   Push BLOCKED as a safety precaution.")
            return 1

        if not changed_files:
            print("   No Python changes detected — skipping.")
            continue

        print(f"   {len(changed_files)} Python file(s) changed.")

        try:
            report = orchestrate_investigation(changed_files, diff_text)
        except Exception as exc:  # noqa: BLE001
            print(f"\n✗  Dev Sentinel raised an unexpected exception: {exc}")
            print("   Push BLOCKED as a safety precaution.")
            return 1

        if report.status == "regression_confirmed":
            # Persist the evidence before printing so the user is told
            # the correct next step even if persistence fails.
            if git_root is not None:
                try:
                    _persist_incident(ref, report, git_root)
                except OSError as exc:
                    # Persistence failed — still block, but warn clearly.
                    _print_result(report.status, report)
                    print(
                        f"⚠  WARNING: The regression was confirmed but the evidence "
                        f"could not be saved ({exc})."
                    )
                    print(
                        "   `dev-sentinel fix` is NOT available until the incident "
                        "can be written to .git/dev-sentinel/last-incident.json."
                    )
                    print()
                    return 1
            else:
                _print_result(report.status, report)
                print(
                    "⚠  WARNING: Could not determine the git root — "
                    "incident evidence was NOT persisted."
                )
                print(
                    "   `dev-sentinel fix` will not be available."
                )
                print()
                return 1

        _print_result(report.status, report)

        if _exit_code(report.status) != 0:
            return 1

    # All refs passed.
    return 0


def main() -> None:
    """CLI entry point called by the shell hook."""
    stdin_text = sys.stdin.read()
    sys.exit(run(stdin_text))


if __name__ == "__main__":
    main()
