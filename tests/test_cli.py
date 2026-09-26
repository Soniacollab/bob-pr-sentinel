"""
tests/test_cli.py

Focused tests for the `dev-sentinel fix` subcommand in sentinel/cli.py.

Tests:
  - No incident file → Fixer not called, return 1
  - Stale HEAD (sha mismatch) → Fixer not called, return 1
  - Empty confirmation input → Fixer not called, return 0
  - 'n' confirmation → Fixer not called, return 0
  - 'y' confirmation → run_fixer called with reconstructed EvidenceReport
  - No git commit or push performed during fix
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel.cli import _cmd_fix, _git_head_sha, _git_toplevel


# ── Fixtures ──────────────────────────────────────────────────────────────────

SHA_A = "a" * 40
SHA_B = "b" * 40


def _make_incident(tmp_path: Path, local_sha: str = SHA_A) -> Path:
    """Write a minimal valid incident JSON and return its path."""
    sentinel_dir = tmp_path / ".git" / "dev-sentinel"
    sentinel_dir.mkdir(parents=True)
    incident = {
        "local_ref":           "refs/heads/main",
        "local_sha":           local_sha,
        "remote_ref":          "refs/remotes/origin/main",
        "remote_sha":          SHA_B,
        "changed_files":       ["app/preprocessing.py"],
        "changed_behaviour":   "None guard removed",
        "affected_components": ["app/api.py"],
        "suspected_issue":     "float(None) raises TypeError",
        "reproduction_result": {
            "passed":    False,
            "exit_code": 1,
            "stdout":    "FAILED test_none_score\nE TypeError",
            "stderr":    "",
        },
        "status": "regression_confirmed",
    }
    path = sentinel_dir / "last-incident.json"
    path.write_text(json.dumps(incident))
    return tmp_path


def _fix_report(status: str = "fixed") -> MagicMock:
    fr = MagicMock()
    fr.status = status
    fr.files_modified = ["app/preprocessing.py"]
    fr.fix_applied = "Restored guard"
    fr.root_cause = "Unguarded float()"
    fr.focused_test_result = MagicMock(passed=True, exit_code=0)
    fr.full_test_result = MagicMock(passed=True, exit_code=0)
    fr.remaining_uncertainty = ""
    return fr


# ── No incident ───────────────────────────────────────────────────────────────

class TestNoIncident:

    def test_no_incident_file_returns_1(self, tmp_path):
        with (
            patch("sentinel.cli._git_toplevel", return_value=tmp_path),
            patch("sentinel.cli.run_fixer") as mock_fixer,
        ):
            # No incident file created — tmp_path/.git/dev-sentinel/ doesn't exist
            result = _cmd_fix()
        assert result == 1
        mock_fixer.assert_not_called()

    def test_no_incident_prints_message(self, tmp_path, capsys):
        with patch("sentinel.cli._git_toplevel", return_value=tmp_path):
            _cmd_fix()
        out = capsys.readouterr().out
        assert "No pending" in out or "does not exist" in out

    def test_no_git_root_returns_1(self, capsys):
        with patch("sentinel.cli._git_toplevel", return_value=None):
            result = _cmd_fix()
        assert result == 1
        out = capsys.readouterr().out
        assert "repository" in out.lower() or "git" in out.lower()


# ── Stale HEAD ────────────────────────────────────────────────────────────────

class TestStaleHead:

    def test_stale_sha_returns_1(self, tmp_path):
        """When current HEAD differs from saved local_sha, Fixer must not run."""
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_B),  # different SHA
            patch("sentinel.cli.run_fixer") as mock_fixer,
        ):
            result = _cmd_fix()
        assert result == 1
        mock_fixer.assert_not_called()

    def test_stale_sha_prints_message(self, tmp_path, capsys):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_B),
        ):
            _cmd_fix()
        out = capsys.readouterr().out
        assert "different HEAD" in out or "stale" in out.lower()


# ── Confirmation prompt — negative answers ────────────────────────────────────

class TestConfirmationNegative:

    def _run_with_answer(self, tmp_path: Path, answer: str):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", return_value=answer),
            patch("sentinel.cli.run_fixer") as mock_fixer,
        ):
            result = _cmd_fix()
        return result, mock_fixer

    def test_empty_answer_does_not_call_fixer(self, tmp_path):
        result, mock_fixer = self._run_with_answer(tmp_path, "")
        mock_fixer.assert_not_called()

    def test_empty_answer_returns_0(self, tmp_path):
        result, _ = self._run_with_answer(tmp_path, "")
        assert result == 0

    def test_n_does_not_call_fixer(self, tmp_path):
        result, mock_fixer = self._run_with_answer(tmp_path, "n")
        mock_fixer.assert_not_called()

    def test_capital_n_does_not_call_fixer(self, tmp_path):
        result, mock_fixer = self._run_with_answer(tmp_path, "N")
        mock_fixer.assert_not_called()

    def test_garbage_input_then_n_does_not_call_fixer(self, tmp_path):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", side_effect=["maybe", "n"]),
            patch("sentinel.cli.run_fixer") as mock_fixer,
        ):
            result = _cmd_fix()

        assert result == 0
        mock_fixer.assert_not_called()

    def test_eof_does_not_call_fixer(self, tmp_path):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", side_effect=EOFError),
            patch("sentinel.cli.run_fixer") as mock_fixer,
        ):
            result = _cmd_fix()
        assert result == 0
        mock_fixer.assert_not_called()


# ── Confirmation prompt — 'y' authorizes the Fixer ───────────────────────────

class TestConfirmationYes:

    def _run_yes(self, tmp_path: Path, fix_status: str = "fixed"):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        mock_report = _fix_report(status=fix_status)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", return_value="y"),
            patch("sentinel.cli.run_fixer", return_value=mock_report) as mock_fixer,
            patch("os.chdir"),   # don't actually chdir in tests
        ):
            result = _cmd_fix()
        return result, mock_fixer, mock_report

    def test_y_calls_run_fixer(self, tmp_path):
        _, mock_fixer, _ = self._run_yes(tmp_path)
        mock_fixer.assert_called_once()

    def test_y_passes_evidence_report(self, tmp_path):
        from sentinel.models import EvidenceReport
        _, mock_fixer, _ = self._run_yes(tmp_path)
        evidence = mock_fixer.call_args[0][0]
        assert isinstance(evidence, EvidenceReport)
        assert evidence.status == "regression_confirmed"
        assert evidence.changed_files == ["app/preprocessing.py"]

    def test_y_evidence_preserves_reproduction_result(self, tmp_path):
        from sentinel.models import TestResult
        _, mock_fixer, _ = self._run_yes(tmp_path)
        evidence = mock_fixer.call_args[0][0]
        assert isinstance(evidence.reproduction_result, TestResult)
        assert evidence.reproduction_result.passed is False
        assert evidence.reproduction_result.exit_code == 1

    def test_y_fixed_returns_0(self, tmp_path):
        result, _, _ = self._run_yes(tmp_path, fix_status="fixed")
        assert result == 0

    def test_y_fix_failed_returns_1(self, tmp_path):
        result, _, _ = self._run_yes(tmp_path, fix_status="fix_failed")
        assert result == 1

    def test_capital_y_calls_fixer(self, tmp_path):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        mock_report = _fix_report()
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", return_value="Y"),
            patch("sentinel.cli.run_fixer", return_value=mock_report) as mock_fixer,
            patch("os.chdir"),
        ):
            _cmd_fix()
        mock_fixer.assert_called_once()


# ── No commit / push during fix ───────────────────────────────────────────────

class TestNoAutoCommitOrPush:

    def test_no_git_commit_called(self, tmp_path):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", return_value="y"),
            patch("sentinel.cli.run_fixer", return_value=_fix_report()),
            patch("os.chdir"),
            patch("subprocess.run") as mock_run,
        ):
            _cmd_fix()
        for c in mock_run.call_args_list:
            cmd = c[0][0] if c[0] else c[1].get("args", [])
            if isinstance(cmd, list):
                assert "commit" not in cmd, f"git commit must not be called: {cmd}"

    def test_no_git_push_called(self, tmp_path):
        git_root = _make_incident(tmp_path, local_sha=SHA_A)
        with (
            patch("sentinel.cli._git_toplevel", return_value=git_root),
            patch("sentinel.cli._git_head_sha", return_value=SHA_A),
            patch("builtins.input", return_value="y"),
            patch("sentinel.cli.run_fixer", return_value=_fix_report()),
            patch("os.chdir"),
            patch("subprocess.run") as mock_run,
        ):
            _cmd_fix()
        for c in mock_run.call_args_list:
            cmd = c[0][0] if c[0] else c[1].get("args", [])
            if isinstance(cmd, list):
                assert "push" not in cmd, f"git push must not be called: {cmd}"
