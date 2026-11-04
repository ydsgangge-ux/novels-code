"""人物纪录片的「填写」页：把分镜从 JSON 变成一张能改的表格。

为什么值得单独做一块表单：让模型写人物生平，年份和事件是它编的；而这块表格里
每一行都是人填的，填错了看得见、改得动。表单**不碰渲染**，它只负责把表格翻译成
一份合法分镜（``to_storyboard()``），后面还是走 ``person.py`` 那条老路。

三个设计取舍：

* **正文单独一个编辑框**，不塞进表格单元格。一条正文 35-70 字，塞进
  ``QTableWidget`` 的单行编辑器里，写的人会疯。
* **照片按行绑**，不是"整个目录轮流发"。表单里点哪一行就给哪一行选图，
  这样才能决定"哪张配哪个年份"。
* **边改边存**。草稿写到 ``.gangge/person-draft.json``，关掉软件再打开接着填——
  没有这一步，它就只是个一次性的对话框，不叫模板。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: 草稿位置：相对当前工作目录。桌面端换目录时不跟着漂，够用且好找。
DEFAULT_DRAFT = Path(".gangge") / "person-draft.json"
#: 预览页固定落这里，反复覆盖——预览是"看一眼"，不该堆出一堆目录。
PREVIEW_DIR = Path(".gangge") / "person_preview"

#: 配音下拉里只放几把常用的嗓子：全列出 edge-tts 的上百个音色，用户反而无从选。
VOICES: tuple[tuple[str, str], ...] = (
    ("云健 · 男声，纪录片感", "zh-CN-YunjianNeural"),
    ("云希 · 男声，年轻", "zh-CN-YunxiNeural"),
    ("晓晓 · 女声，通用", "zh-CN-XiaoxiaoNeural"),
    ("晓伊 · 女声，温和", "zh-CN-XiaoyiNeural"),
)

_EMPTY_NODE: dict[str, Any] = {"year": "", "title": "", "text": "", "photos": []}

_STYLE = """
QWidget#PersonForm { background: #0d1117; }
QLineEdit, QPlainTextEdit, QComboBox {
    background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
    color: #c9d1d9; font-size: 11px; padding: 4px 6px;
}
QLineEdit:focus, QPlainTextEdit:focus { border-color: #f78166; }
QLabel { color: #8b949e; font-size: 11px; }
QTableWidget {
    background: #0d1117; border: 1px solid #21262d; border-radius: 4px;
    color: #c9d1d9; font-size: 11px; gridline-color: #21262d;
}
QTableWidget::item:selected { background: rgba(31, 111, 235, 0.35); color: #f0f6fc; }
QHeaderView::section {
    background: #161b22; color: #8b949e; border: none;
    border-bottom: 1px solid #21262d; padding: 4px; font-size: 10px;
}
QPushButton {
    background: #21262d; border: 1px solid #30363d; border-radius: 4px;
    color: #c9d1d9; font-size: 11px; padding: 4px 10px;
}
QPushButton:hover { background: #30363d; }
QPushButton#primary {
    background: #238636; border-color: #2ea043; color: #ffffff; font-weight: bold;
}
QPushButton#primary:hover { background: #2ea043; }
"""


def _new_node() -> dict[str, Any]:
    return {"year": "", "title": "", "text": "", "photos": []}


class PersonFormWidget(QWidget):
    """人物纪录片的内容表单：人名 + 一串人生节点。"""

    #: 预览生成好了，让人知道文件在哪（主窗口可以拿去写日志）。
    previewed = pyqtSignal(str)

    def __init__(self, draft_path: Path | str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PersonForm")
        self.setStyleSheet(_STYLE)
        self._draft_path = Path(draft_path) if draft_path else DEFAULT_DRAFT
        self._nodes: list[dict[str, Any]] = []
        self._text_row = -1
        self._loading = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.save_draft)

        self._build()
        self.load_draft()

    # ── 构建 ────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        head = QFormLayout()
        head.setSpacing(4)
        head.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._name = QLineEdit()
        self._name.setPlaceholderText("人物名，如「钱学森」")
        self._name.textChanged.connect(self._on_changed)
        head.addRow("人名", self._name)

        self._subtitle = QLineEdit()
        self._subtitle.setPlaceholderText("一句话概括一生，如「他让中国有了箭」")
        self._subtitle.textChanged.connect(self._on_changed)
        head.addRow("副标题", self._subtitle)

        opts = QHBoxLayout()
        opts.setSpacing(6)
        self._aspect = QComboBox()
        self._aspect.addItem("横屏 16:9", "16:9")
        self._aspect.addItem("竖屏 9:16", "9:16")
        self._aspect.currentIndexChanged.connect(self._on_changed)
        opts.addWidget(self._aspect)

        self._voice = QComboBox()
        for label, value in VOICES:
            self._voice.addItem(label, value)
        self._voice.currentIndexChanged.connect(self._on_changed)
        opts.addWidget(self._voice, 1)
        head.addRow("画幅/配音", self._wrap(opts))

        self._ending = QLineEdit()
        self._ending.setPlaceholderText("结尾那句总结（可留空）")
        self._ending.textChanged.connect(self._on_changed)
        head.addRow("结语", self._ending)

        lay.addLayout(head)

        # ── 节点表 ──
        lay.addWidget(QLabel("人生节点（按时间顺序）"))

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["年份", "标题", "照片"])
        self._table.verticalHeader().setDefaultSectionSize(26)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 66)
        self._table.setColumnWidth(2, 72)
        self._table.setMinimumHeight(104)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(
            lambda row, *_: self._on_row_changed(row)
        )
        # 表格高度跟着行数走（见 _autosize_table），多出来的高度全给正文：
        # 写一段 60 字的人比看四行表格的人更需要地方。
        lay.addWidget(self._table, 0)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        for text, tip, slot in (
            ("＋ 节点", "在这一行后面插入一个节点", self._add_node),
            ("↑", "上移", lambda: self._move(-1)),
            ("↓", "下移", lambda: self._move(1)),
            ("🗑", "删除选中的节点", self._remove_node),
            ("🖼 照片", "给选中的这一行选照片", self._pick_photos),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        bar.addStretch(1)
        lay.addLayout(bar)

        # ── 正文：跟着选中行走 ──
        lay.addWidget(QLabel("正文（选中哪一行就编辑哪一行）"))
        self._body = QPlainTextEdit()
        self._body.setPlaceholderText("这一站发生了什么，35-70 字。写具体的人、地点或动作。")
        self._body.setMinimumHeight(64)
        self._body.textChanged.connect(self._on_body_changed)
        lay.addWidget(self._body, 1)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self._preview_btn = QPushButton("▶ 预览这一版")
        self._preview_btn.setObjectName("primary")
        self._preview_btn.setToolTip("生成可在浏览器播放的页面，不渲染视频")
        self._preview_btn.clicked.connect(self.preview)
        foot.addWidget(self._preview_btn)
        foot.addStretch(1)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        foot.addWidget(self._status, 2)
        lay.addLayout(foot)

    @staticmethod
    def _wrap(layout) -> QWidget:
        box = QWidget()
        box.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return box

    # ── 数据 ←→ 界面 ───────────────────────────────────────────
    @property
    def nodes(self) -> list[dict[str, Any]]:
        """当前节点列表的副本（测试与主窗口都从这里读）。"""
        return [dict(n) for n in self._nodes]

    def to_dict(self) -> dict[str, Any]:
        """表单内容 → 草稿结构。"""
        return {
            "name": self._name.text().strip(),
            "subtitle": self._subtitle.text().strip(),
            "ending": self._ending.text().strip(),
            "aspect": self._aspect.currentData(),
            "voice": self._voice.currentData(),
            "nodes": [
                {
                    "year": n["year"],
                    "title": n["title"],
                    "text": n["text"],
                    "photos": list(n["photos"]),
                }
                for n in self._nodes
            ],
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        """草稿 → 表单。缺字段就当空，不让一份残缺的 draft 打不开界面。"""
        self._loading = True
        try:
            self._name.setText(str(data.get("name") or ""))
            self._subtitle.setText(str(data.get("subtitle") or ""))
            self._ending.setText(str(data.get("ending") or ""))

            aspect = str(data.get("aspect") or "16:9")
            index = self._aspect.findData(aspect)
            self._aspect.setCurrentIndex(index if index >= 0 else 0)
            voice = str(data.get("voice") or VOICES[0][1])
            index = self._voice.findData(voice)
            self._voice.setCurrentIndex(index if index >= 0 else 0)

            nodes = data.get("nodes")
            self._nodes = [
                {
                    "year": str(n.get("year") or ""),
                    "title": str(n.get("title") or ""),
                    "text": str(n.get("text") or ""),
                    "photos": [str(p) for p in (n.get("photos") or []) if str(p).strip()],
                }
                for n in (nodes or [])
                if isinstance(n, dict)
            ]
            if not self._nodes:
                self._nodes = [_new_node()]
        finally:
            self._loading = False
        self._refresh_table(select=0)

    def _refresh_table(self, select: int = -1) -> None:
        self._loading = True
        try:
            self._table.setRowCount(len(self._nodes))
            for row, node in enumerate(self._nodes):
                year = QTableWidgetItem(node["year"])
                year.setToolTip("年份或年代，留空则不显示")
                title = QTableWidgetItem(node["title"])
                count = len(node["photos"])
                photo = QTableWidgetItem(f"{count} 张" if count else "—")
                photo.setFlags(photo.flags() & ~Qt.ItemFlag.ItemIsEditable)
                photo.setToolTip(
                    "\n".join(node["photos"]) if count else "双击「🖼 照片」按钮给这一行选图"
                )
                photo.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                for col, item in enumerate((year, title, photo)):
                    self._table.setItem(row, col, item)
            if 0 <= select < len(self._nodes):
                self._table.setCurrentCell(select, 1)
        finally:
            self._loading = False
        self._autosize_table()
        self._on_row_changed(self._table.currentRow())

    def _autosize_table(self) -> None:
        """表格按行数收放：四行占半屏只会让正文没地方写。

        超过 300px 就固定住让表格自己滚——节点有十几个时不该把整个面板顶下去。
        """
        header = self._table.horizontalHeader().height()
        wanted = header + len(self._nodes) * 28 + 6
        self._table.setMaximumHeight(max(120, min(300, wanted)))

    def _on_row_changed(self, row: int) -> None:
        self._text_row = row
        self._loading = True
        try:
            self._body.setPlainText(
                self._nodes[row]["text"] if 0 <= row < len(self._nodes) else ""
            )
        finally:
            self._loading = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._nodes)):
            return
        if col == 0:
            self._nodes[row]["year"] = item.text().strip()
        elif col == 1:
            self._nodes[row]["title"] = item.text().strip()
        else:
            return
        self._on_changed()

    def _on_body_changed(self) -> None:
        if self._loading or not (0 <= self._text_row < len(self._nodes)):
            return
        self._nodes[self._text_row]["text"] = self._body.toPlainText().strip()
        self._on_changed()

    def _on_changed(self, *_) -> None:
        if self._loading:
            return
        self._status.setText("")
        self._save_timer.start()

    # ── 增删改 ──────────────────────────────────────────────────
    def _add_node(self) -> None:
        row = self._table.currentRow()
        at = len(self._nodes) if row < 0 else row + 1
        self._nodes.insert(at, _new_node())
        self._refresh_table(select=at)
        self._on_changed()
        self._table.editItem(self._table.item(at, 1))

    def _remove_node(self) -> None:
        row = self._table.currentRow()
        if not (0 <= row < len(self._nodes)):
            return
        self._nodes.pop(row)
        if not self._nodes:
            self._nodes = [_new_node()]
        self._refresh_table(select=min(row, len(self._nodes) - 1))
        self._on_changed()

    def _move(self, delta: int) -> None:
        row = self._table.currentRow()
        target = row + delta
        if not (0 <= row < len(self._nodes)) or not (0 <= target < len(self._nodes)):
            return
        self._nodes[row], self._nodes[target] = self._nodes[target], self._nodes[row]
        self._refresh_table(select=target)
        self._on_changed()

    def _pick_photos(self) -> None:
        row = self._table.currentRow()
        if not (0 <= row < len(self._nodes)):
            self._status.setText("先选中一行，再给它选照片。")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择这一站的照片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.avif)",
        )
        if not files:
            return
        self._nodes[row]["photos"] = files
        self._refresh_table(select=row)
        self._on_changed()

    # ── 校验 / 换成分镜 ────────────────────────────────────────
    def validate(self) -> str:
        """返回一句人话；空字符串表示可以出片。"""
        if not self._name.text().strip():
            return "先填人名——片头和结束卡都用它。"
        usable = [n for n in self._nodes if n["title"] or n["text"]]
        if not usable:
            return "至少要有一个人生节点：填个年份加一句标题。"
        if len(usable) > 12:
            return f"节点有 {len(usable)} 个，太多了——一条片子 7-10 个更好记。"
        return ""

    def to_storyboard(self) -> dict[str, Any]:
        """表单 → 分镜 dict（就是 ``Storyboard.from_dict`` 吃的那一份）。

        表单里的一行 = 一个人生节点，所以这里补出开场和结尾两个镜头：
        用户不需要知道"第一个必须是 title"这种规矩。
        """
        name = self._name.text().strip()
        subtitle = self._subtitle.text().strip()
        scenes: list[dict[str, Any]] = [
            {"template": "title", "title": name, "text": subtitle}
        ]
        for node in self._nodes:
            if not (node["title"] or node["text"]):
                continue
            scenes.append(
                {
                    "template": "quote",
                    "year": node["year"],
                    "title": node["title"],
                    "text": node["text"],
                    "photos": list(node["photos"]),
                }
            )
        ending = self._ending.text().strip()
        if ending:
            scenes.append({"template": "outro", "title": "", "text": ending})

        portrait = self._aspect.currentData() == "9:16"
        return {
            "title": name,
            "subtitle": subtitle,
            "style": "dark",
            "voice": self._voice.currentData() or VOICES[0][1],
            "width": 1080 if portrait else 1920,
            "height": 1920 if portrait else 1080,
            "scenes": scenes,
        }

    # ── 预览 ────────────────────────────────────────────────────
    def preview(self) -> Path | None:
        """生成可播放的页面并在浏览器里打开；失败时把原因写在状态栏。"""
        problem = self.validate()
        if problem:
            self._set_status(problem, warn=True)
            return None

        from gangge.layer4_tools.storyboard import Storyboard, write_preview

        try:
            storyboard = Storyboard.from_dict(self.to_storyboard())
            out = Path(PREVIEW_DIR)
            html = write_preview(storyboard, out, page="person")
        except Exception as exc:  # noqa: BLE001 - 用户填错不该炸窗口
            self._set_status(f"预览失败：{exc}", warn=True)
            return None

        self.save_draft()
        self._set_status(f"已生成：{html}（{storyboard.total_duration:.0f} 秒）")
        self.previewed.emit(str(html))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(html.resolve())))
        return html

    def _set_status(self, text: str, warn: bool = False) -> None:
        color = "#d29922" if warn else "#3fb950"
        self._status.setStyleSheet(f"color:{color};font-size:10px;")
        self._status.setText(text)

    # ── 草稿 ────────────────────────────────────────────────────
    def save_draft(self) -> Path | None:
        try:
            self._draft_path.parent.mkdir(parents=True, exist_ok=True)
            self._draft_path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return self._draft_path
        except OSError:
            # 草稿存不下不该挡住出片：磁盘满、只读目录都可能发生
            return None

    def load_draft(self) -> None:
        data: dict[str, Any] = {}
        try:
            if self._draft_path.is_file():
                data = json.loads(self._draft_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.load_dict(data)
