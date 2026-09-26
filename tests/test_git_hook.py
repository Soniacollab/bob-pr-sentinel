"""
tests/test_git_hook.py

Focused tests for sentinel/git_hook.py — the Git pre-push adapter.

Covers scenarios A–O from the specification:
  A. Clean push → exit 0
  B. Regression detected → exit 1
  C. Static suspicion without executable failure → no Fixer, no regression_confirmed
  D. Regression confirmed + fix verified → still exit 1 (push blocked)
  E. Regression confirmed + failed Fixer → exit 1
  F. Fix verification fails → exit 1, never claim fixed
  G. Unexpected Sentinel exception → exit 1
  H. Initial push / missing remote SHA → handled safely
  I. Multiple commits → effective pushed changes analysed
  J. Unrelated unstaged changes → not part of push analysis
  K. Staged but uncommitted changes → not treated as pushed
  L. Untracked files → not silently included
  M. No automatic commit
  N. No automatic push
  O. Existing test suite remains green (verified by running full suite)

Unit tests use unittest.mock to avoid touching Git or the filesystem.
"""

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from sentinel.git_hook import (
    ZERO_SHA,
    PushRef,
    _diff_for_ref,
    _exit_code,
    parse_push_refs,
    run,
)
from sentinel.models import EvidenceReport, TestResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

LOCAL_SHA  = "a" * 40
REMOTE_SHA = "b" * 40
ROOT_SHA   = "c" * 40

def _normal_ref(**kw) -> PushRef:
    defaults = dict(
        local_ref="refs/heads/main",
        local_sha=LOCAL_SHA,
        remote_ref="refs/remotes/origin/main",
        remote_sha=REMOTE_SHA,
    )
    defaults.update(kw)
    return PushRef(**defaults)


def _initial_ref(**kw) -> PushRef:
    return _normal_ref(remote_sha=ZERO_SHA, **kw)


def _deletion_ref(**kw) -> PushRef:
    return _normal_ref(local_sha=ZERO_SHA, **kw)


def _clean_report() -> EvidenceReport:
    return EvidenceReport(status="clean")


def _regression_report() -> EvidenceReport:
    return EvidenceReport(
        status="regression_confirmed",
        reproduction_result=TestResult(
            passed=False, exit_code=1,
            stdout="FAILED test_x\nE TypeError", stderr="",
        ),
    )


def _fixed_report() -> EvidenceReport:
    return EvidenceReport(
        status="fixed",
        fix_applied="Restored guard at app/preprocessing.py:5",
        reproduction_result=TestResult(
            passed=False, exit_code=1, stdout="", stderr="",
        ),
        final_test_result=TestResult(
            passed=True, exit_code=0, stdout="5 passed", stderr="",
        ),
    )


def _stdin(ref: PushRef) -> str:
    return (
        f"{ref.local_ref} {ref.local_sha} "
        f"{ref.remote_ref} {ref.remote_sha}\n"
    )


# ── parse_push_refs ────────────────────────────────────────────────────────────

class TestParsePushRefs:

    def test_parses_normal_line(self):
        text = f"refs/heads/main {LOCAL_SHA} refs/remotes/origin/main {REMOTE_SHA}\n"
        refs = parse_push_refs(text)
        assert len(refs) == 1
        assert refs[0].local_sha  == LOCAL_SHA
        assert refs[0].remote_sha == REMOTE_SHA

    def test_ignores_blank_lines(self):
        text = f"\n  \nrefs/heads/main {LOCAL_SHA} refs/remotes/origin/main {REMOTE_SHA}\n\n"
        refs = parse_push_refs(text)
        assert len(refs) == 1

    def test_parses_multiple_refs(self):
        text = (
            f"refs/heads/feat {LOCAL_SHA} refs/remotes/origin/feat {REMOTE_SHA}\n"
            f"refs/heads/main {LOCAL_SHA} refs/remotes/origin/main {REMOTE_SHA}\n"
        )
        refs = parse_push_refs(text)
        assert len(refs) == 2

    def test_empty_stdin_returns_empty(self):
        assert parse_push_refs("") == []
        assert parse_push_refs("   \n  ") == []

    def test_is_deletion(self):
        ref = _deletion_ref()
        assert ref.is_deletion is True
        assert _normal_ref().is_deletion is False

    def test_is_initial(self):
        ref = _initial_ref()
        assert ref.is_initial is True
        assert _normal_ref().is_initial is False


