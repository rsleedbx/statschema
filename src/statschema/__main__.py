"""Enable `python -m statschema` as the CLI entry point."""
import sys
from pathlib import Path

# When run as `python -m statschema` from inside a repo that has not been
# installed via pip, ensure the src/ layout is on sys.path.
_src = Path(__file__).parent.parent  # .../src/
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from .cli import main  # noqa: E402 (import after sys.path mutation is intentional)

main()
