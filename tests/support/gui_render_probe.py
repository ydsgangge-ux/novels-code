"""Probe the desktop chat renderer out-of-process and report JSON.

Why a subprocess: running Qt widgets *inside* the pytest process crashes the
interpreter on Windows (STATUS_STACK_BUFFER_OVERRUN) as soon as the renderer
touches the document. Keeping Qt in its own process makes the check reliable —
and a crash becomes a readable test failure instead of a dead session.

Output: one JSON object on stdout, ASCII-escaped so no console encoding can
break it. ``QT_QPA_PLATFORM=offscreen`` means no display is required.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
ANCHOR = "\u200b"


def main() -> int:
    from PyQt6 import QtWidgets

    # The reference must be kept: an unreferenced QApplication is garbage
    # collected immediately, and destroying the app while widgets live crashes
    # the process (0xC0000409) instead of raising.
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    spec = importlib.util.spec_from_file_location("gangge_desktop_app", ROOT / "desktop" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    window_cls = module.GanggeDesktop

    class View:
        _append_output = window_cls._append_output
        _strip_rich_markup = window_cls._strip_rich_markup
        _STREAM_ANCHOR = window_cls._STREAM_ANCHOR
        _RICH_TAG = window_cls._RICH_TAG

        def __init__(self) -> None:
            self._output = QtWidgets.QTextEdit()
            self._thinking_bubbles: dict[str, str] = {}
            self._live_role = ""
            self._live_pos = -1

        @property
        def plain(self) -> str:
            return self._output.toPlainText().replace(ANCHOR, "")

    result: dict[str, object] = {}

    # 1. Several streamed chunks must grow ONE bubble.
    view = View()
    for chunk in ("第一段", "第二段", "第三段"):
        view._append_output(chunk, "assistant")
    result["streamed_text"] = view.plain
    result["streamed_headers"] = view.plain.count("AI")

    # 2. Another role closes the bubble; the next answer is a new card.
    view2 = View()
    view2._append_output("A1", "assistant")
    view2._append_output("tool output", "tool")
    view2._append_output("A2", "assistant")
    result["turn_headers"] = view2.plain.count("AI")

    # 3. Reasoning closes the bubble too.
    view3 = View()
    view3._append_output("A1", "assistant")
    view3._append_output("reasoning", "thinking")
    view3._append_output("A2", "assistant")
    result["thinking_headers"] = view3.plain.count("AI")
    result["has_thinking"] = "思考过程" in view3.plain

    # 4. Rich markup never reaches the document.
    view4 = View()
    view4._append_output("[dim]── 第 1 轮 ──[/dim]", "system")
    result["system_text"] = view4.plain.strip()

    # 5. The stripper itself: terminal tags go, real brackets stay.
    strip = window_cls._strip_rich_markup
    result["stripped"] = [
        strip("[dim]── 第 1 轮 ──[/dim]"),
        strip("[bold cyan]hi[/bold cyan]"),
        strip("[yellow]❓ 问题[/yellow]"),
        strip("list[int] 和 [1]"),
        strip("[MCP:fs__read] read_file"),
        strip("[链接](http://x)"),
    ]

    json.dump(result, sys.stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