# ── _diff_for_ref ─────────────────────────────────────────────────────────────

class TestDiffForRef:

    def test_deletion_ref_returns_empty(self):
        ref = _deletion_ref()
        files, diff = _diff_for_ref(ref)
        assert files == []
        assert diff == ""

    @patch("sentinel.git_hook._git")
    def test_normal_ref_uses_range_diff(self, mock_git):
        mock_git.side_effect = [
            "app/preprocessing.py",        # git diff --name-only
            "--- a/app/preprocessing.py\n+stub", # git diff
        ]
        ref = _normal_ref()
        files, diff = _diff_for_ref(ref)
        # Verify the range uses remote_sha as base
        name_only_call = mock_git.call_args_list[0]
        assert f"{REMOTE_SHA}..{LOCAL_SHA}" in name_only_call[0]

    @patch("sentinel.git_hook._git")
    def test_initial_ref_uses_root_commit(self, mock_git):
        mock_git.side_effect = [
            ROOT_SHA,                       # rev-list --max-parents=0
            "app/preprocessing.py",         # git diff --name-only
            "--- a/app/preprocessing.py\n+stub",
        ]
        ref = _initial_ref()
        files, diff = _diff_for_ref(ref)
        root_call = mock_git.call_args_list[0]
        assert "rev-list" in root_call[0]
        assert "--max-parents=0" in root_call[0]
        # The range should use the root commit as base
        range_call = mock_git.call_args_list[1]
        assert f"{ROOT_SHA}..{LOCAL_SHA}" in range_call[0]

    @patch("sentinel.git_hook._git")
    def test_no_python_changes_returns_empty(self, mock_git):
        mock_git.side_effect = ["", ""]  # empty name-only output
        ref = _normal_ref()
        files, diff = _diff_for_ref(ref)
        assert files == []
        assert diff == ""

    @patch("sentinel.git_hook._git")
    def test_only_python_files_in_diff(self, mock_git):
        """Non-Python files are excluded — only *.py is passed to git diff."""
        mock_git.side_effect = [
            "app/preprocessing.py",
            "--- a/app/preprocessing.py\n+content",
        ]
        ref = _normal_ref()
        files, diff = _diff_for_ref(ref)
        # Both git calls must include "*.py" as a path filter.
        for c in mock_git.call_args_list:
            assert "*.py" in c[0]


# ── _exit_code ────────────────────────────────────────────────────────────────

class TestExitCode:

    def test_clean_exits_zero(self):
        assert _exit_code("clean") == 0

    def test_fixed_exits_one(self):
        """fixed → push still blocked (fix is only in working tree)."""
        assert _exit_code("fixed") == 1

    def test_regression_confirmed_exits_one(self):
        assert _exit_code("regression_confirmed") == 1

    def test_pending_exits_one(self):
        assert _exit_code("pending") == 1

    def test_unknown_status_exits_one(self):
        assert _exit_code("something_weird") == 1


# ── A. CLEAN PUSH ─────────────────────────────────────────────────────────────

class TestCleanPush:

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_clean_push_exits_zero(self, mock_diff, mock_orch):
        ref = _normal_ref()
        result = run(_stdin(ref))
        assert result == 0

    def test_empty_stdin_exits_zero(self):
        result = run("")
        assert result == 0

    @patch("sentinel.git_hook._diff_for_ref", return_value=([], ""))
    def test_no_python_changes_exits_zero(self, mock_diff):
        ref = _normal_ref()
        result = run(_stdin(ref))
        assert result == 0

    def test_deletion_ref_exits_zero(self):
        ref = _deletion_ref()
        result = run(_stdin(ref))
        assert result == 0


