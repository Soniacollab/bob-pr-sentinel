"""
sentinel/git_hook.py

Git pre-push adapter for Dev Sentinel.

Responsibility:
  1. Parse Git's pre-push stdin protocol.
  2. Compute the correct diff for each ref being pushed.
  3. Feed the diff to the existing Orchestrator.
  4. Translate the EvidenceReport into a push decision (exit 0 / exit 1).

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

It ONLY:
  - reads Git metadata (read-only git commands)
  - passes data to the Orchestrator
  - exits with 0 (allow) or 1 (block)
"""

import subprocess
import sys
from dataclasses import dataclass

from sentinel.orchestrator import orchestrate_investigation

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


def _print_result(status: str, fix_applied: str) -> None:
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
        print(f"🔧 Fix applied and verified: {fix_applied}")
        print()
        print("   Push BLOCKED.")
        print("   Review the generated fix, stage it, commit it,")
        print("   then push again.")
        print()
    elif status == "regression_confirmed":
        print()
        print("✗  Regression confirmed — fix could not be applied or verified.")
        print()
        print("   Push BLOCKED.")
        print("   Resolve the regression before pushing.")
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

        _print_result(report.status, report.fix_applied)

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
