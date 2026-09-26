"""
tests/test_install.py

Installation and cross-repository tests for Dev Sentinel.

Tests use real temporary Git repositories created with pytest's `tmp_path`
fixture to verify the actual install/uninstall/hook-invocation flow.

Scenarios A–J from the specification:
  A. Existing test suite still passes (covered by running the suite itself)
  B. Install into an external repository works
  C. Generated hook locates sentinel without relying on CWD being bob-pr-sentinel
  D. No hardcoded absolute path exists in any hook or CLI output
  E. Existing core.hooksPath is handled safely (not silently overwritten)
  F. Re-running installation is idempotent
  G. Uninstall does not remove unrelated configuration
  H. A target repository with normal Python tests can invoke real pytest
  I. A target repository without tests/ does not crash
  J. Existing pre-push behavior in bob-pr-sentinel still works

Integration scenarios (I1-I4) use a real temporary Git repo and a real
subprocess `git push` through a local bare repository.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Resolve the dev-sentinel CLI so tests can call it regardless of CWD.
_SENTINEL_BIN = Path(sys.executable).parent / "dev-sentinel"
_HOOK_BIN     = Path(sys.executable).parent / "dev-sentinel-hook"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _git(*args, cwd, check=True, **kw):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check, **kw
    )


def _make_git_repo(path: Path) -> Path:
    """Initialise a bare-minimum Git repository at *path*."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name",  "Test", cwd=path)
    # Initial commit so HEAD exists
    (path / "README.md").write_text("test repo\n")
    _git("add", ".", cwd=path)
    _git("commit", "-m", "initial", cwd=path)
    return path


