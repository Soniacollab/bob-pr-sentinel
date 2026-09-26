"""
sentinel/cli.py

Top-level CLI entry point for the `dev-sentinel` command.

Sub-commands
------------
dev-sentinel install [--target DIR]
    Install the pre-push hook into DIR (default: current directory).

dev-sentinel uninstall [--target DIR]
    Remove the Sentinel hook configuration from DIR.

dev-sentinel run
    Run the investigation pipeline against the current working-tree diff
    (the original interactive mode, equivalent to running dev_sentinel.py).

dev-sentinel fix
    Load the persisted regression incident from .git/dev-sentinel/last-incident.json,
    display the evidence, ask for explicit developer authorization, and on approval
    call the existing run_fixer() with the reconstructed EvidenceReport.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sentinel.agents.fixer import run_fixer


# ── install ───────────────────────────────────────────────────────────────────

def _cmd_install(target: Path, force: bool) -> int:
    """Install the Dev Sentinel pre-push hook into *target* repository."""
    git_dir = target / ".git"
    if not git_dir.is_dir():
        print(f"✗  {target} is not a Git repository (.git/ not found).")
        return 1

    hooks_dir = target / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_file = hooks_dir / "pre-push"

    # Check for an existing hook that is NOT ours.
    if hook_file.exists() and not force:
        content = hook_file.read_text(errors="replace")
        if "dev-sentinel-hook" not in content and "sentinel.git_hook" not in content:
            print(f"⚠   An existing pre-push hook was found at {hook_file}")
            print("    It does not appear to be a Dev Sentinel hook.")
            print("    Use --force to overwrite it, or add the following line manually:")
            print()
            print("        exec dev-sentinel-hook")
            print()
            return 1

    _write_hook(hook_file)
    print("✓  Dev Sentinel pre-push hook installed.")
    print(f"   Hook: {hook_file}")
    print()
    print("   To bypass for a single push: git push --no-verify")
    print("   To uninstall:                dev-sentinel uninstall")
    return 0


def _write_hook(hook_file: Path) -> None:
    """Write the executable pre-push hook script."""
    content = """\
