"""
statschema service layer.

All external surfaces (CLI, future REST API, future SDK) call these modules
rather than reaching into implementation modules directly.  The service
modules are thin orchestration wrappers — they add logging, validation, and
error normalisation on top of the underlying implementation functions.
"""

from .collect import collect, collect_queries
from .describe import describe
from .generate import generate
from .inject import inject
from .transpile import parse, emit, transpile
from .benchmark import run_identity_test

__all__ = [
    "collect",
    "collect_queries",
    "describe",
    "generate",
    "inject",
    "parse",
    "emit",
    "transpile",
    "run_identity_test",
]
