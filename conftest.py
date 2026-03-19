"""
Root conftest.py — runs before any test collection.

Sets JAVA_HOME to the Homebrew OpenJDK 17 install on macOS when it isn't
already set in the environment.  This allows `pytest` (and the .venv_test
runner) to start a local PySpark session without requiring the developer to
manually export JAVA_HOME in each shell.
"""
import os
from pathlib import Path

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