#!/usr/bin/env sh
# Dev Sentinel pre-push hook
# Managed by: dev-sentinel install
# To uninstall: dev-sentinel uninstall
# To bypass:    git push --no-verify
exec dev-sentinel-hook
"""
    hook_file.write_text(content)
    hook_file.chmod(0o755)


# ── uninstall ─────────────────────────────────────────────────────────────────

def _cmd_uninstall(target: Path) -> int:
    """Remove the Sentinel hook from *target* repository."""
    hook_file = target / ".git" / "hooks" / "pre-push"

    if not hook_file.exists():
        print("   No pre-push hook found — nothing to remove.")
        return 0

    content = hook_file.read_text(errors="replace")
    if "dev-sentinel-hook" not in content and "sentinel.git_hook" not in content:
        print("⚠   The existing pre-push hook does not appear to be a Dev Sentinel hook.")
        print("    It has NOT been removed to preserve existing configuration.")
        return 1

    hook_file.unlink()
    print("✓  Dev Sentinel pre-push hook removed.")
    return 0


# ── run (interactive mode) ────────────────────────────────────────────────────

def _cmd_run() -> int:
    """Run the investigation pipeline against the current working-tree diff."""
    import subprocess as _sp
    diff_result = _sp.run(
        ["git", "diff"], capture_output=True, text=True
    )
    files_result = _sp.run(
        ["git", "diff", "--name-only"], capture_output=True, text=True
    )
    changed_files = [f for f in files_result.stdout.splitlines() if f.strip()]
    diff = diff_result.stdout

    print("🛡️  Dev Sentinel")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if not changed_files:
        print("\n   No local changes detected.")
        return 0

    from sentinel.orchestrator import orchestrate_investigation
    report = orchestrate_investigation(changed_files, diff)
    print(f"\n   Status: {report.status}")
    if report.fix_applied:
        print(f"   Fix:    {report.fix_applied}")
    return 0 if report.status == "clean" else 1


# ── fix ───────────────────────────────────────────────────────────────────────

def _git_toplevel() -> Path | None:
    """Return the repository root via git rev-parse --show-toplevel, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _git_head_sha(cwd: Path) -> str | None:
    """Return the current HEAD SHA, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
            cwd=str(cwd),
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _cmd_fix() -> int:
    """Load the persisted incident, show evidence, ask for authorization, call Fixer."""
    # A. Find the git root.
    git_root = _git_toplevel()
    if git_root is None:
        print("✗  Could not determine the Git repository root.")
        print("   Run `dev-sentinel fix` from inside a Git repository.")
        return 1

    # B. Load the incident file.
    incident_path = git_root / ".git" / "dev-sentinel" / "last-incident.json"
    if not incident_path.exists():
        print("   No pending confirmed regression found.")
        print(f"   ({incident_path} does not exist)")
        return 1

    try:
        incident = json.loads(incident_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"✗  Could not read incident file: {exc}")
        return 1

    # C. Validate the incident is still relevant (HEAD must match local_sha).
    current_head = _git_head_sha(git_root)
    saved_sha = incident.get("local_sha", "")
    if current_head != saved_sha:
        print("⚠  The saved incident belongs to a different HEAD.")
        print(f"   Saved SHA:   {saved_sha}")
        print(f"   Current HEAD: {current_head}")
        print()
        print("   The incident is stale. Push the current branch and let the")
        print("   hook re-run the investigation, or discard the incident manually:")
        print(f"   rm {incident_path}")
        return 1

    # D. Display the saved evidence.
    print()
    print("🛡️  Dev Sentinel — Confirmed Regression")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    changed_files = incident.get("changed_files") or []
    if changed_files:
        print("📍 Changed files")
        for f in changed_files:
            print(f"   {f}")
        print()

    changed_behaviour = incident.get("changed_behaviour", "")
    if changed_behaviour:
        print(f"🔄 Changed behaviour")
        print(f"   {changed_behaviour}")
        print()

    repro_data = incident.get("reproduction_result") or {}
    if repro_data:
        print("🧪 Evidence")
        print(f"   Tests passed: {repro_data.get('passed')}")
        print(f"   Exit code:    {repro_data.get('exit_code')}")
        stdout = (repro_data.get("stdout") or "").strip()
        if stdout:
            lines = stdout.splitlines()
            excerpt = lines[-10:] if len(lines) > 10 else lines
            for line in excerpt:
                print(f"   {line}")
        print()

    affected = incident.get("affected_components") or []
    if affected:
        print("Affected:")
        for comp in affected:
            print(f"   {comp}")
        print()

    suspected = incident.get("suspected_issue", "")
    if suspected:
        print("🔎 Suspected cause")
        for line in suspected.splitlines():
            print(f"   {line}")
        print()

    # E. Ask for explicit authorization.
    try:
        while True:
            answer = input("Apply this fix? [y/N] ").strip().lower()

            if answer == "y":
                break

            if answer in ("", "n"):
                print("   Fix cancelled.")
                return 0

            print("   Please answer y or n.")
    except (EOFError, KeyboardInterrupt):
        print()
        print("   Cancelled.")
        return 0

    # F. Reconstruct EvidenceReport and call the existing Fixer.
    from sentinel.models import EvidenceReport, TestResult

    repro_result: TestResult | None = None
    if repro_data:
        repro_result = TestResult(
            passed=bool(repro_data.get("passed")),
            exit_code=int(repro_data.get("exit_code") or 1),
            stdout=repro_data.get("stdout") or "",
            stderr=repro_data.get("stderr") or "",
        )

    evidence = EvidenceReport(
        changed_files=changed_files,
        changed_behaviour=changed_behaviour,
        affected_components=affected,
        suspected_issue=suspected,
        reproduction_result=repro_result,
        status="regression_confirmed",
    )

    # The Fixer uses Path.cwd() / failing_file.  Change to the repo root so
    # relative paths in changed_files resolve correctly.
    original_cwd = Path.cwd()
    os.chdir(git_root)
    try:
        fix_report = run_fixer(evidence)
    finally:
        os.chdir(original_cwd)

    # G. Display the FixReport.
    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"Fix status:       {fix_report.status}")

    if fix_report.files_modified:
        print(f"Files modified:   {', '.join(fix_report.files_modified)}")

    if fix_report.fix_applied:
        print(f"Fix applied:      {fix_report.fix_applied}")

    if fix_report.root_cause:
        print(f"Root cause:       {fix_report.root_cause}")

    if fix_report.focused_test_result:
        ft = fix_report.focused_test_result
        print(f"Focused tests:    {'passed' if ft.passed else 'FAILED'} (exit {ft.exit_code})")

    if fix_report.full_test_result:
        ft = fix_report.full_test_result
        print(f"Full suite:       {'passed' if ft.passed else 'FAILED'} (exit {ft.exit_code})")

    if fix_report.remaining_uncertainty:
        print(f"Uncertainty:      {fix_report.remaining_uncertainty}")

    print()

    if fix_report.status == "fixed":
        print("✓  Fix applied and verified.")
        print("   Review the changes, stage them, commit, and push again.")
    else:
        print("✗  Fix could not be fully applied.")
        print("   Manual review required.")

    print()
    return 0 if fix_report.status == "fixed" else 1


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dev-sentinel",
        description="Dev Sentinel — AI-assisted pre-push safety tool.",
    )
    sub = parser.add_subparsers(dest="command")

    # install
    p_install = sub.add_parser("install", help="Install the pre-push hook.")
    p_install.add_argument(
        "--target", type=Path, default=Path.cwd(),
        help="Target Git repository (default: current directory).",
    )
    p_install.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing non-Sentinel pre-push hook.",
    )

    # uninstall
    p_uninstall = sub.add_parser("uninstall", help="Remove the pre-push hook.")
    p_uninstall.add_argument(
        "--target", type=Path, default=Path.cwd(),
        help="Target Git repository (default: current directory).",
    )

    # run
    sub.add_parser("run", help="Run the pipeline against the current working-tree diff.")

    # fix
    sub.add_parser("fix", help="Review and authorize a fix for a confirmed regression.")

    args = parser.parse_args()

    if args.command == "install":
        sys.exit(_cmd_install(args.target.resolve(), args.force))
    elif args.command == "uninstall":
        sys.exit(_cmd_uninstall(args.target.resolve()))
    elif args.command == "run":
        sys.exit(_cmd_run())
    elif args.command == "fix":
        sys.exit(_cmd_fix())
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