def _make_bare_repo(path: Path) -> Path:
    """Create a bare Git repository to act as a remote."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", cwd=path)
    return path


def _install_sentinel(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_SENTINEL_BIN), "install", "--target", str(target)],
        capture_output=True, text=True,
    )


def _uninstall_sentinel(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_SENTINEL_BIN), "uninstall", "--target", str(target)],
        capture_output=True, text=True,
    )


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-push"


# ── B. Install into external repository ──────────────────────────────────────

class TestInstall:

    def test_install_creates_hook_file(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        result = _install_sentinel(repo)
        assert result.returncode == 0, result.stderr
        assert _hook_path(repo).exists()

    def test_hook_file_is_executable(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        hook = _hook_path(repo)
        assert os.access(hook, os.X_OK)

    def test_hook_invokes_dev_sentinel_hook(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        content = _hook_path(repo).read_text()
        assert "dev-sentinel-hook" in content

    def test_install_fails_outside_git_repo(self, tmp_path):
        non_repo = tmp_path / "notarepo"
        non_repo.mkdir()
        result = _install_sentinel(non_repo)
        assert result.returncode != 0
        assert "not a Git repository" in result.stdout or "not a Git" in result.stderr


# ── C. Hook locates sentinel without CWD being bob-pr-sentinel ───────────────

class TestHookLocation:

    def test_hook_uses_installed_entry_point_not_relative_path(self, tmp_path):
        """The hook must use 'dev-sentinel-hook' (on PATH), not a relative path."""
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        content = _hook_path(repo).read_text()
        # Must not reference any absolute path
        assert "/Users/" not in content
        assert "/home/"  not in content
        # Must not reference .venv or a relative sentinel/ path
        assert ".venv"         not in content
        assert "sentinel.git_hook" not in content or "dev-sentinel-hook" in content


# ── D. No hardcoded absolute path ────────────────────────────────────────────

class TestNoHardcodedPaths:

    def test_hook_file_has_no_absolute_path(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        content = _hook_path(repo).read_text()
        assert "/Users/sonita" not in content
        assert "/home/"        not in content

    def test_githooks_pre_push_has_no_absolute_path(self):
        hook = Path(__file__).parent.parent / ".githooks" / "pre-push"
        content = hook.read_text()
        assert "/Users/sonita" not in content
        assert "/home/"        not in content

    def test_sentinel_cli_help_has_no_absolute_path(self):
        result = subprocess.run(
            [str(_SENTINEL_BIN), "--help"], capture_output=True, text=True
        )
        assert "/Users/sonita" not in result.stdout


# ── E. Existing core.hooksPath / pre-push hook handled safely ────────────────

class TestExistingHook:

    def test_existing_sentinel_hook_overwritten_silently(self, tmp_path):
        """Re-installing over an existing Sentinel hook is idempotent."""
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        result = _install_sentinel(repo)
        assert result.returncode == 0

    def test_existing_non_sentinel_hook_not_overwritten(self, tmp_path):
        """An existing non-Sentinel hook must not be silently overwritten."""
        repo = _make_git_repo(tmp_path / "myrepo")
        hook = _hook_path(repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'custom hook'\n")
        hook.chmod(0o755)

        result = _install_sentinel(repo)
        assert result.returncode != 0
        assert "existing" in result.stdout.lower() or "existing" in result.stderr.lower()
        # Original hook must be untouched
        assert "custom hook" in hook.read_text()

    def test_force_flag_overwrites_non_sentinel_hook(self, tmp_path):
        """--force allows overwriting a non-Sentinel hook."""
        repo = _make_git_repo(tmp_path / "myrepo")
        hook = _hook_path(repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'custom hook'\n")
        hook.chmod(0o755)

        result = subprocess.run(
            [str(_SENTINEL_BIN), "install", "--target", str(repo), "--force"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "dev-sentinel-hook" in hook.read_text()


# ── F. Idempotent installation ────────────────────────────────────────────────

class TestIdempotent:

    def test_second_install_succeeds(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        r1 = _install_sentinel(repo)
        r2 = _install_sentinel(repo)
        assert r1.returncode == 0
        assert r2.returncode == 0

    def test_second_install_does_not_duplicate_content(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        _install_sentinel(repo)
        content = _hook_path(repo).read_text()
        assert content.count("dev-sentinel-hook") == 1


# ── G. Uninstall ──────────────────────────────────────────────────────────────

class TestUninstall:

    def test_uninstall_removes_hook(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        _install_sentinel(repo)
        result = _uninstall_sentinel(repo)
        assert result.returncode == 0
        assert not _hook_path(repo).exists()

    def test_uninstall_no_hook_is_safe(self, tmp_path):
        """Uninstalling when no hook exists should not error."""
        repo = _make_git_repo(tmp_path / "myrepo")
        result = _uninstall_sentinel(repo)
        assert result.returncode == 0

    def test_uninstall_does_not_touch_non_sentinel_hook(self, tmp_path):
        """Uninstall must not remove a non-Sentinel hook."""
        repo = _make_git_repo(tmp_path / "myrepo")
        hook = _hook_path(repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'custom hook'\n")
        hook.chmod(0o755)

        result = _uninstall_sentinel(repo)
        assert result.returncode != 0  # signals it was not a Sentinel hook
        assert hook.exists()           # untouched


# ── H. Target repo with tests — real pytest invocation ────────────────────────

class TestWithPythonTests:
    """Verify that Sentinel can invoke real pytest inside a target repo."""

    def test_sentinel_run_in_repo_with_passing_tests(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        # Add a simple passing test
        test_file = repo / "tests" / "test_sample.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_pass(): assert True\n")

        # Run the runner directly — not through the hook — to avoid needing a real remote
        from sentinel.runner import run_tests
        # We must change CWD because runner.py uses sys.executable and cwd-relative paths
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(repo)
            result = run_tests("tests/")
        finally:
            os.chdir(old_cwd)

        # pytest may not be importable from the target env — but our venv has it
        # so this should pass
        assert result.passed

    def test_sentinel_run_in_repo_with_failing_tests(self, tmp_path):
        repo = _make_git_repo(tmp_path / "myrepo")
        test_file = repo / "tests" / "test_sample.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_fail(): assert False\n")

        from sentinel.runner import run_tests
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(repo)
            result = run_tests("tests/")
        finally:
            os.chdir(old_cwd)

        assert not result.passed
        assert result.exit_code != 0


# ── I. Target repo without tests/ ─────────────────────────────────────────────

class TestNoTestsDirectory:

    def test_orchestrator_does_not_crash_without_tests_dir(self, tmp_path):
        """The pipeline must not crash when there is no tests/ directory."""
        repo = _make_git_repo(tmp_path / "myrepo")
        # No tests/ directory created

        from sentinel.orchestrator import orchestrate_investigation
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(repo)
            # With a clean diff there is nothing to investigate
            result = orchestrate_investigation([], "")
        finally:
            os.chdir(old_cwd)

        assert result.status == "clean"


# ── J. Existing pre-push behavior in bob-pr-sentinel still works ──────────────

class TestBackwardCompatibility:

    def test_githooks_pre_push_still_exists_and_is_executable(self):
        hook = Path(__file__).parent.parent / ".githooks" / "pre-push"
        assert hook.exists()
        assert os.access(hook, os.X_OK)

    def test_githooks_pre_push_invokes_dev_sentinel_hook(self):
        hook = Path(__file__).parent.parent / ".githooks" / "pre-push"
        content = hook.read_text()
        assert "dev-sentinel-hook" in content

    def test_existing_tests_still_pass(self):
        """Run the existing test suite as a final smoke test."""
        result = subprocess.run(
            [str(Path(sys.executable)), "-m", "pytest",
             "tests/test_api.py",
             "tests/test_preprocessing.py",
             "tests/test_git_hook.py",
             "-q", "--tb=short"],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Existing test suite failed:\n{result.stdout}\n{result.stderr}"
        )


# ── Integration: real git push through local bare remote ──────────────────────

class TestRealPushIntegration:
    """
    End-to-end: create a real Git repository with a Sentinel hook,
    add a remote (local bare repo), and verify that `git push` is
    intercepted by the hook.

    These tests rely on `dev-sentinel-hook` being on PATH (i.e., the
    package is installed). They are skipped if the entry point is absent.
    """

    @pytest.fixture(autouse=True)
    def require_hook_bin(self):
        if not _HOOK_BIN.exists():
            pytest.skip("dev-sentinel-hook not on PATH — package not installed")

    def _setup_push_env(self, tmp_path):
        """Create a source repo + bare remote + install sentinel hook."""
        bare = _make_bare_repo(tmp_path / "remote.git")
        src  = _make_git_repo(tmp_path / "src")
        _git("remote", "add", "origin", str(bare), cwd=src)
        _git("push", "-u", "origin", "main", cwd=src)
        _install_sentinel(src)
        return src, bare

    def _push(self, repo: Path) -> subprocess.CompletedProcess:
        """Run git push in *repo* with the hook active, using the venv PATH."""
        env = os.environ.copy()
        venv_bin = str(Path(sys.executable).parent)
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            ["git", "push"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_clean_push_is_allowed(self, tmp_path):
        src, _ = self._setup_push_env(tmp_path)
        # Add a harmless change and commit it
        (src / "hello.py").write_text("x = 1\n")
        _git("add", ".", cwd=src)
        _git("commit", "-m", "clean change", cwd=src)

        result = self._push(src)
        # Hook should have run; clean path exits 0 → git push succeeds
        assert result.returncode == 0, (
            f"Push should have been allowed.\nstdout:{result.stdout}\nstderr:{result.stderr}"
        )

    def test_hook_is_executed_on_push(self, tmp_path):
        src, _ = self._setup_push_env(tmp_path)
        (src / "hello.py").write_text("x = 1\n")
        _git("add", ".", cwd=src)
        _git("commit", "-m", "clean change", cwd=src)

        result = self._push(src)
        # Dev Sentinel header must appear in stderr (hooks write to stderr)
        combined = result.stdout + result.stderr
        assert "Dev Sentinel" in combined, (
            f"Hook output not found.\nstdout:{result.stdout}\nstderr:{result.stderr}"
        )

    def test_no_auto_commit_on_push(self, tmp_path):
        src, _ = self._setup_push_env(tmp_path)
        (src / "hello.py").write_text("x = 1\n")
        _git("add", ".", cwd=src)
        _git("commit", "-m", "clean change", cwd=src)

        log_before = _git("log", "--oneline", cwd=src).stdout
        self._push(src)
        log_after  = _git("log", "--oneline", cwd=src).stdout
        assert log_before == log_after, "Hook must not create commits automatically."
