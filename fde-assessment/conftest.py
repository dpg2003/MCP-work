"""Loaded when pytest is invoked at or below the assessment root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _collection_guard import check  # noqa: E402


def pytest_configure(config):
    check(config)
