"""Test bootstrap.

``tests/support`` holds shared test doubles (the scripted model, snapshot
helper). Putting ``tests/`` on ``sys.path`` is what makes ``support.*``
importable without turning the test tree into an installable package.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