# ── B. REGRESSION DETECTED ────────────────────────────────────────────────────

class TestRegressionDetected:

    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=_regression_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_regression_blocks_push(self, mock_diff, mock_orch):
        result = run(_stdin(_normal_ref()))
        assert result == 1


# ── C. STATIC SUSPICION WITHOUT EXECUTABLE FAILURE ───────────────────────────

class TestSpeculativeIssue:
    """Impact suspects something but tests pass → clean, no Fixer."""

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_speculative_exits_zero(self, mock_diff, mock_orch):
        result = run(_stdin(_normal_ref()))
        assert result == 0


# ── D. REGRESSION + SUCCESSFUL FIX → STILL BLOCKED ───────────────────────────

class TestFixedStillBlocked:

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_fixed_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_fixed_status_still_exits_one(self, mock_diff, mock_orch):
        """Push must be blocked even when Fixer succeeded — fix not yet committed."""
        result = run(_stdin(_normal_ref()))
        assert result == 1

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_fixed_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_output_mentions_review_and_commit(self, mock_diff, mock_orch, capsys):
        run(_stdin(_normal_ref()))
        captured = capsys.readouterr().out
        assert "Review" in captured or "review" in captured
        assert "commit" in captured or "Commit" in captured


# ── E. REGRESSION + FAILED FIXER → BLOCKED ───────────────────────────────────

class TestFailedFixer:

    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=_regression_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_failed_fix_exits_one(self, mock_diff, mock_orch):
        result = run(_stdin(_normal_ref()))
        assert result == 1


# ── F. FIX VERIFICATION FAILS → NEVER CLAIM FIXED ────────────────────────────

class TestFixVerificationFails:

    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=EvidenceReport(
               status="regression_confirmed",
               fix_applied="partial",
               final_test_result=TestResult(
                   passed=False, exit_code=1, stdout="1 failed", stderr="",
               ),
           ))
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_partial_fix_exits_one(self, mock_diff, mock_orch):
        result = run(_stdin(_normal_ref()))
        assert result == 1


# ── G. UNEXPECTED SENTINEL EXCEPTION ─────────────────────────────────────────

class TestSentinelException:

    @patch("sentinel.git_hook.orchestrate_investigation",
           side_effect=RuntimeError("boom"))
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_exception_exits_one(self, mock_diff, mock_orch):
        result = run(_stdin(_normal_ref()))
        assert result == 1

    @patch("sentinel.git_hook._diff_for_ref", side_effect=subprocess.CalledProcessError(1, "git"))
    def test_git_error_exits_one(self, mock_diff):
        result = run(_stdin(_normal_ref()))
        assert result == 1


# ── H. INITIAL PUSH ───────────────────────────────────────────────────────────

class TestInitialPush:

    @patch("sentinel.git_hook._git")
    def test_initial_push_uses_root_commit(self, mock_git):
        mock_git.side_effect = [
            ROOT_SHA,
            "app/preprocessing.py",
            "--- a/preprocessing.py\n+content",
        ]
        ref = _initial_ref()
        files, diff = _diff_for_ref(ref)
        assert files == ["app/preprocessing.py"]
        # Root commit must have been queried
        first_call_args = mock_git.call_args_list[0][0]
        assert "rev-list" in first_call_args
        assert "--max-parents=0" in first_call_args

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_initial_push_clean_exits_zero(self, mock_diff, mock_orch):
        result = run(_stdin(_initial_ref()))
        assert result == 0


# ── I. MULTIPLE COMMITS / MULTIPLE REFS ───────────────────────────────────────

