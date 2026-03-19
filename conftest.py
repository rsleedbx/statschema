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
from pathlib import Path

try:
    from dotenv import load_dotenv
    # override=False means existing env vars (e.g. from the shell) take precedence
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed; env vars must be set in the shell

_CANDIDATE_JAVA_HOMES = [
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


if not os.environ.get("JAVA_HOME"):
    found = _find_java_home()
    if found:
        os.environ["JAVA_HOME"] = found
        os.environ["PATH"] = f"{found}/bin:{os.environ.get('PATH', '')}"
