"""Repository-root guard.

Loaded by pytest at startup for any invocation rooted here, which is where a
naive `pytest` from the repository root would otherwise pull all four projects
into one process. The real check lives with the projects it protects.
"""

import sys
from pathlib import Path

_ASSESSMENT = Path(__file__).resolve().parent / "fde-assessment"
if _ASSESSMENT.is_dir():
    sys.path.insert(0, str(_ASSESSMENT))
    from _collection_guard import check
else:  # pragma: no cover - the assessment folder is always present
    check = None


def pytest_configure(config):
    if check is not None:
        check(config)