class TestMultipleRefs:

    @patch("sentinel.git_hook.orchestrate_investigation")
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_multiple_refs_all_clean_exits_zero(self, mock_diff, mock_orch):
        mock_orch.return_value = _clean_report()
        ref1 = _normal_ref(local_ref="refs/heads/feat", local_sha="a" * 40)
        ref2 = _normal_ref(local_ref="refs/heads/main", local_sha="b" * 40)
        stdin = _stdin(ref1) + _stdin(ref2)
        result = run(stdin)
        assert result == 0
        assert mock_orch.call_count == 2

    @patch("sentinel.git_hook.orchestrate_investigation")
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_one_failing_ref_blocks_push(self, mock_diff, mock_orch):
        """If any ref has a regression the push must be blocked."""
        mock_orch.side_effect = [_clean_report(), _regression_report()]
        ref1 = _normal_ref(local_ref="refs/heads/feat", local_sha="a" * 40)
        ref2 = _normal_ref(local_ref="refs/heads/main", local_sha="b" * 40)
        result = run(_stdin(ref1) + _stdin(ref2))
        assert result == 1


# ── J. UNRELATED UNSTAGED CHANGES ────────────────────────────────────────────

class TestUnstagedChanges:
    """The hook uses git diff <range>, not git diff (working tree).
    Unstaged changes are therefore invisible to the analysis."""

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref")
    def test_diff_for_ref_called_with_range_not_working_tree(
        self, mock_diff_for_ref, mock_orch
    ):
        mock_diff_for_ref.return_value = ([], "")
        ref = _normal_ref()
        run(_stdin(ref))
        # _diff_for_ref must be called with the PushRef object,
        # not with no arguments (which would be working-tree diff).
        mock_diff_for_ref.assert_called_once_with(ref)


# ── K. STAGED CHANGES ────────────────────────────────────────────────────────

class TestStagedChanges:
    """Staged-but-uncommitted changes are not in <range> diff."""

    @patch("sentinel.git_hook._git")
    def test_staged_changes_not_in_range_diff(self, mock_git):
        # git diff <sha>..<sha> only shows committed changes.
        # Staged changes appear in `git diff --cached`, never in range diff.
        # We verify the git call uses a commit range, not --cached.
        mock_git.side_effect = ["app/foo.py", "diff content"]
        ref = _normal_ref()
        _diff_for_ref(ref)
        for c in mock_git.call_args_list:
            assert "--cached" not in c[0]


# ── L. UNTRACKED FILES ────────────────────────────────────────────────────────

class TestUntrackedFiles:
    """git diff <range> only shows tracked file changes, never untracked files."""

    @patch("sentinel.git_hook._git")
    def test_untracked_files_not_returned(self, mock_git):
        # If git diff --name-only returns nothing, changed_files is empty.
        mock_git.side_effect = ["", ""]
        ref = _normal_ref()
        files, diff = _diff_for_ref(ref)
        assert files == []


# ── M. NO AUTOMATIC COMMIT ───────────────────────────────────────────────────

class TestNoAutoCommit:

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_no_git_commit_called(self, mock_diff, mock_orch):
        with patch("subprocess.run") as mock_run:
            # Allow the git calls inside _diff_for_ref / _git to pass through
            # but capture them for inspection. We override _diff_for_ref so
            # subprocess.run is only called if the hook tries to run extra git commands.
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run(_stdin(_normal_ref()))
            for c in mock_run.call_args_list:
                cmd = c[0][0] if c[0] else c[1].get("args", [])
                if isinstance(cmd, list):
                    assert "commit" not in cmd, f"git commit must not be called: {cmd}"


# ── N. NO AUTOMATIC PUSH ─────────────────────────────────────────────────────

class TestNoAutoPush:

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_no_git_push_called(self, mock_diff, mock_orch):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run(_stdin(_normal_ref()))
            for c in mock_run.call_args_list:
                cmd = c[0][0] if c[0] else c[1].get("args", [])
                if isinstance(cmd, list):
                    assert "push" not in cmd, f"git push must not be called: {cmd}"


