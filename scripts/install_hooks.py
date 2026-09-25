"""
scripts/install_hooks.py

Install the Dev Sentinel pre-push hook for this repository.

Usage:
    python scripts/install_hooks.py

What it does:
    Runs: git config core.hooksPath .githooks

This is repository-local — it does NOT modify global Git configuration.
The setting is stored in .git/config, which is not committed.

To uninstall:
    git config --unset core.hooksPath

To bypass for a single push:
    git push --no-verify
"""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).parent.parent
    hooks_path = repo_root / ".githooks"

    if not hooks_path.exists():
        print(f"✗  .githooks/ directory not found at {hooks_path}")
        sys.exit(1)

    hook_file = hooks_path / "pre-push"
    if not hook_file.exists():
        print(f"✗  pre-push hook not found at {hook_file}")
        sys.exit(1)

    result = subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"✗  git config failed: {result.stderr.strip()}")
        sys.exit(1)

    print("✓  Dev Sentinel pre-push hook installed.")
    print()
    print("   Git will now run .githooks/pre-push before every push.")
    print("   To bypass for a single push: git push --no-verify")
    print("   To uninstall:                git config --unset core.hooksPath")


if __name__ == "__main__":
    main()
