import subprocess
import sys

from sentinel.models import TestResult


def run_tests(test_target: str | None = None) -> TestResult:
    """Run pytest and return a TestResult.

    Args:
        test_target: A specific test file, directory, or node id
            (e.g. "tests/test_preprocessing.py::test_clean_user_input_none_score").
            When None the complete test suite is run.

    Returns:
        TestResult with the exit code, stdout, and stderr of the pytest process.
    """
    command = [sys.executable, "-m", "pytest", "-v"]

    if test_target is not None:
        command.append(test_target)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    return TestResult(
        passed=result.returncode == 0,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