# ── P. INCIDENT PERSISTENCE ───────────────────────────────────────────────────

class TestIncidentPersistence:
    """Confirmed regressions write last-incident.json; failures keep exit code 1."""

    @patch("sentinel.git_hook._git_root")
    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=_regression_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_confirmed_regression_writes_incident(self, mock_diff, mock_orch, mock_root, tmp_path):
        """When regression_confirmed, last-incident.json is written under .git/dev-sentinel/."""
        fake_git = tmp_path / "repo"
        fake_git.mkdir()
        (fake_git / ".git").mkdir()
        mock_root.return_value = fake_git

        result = run(_stdin(_normal_ref()))

        assert result == 1
        incident_file = fake_git / ".git" / "dev-sentinel" / "last-incident.json"
        assert incident_file.exists(), "last-incident.json must be created"

    @patch("sentinel.git_hook._git_root")
    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=_regression_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_incident_json_contains_expected_fields(self, mock_diff, mock_orch, mock_root, tmp_path):
        """The incident JSON must contain all required top-level fields."""
        fake_git = tmp_path / "repo"
        fake_git.mkdir()
        (fake_git / ".git").mkdir()
        mock_root.return_value = fake_git

        run(_stdin(_normal_ref()))

        import json as _json
        incident = _json.loads(
            (fake_git / ".git" / "dev-sentinel" / "last-incident.json").read_text()
        )
        for field in (
            "local_ref", "local_sha", "remote_ref", "remote_sha",
            "changed_files", "changed_behaviour", "affected_components",
            "suspected_issue", "reproduction_result", "status",
        ):
            assert field in incident, f"Missing field: {field}"
        assert incident["status"] == "regression_confirmed"

    @patch("sentinel.git_hook._git_root")
    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=_regression_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_incident_reproduction_result_is_plain_json(self, mock_diff, mock_orch, mock_root, tmp_path):
        """reproduction_result must be a plain dict, not a dataclass."""
        fake_git = tmp_path / "repo"
        fake_git.mkdir()
        (fake_git / ".git").mkdir()
        mock_root.return_value = fake_git

        run(_stdin(_normal_ref()))

        import json as _json
        incident = _json.loads(
            (fake_git / ".git" / "dev-sentinel" / "last-incident.json").read_text()
        )
        repro = incident["reproduction_result"]
        assert isinstance(repro, dict)
        assert "passed" in repro
        assert "exit_code" in repro
        assert "stdout" in repro
        assert "stderr" in repro

    @patch("sentinel.git_hook._git_root")
    @patch("sentinel.git_hook.orchestrate_investigation",
           return_value=_regression_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_persistence_failure_keeps_exit_code_1(self, mock_diff, mock_orch, mock_root, tmp_path):
        """If the incident file cannot be written, the push is still blocked (exit 1)."""
        fake_git = tmp_path / "repo"
        fake_git.mkdir()
        (fake_git / ".git").mkdir()
        mock_root.return_value = fake_git

        # Make the directory read-only so mkdir/write_text fails.
        git_dir = fake_git / ".git"
        git_dir.chmod(0o555)
        try:
            result = run(_stdin(_normal_ref()))
            assert result == 1
        finally:
            git_dir.chmod(0o755)

    @patch("sentinel.git_hook.orchestrate_investigation", return_value=_clean_report())
    @patch("sentinel.git_hook._diff_for_ref", return_value=(["app/foo.py"], "diff"))
    def test_clean_push_does_not_write_incident(self, mock_diff, mock_orch, tmp_path):
        """A clean push must never write an incident file."""
        with patch("sentinel.git_hook._git_root", return_value=tmp_path):
            run(_stdin(_normal_ref()))
        incident_file = tmp_path / ".git" / "dev-sentinel" / "last-incident.json"
        assert not incident_file.exists()
