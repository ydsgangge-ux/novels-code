"""A tiny snapshot helper — the Python answer to Codex's ``insta`` snapshots.

Why snapshots at all: the most valuable regression test for an agent is not
"did the function return 3" but "did the *request we send the model* change".
Prompt drift is silent — nothing fails, the agent just gets worse. Freezing the
observable shape (and, where deterministic, the text) makes that drift a red
diff instead of a mystery.

Usage::

    assert_snapshot(rendered_text, "loop/tool_chain")

    # regenerate after an intentional change:
    #   $env:GANGGE_UPDATE_SNAPSHOTS=1 ; python -m pytest tests

A missing snapshot file is written on first run (so tests are not born red), and
an existing one must match exactly.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots"


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.snap"


def assert_snapshot(value: Any, name: str) -> Path:
    """Compare ``value`` against ``tests/snapshots/<name>.snap``."""
    path = snapshot_path(name)
    rendered = _render(value)

    update = os.environ.get("GANGGE_UPDATE_SNAPSHOTS", "").strip() == "1"
    if update or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return path

    expected = path.read_text(encoding="utf-8")
    if expected == rendered:
        return path

    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            rendered.splitlines(),
            fromfile=f"{name}.snap (expected)",
            tofile=f"{name}.snap (actual)",
            lineterm="",
        )
    )
    raise AssertionError(
        f"快照不一致: {path}\n{diff}\n\n"
        "如果这是有意的行为变更，用 GANGGE_UPDATE_SNAPSHOTS=1 重新生成并提交。"
    )
