"""Desktop chat renderer: streaming append + Rich-markup stripping.

The GUI had no test at all, which is how a regression shipped: with streaming
enabled, every chunk became its own card, so one answer produced dozens of
"🤖 AI 21:26" headers and every reasoning token produced a separate
"🧠 思考过程" bubble.

Qt widgets cannot be driven inside the pytest process on Windows (the
interpreter dies with STATUS_STACK_BUFFER_OVERRUN), so the renderer runs in a
subprocess (``tests/support/gui_render_probe.py``) and reports JSON. A crash
there is a normal, readable test failure.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parent / "support" / "gui_render_probe.py"
ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("PyQt6") is None,
    reason="PyQt6 not installed",
)


@pytest.fixture(scope="module")
def rendered() -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(PROBE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        cwd=str(ROOT),
        env=env,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"renderer probe exited with {proc.returncode}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return json.loads(proc.stdout)


def test_streamed_chunks_grow_one_bubble(rendered):
    assert rendered["streamed_headers"] == 1, (
        f"one card header expected, got {rendered['streamed_headers']}"
    )
    text = rendered["streamed_text"].replace("\n", "")
    assert "第一段第二段第三段" in text, f"chunks out of order: {text!r}"


def test_new_turn_starts_a_new_bubble(rendered):
    assert rendered["turn_headers"] == 2


def test_reasoning_closes_the_live_bubble(rendered):
    assert rendered["has_thinking"] is True
    assert rendered["thinking_headers"] == 2, (
        "a reasoning block must not be appended into the answer card"
    )


def test_rich_markup_never_reaches_the_document(rendered):
    assert rendered["system_text"] == "── 第 1 轮 ──"


def test_rich_markup_is_stripped(rendered):
    dimmed, bold, yellow, _plain, _mcp, _link = rendered["stripped"]
    assert dimmed == "── 第 1 轮 ──"
    assert bold == "hi"
    assert yellow == "❓ 问题"


def test_markup_stripper_leaves_real_brackets_alone(rendered):
    _dimmed, _bold, _yellow, plain, mcp, link = rendered["stripped"]
    assert plain == "list[int] 和 [1]"
    assert mcp == "[MCP:fs__read] read_file"
    assert link == "[链接](http://x)"
