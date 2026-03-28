"""
Root conftest.py — runs before any test collection.

1. Loads .env from the repo root (if present) so that passwords and endpoints
   stored in .env are available to all tests without requiring the developer to
   export them in every shell session.  Copy .env.example → .env and fill in
   your values.

2. Sets JAVA_HOME to the Homebrew OpenJDK 17 install on macOS when it isn't
   already set in the environment.  This allows `pytest` (and the .venv_test
   runner) to start a local PySpark session without requiring the developer to
   manually export JAVA_HOME in each shell.
"""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    # override=False means existing env vars (e.g. from the shell) take precedence
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed; env vars must be set in the shell


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: marks tests that require a live database and take >30s to complete",
    )

_CANDIDATE_JAVA_HOMES = [
    # Linux / Docker (Debian/Ubuntu, Alpine, RHEL)
    "/usr/lib/jvm/java-17-openjdk-amd64",
    "/usr/lib/jvm/java-17-openjdk-arm64",
    "/usr/lib/jvm/java-17-openjdk",
    "/usr/lib/jvm/java-21-openjdk-amd64",
    "/usr/lib/jvm/java-21-openjdk",
    # macOS Homebrew
    "/opt/homebrew/opt/openjdk@17",
    "/opt/homebrew/opt/openjdk@21",
    "/opt/homebrew/opt/openjdk@11",
    "/opt/homebrew/opt/openjdk",
]


def _find_java_home() -> str | None:
    for candidate in _CANDIDATE_JAVA_HOMES:
        if Path(candidate, "bin", "java").exists():
            return candidate
    return None


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """When STATSCHEMA_ASSERT_NO_SKIPS=1, turn any skip into a hard failure.

    Set automatically by all test-live-* Makefile targets.  Prevents silent
    passes when a database container or endpoint is not running.
    """
    if not os.environ.get("STATSCHEMA_ASSERT_NO_SKIPS"):
        return
    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    # Collect unique skip reasons from longrepr (filename, lineno, "Skipped: …")
    reasons = dict.fromkeys(
        r.longrepr[2] if isinstance(r.longrepr, tuple) else str(r.longrepr)
        for r in skipped
    )
    terminalreporter.write_sep(
        "=",
        f"ASSERT_NO_SKIPS: {len(skipped)} test(s) skipped — fix the infrastructure or unset STATSCHEMA_ASSERT_NO_SKIPS",
        red=True,
    )
    for reason in reasons:
        terminalreporter.write_line(f"  {reason}", red=True)
    sys.exit(1)


if not os.environ.get("JAVA_HOME"):
    found = _find_java_home()
    if found:
        os.environ["JAVA_HOME"] = found
        os.environ["PATH"] = f"{found}/bin:{os.environ.get('PATH', '')}"
