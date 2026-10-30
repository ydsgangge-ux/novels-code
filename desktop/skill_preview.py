"""技能「效果」预览面板：一层静态图，一层真动画。

两层的分工是刻意的：

* **图片**永远是主角。一张 PNG，谁都能看，不依赖 QtWebEngine、不吃内存。
* **动画**是加分项。有 WebEngine 且机器带得动时，把同一份 storyboard HTML
  嵌进来真播；装不了、内存不够，就只在这块说清原因，图片那一块照常可用。

QtWebEngine 必须延迟创建：它一加载就是 200 MB 的 Chromium，在内存紧张的机器
上可能直接把进程带走。所以只有用户主动点「播放」才 import 并 new。
"""

from __future__ import annotations

import ctypes
import importlib.util
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gangge.layer4_tools.skills import Skill, preview_image
from gangge.layer4_tools.storyboard.demo import demo_storyboard
from gangge.layer4_tools.storyboard.schema import STYLE_NAMES
from gangge.layer4_tools.storyboard.templates import render_page

_IDLE = "点左侧「⚡ 技能」挑一条，这里会显示它的效果图。"
_NO_IMAGE = "这条技能是直接对话型的，没有视觉预览。"
_FOOTER = "图片是定格示例；动画按同一份 storyboard 实时渲染。"
_ANIM_IDLE = "点「▶ 播放动画」在这里真播一遍（需要 PyQt6-WebEngine）。"

#: Chromium 一启动就要预留一大块提交内存；低于这个数就别去试，直接说原因。
#: 本机实测：可用提交 0.1 GB 时 QWebEngineView 一 show 就 abort。
MIN_FREE_COMMIT_MB = 1024

_ANIM_DIR = Path(tempfile.gettempdir()) / "gangge-skill-anim"


# ── 环境探测（都不 import WebEngine，探测本身零成本） ──────────────

def free_commit_mb() -> int | None:
    """Windows 上的可用提交内存（MB）；其它平台返回 ``None`` 表示"不知道"。"""
    if sys.platform != "win32":
        return None

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):  # pragma: no cover - 非 Windows 才会走到
        return None
    if not ok:
        return None
    return int(status.ullAvailPageFile // (1024 * 1024))


def webengine_status() -> tuple[bool, str]:
    """``(能不能播, 不能播的原因)``——原因是要直接给用户看的一句话。"""
    if importlib.util.find_spec("PyQt6.QtWebEngineWidgets") is None:
        return False, "没装 PyQt6-WebEngine，动画不可用；图片照常可看。"
    free = free_commit_mb()
    if free is not None and free < MIN_FREE_COMMIT_MB:
        return False, (
            f"可用内存只剩 {free} MB（动画至少要 {MIN_FREE_COMMIT_MB} MB），"
            "关掉些程序再试；图片照常可看。"
        )
    return True, ""


def animation_html(skill: Skill) -> Path | None:
    """把技能对应的示例 storyboard 落成 HTML（同一份缓存复用）。"""
    stem = Path(skill.preview).stem
    # 竖屏技能没有独立风格，用 neon 的配色 + 9:16 画幅
    portrait = stem == "portrait"
    style = "neon" if portrait else stem
    if style not in STYLE_NAMES:
        return None

    name = "portrait" if portrait else style
    path = _ANIM_DIR / f"{name}.html"
    if not path.is_file():
        storyboard = demo_storyboard(style)
        if portrait:
            storyboard.width, storyboard.height = 1080, 1920
        _ANIM_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(render_page(storyboard, storyboard.timeline()), encoding="utf-8")
    return path


# ── 面板 ────────────────────────────────────────────────────────

class SkillEffectPanel(QWidget):
    """右侧「效果」页签：图片（人人可看）+ 动画（有环境才播）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._fitted_width = -1
        self._skill: Skill | None = None
        self._web_view = None  # 延迟到用户点播放才创建
        self.setStyleSheet("SkillEffectPanel{background:#0d1117;}")
        self._build()
        self.clear()

    # ── 构建 ────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet("color:#f0f6fc;font-size:13px;font-weight:bold;")
        lay.addWidget(self._title)

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#8b949e;font-size:11px;")
        lay.addWidget(self._hint)

        self._views = QTabWidget()
        self._views.setDocumentMode(True)
        self._views.setStyleSheet(
            "QTabWidget::pane{border:1px solid #21262d;border-radius:4px;background:#0d1117;}"
            "QTabBar::tab{background:#161b22;color:#8b949e;border:none;"
            "padding:5px 12px;font-size:11px;}"
            "QTabBar::tab:selected{color:#f0f6fc;border-bottom:2px solid #f78166;}"
        )
        self._views.addTab(self._build_image_tab(), "🖼 图片")
        self._views.addTab(self._build_anim_tab(), "🎬 动画")
        lay.addWidget(self._views, 1)

        self._footer = QLabel(_FOOTER)
        self._footer.setWordWrap(True)
        self._footer.setStyleSheet("color:#6e7681;font-size:10px;")
        lay.addWidget(self._footer)

    def _build_image_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 0, 0, 0)

        # widgetResizable 交给 QLabel 的 sizeHint 会按原图撑宽、出横向滚动条，
        # 所以这里自己按视口宽度算尺寸。
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setStyleSheet(
            "QScrollArea{background:#0d1117;border:none;}"
        )
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setStyleSheet("background:#0d1117;color:#6e7681;font-size:11px;")
        self._scroll.setWidget(self._image)
        lay.addWidget(self._scroll)

        self._empty = QLabel(_IDLE)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color:#6e7681;font-size:11px;padding:12px;")
        lay.addWidget(self._empty)
        return tab

    def _build_anim_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._play_btn = QPushButton("▶ 播放动画")
        self._play_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:4px 10px;}"
            "QPushButton:hover{background:#30363d;}"
        )
        self._play_btn.clicked.connect(self._play_animation)
        bar.addWidget(self._play_btn)

        self._browser_btn = QPushButton("🌐 浏览器")
        self._browser_btn.setToolTip("用系统浏览器打开同一份 HTML（不需要 QtWebEngine）")
        self._browser_btn.setStyleSheet(self._play_btn.styleSheet())
        self._browser_btn.clicked.connect(self._open_in_browser)
        bar.addWidget(self._browser_btn)
        bar.addStretch(1)
        lay.addLayout(bar)

        self._anim_status = QLabel(_ANIM_IDLE)
        self._anim_status.setWordWrap(True)
        self._anim_status.setStyleSheet("color:#6e7681;font-size:10px;")
        lay.addWidget(self._anim_status)

        self._anim_stage = QWidget()
        self._anim_layout = QVBoxLayout(self._anim_stage)
        self._anim_layout.setContentsMargins(0, 0, 0, 0)
        self._anim_placeholder = QLabel("这里会嵌入 storyboard 动画。")
        self._anim_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._anim_placeholder.setWordWrap(True)
        self._anim_placeholder.setStyleSheet(
            "background:#0d1117;border:1px dashed #30363d;border-radius:4px;"
            "color:#6e7681;font-size:11px;padding:16px;"
        )
        self._anim_layout.addWidget(self._anim_placeholder)
        lay.addWidget(self._anim_stage, 1)
        return tab

    # ── 对外 ────────────────────────────────────────────────────
    def show_skill(self, skill: Skill | None) -> None:
        """展示一条技能；``None`` 或没有图时退化成说明文字。"""
        if skill is None:
            self.clear()
            return

        self._skill = skill
        self._title.setText(skill.label)
        self._hint.setText(skill.hint)
        self._hint.show()
        self._reset_animation()

        path = preview_image(skill)
        if path is None:
            self._set_placeholder(_NO_IMAGE)
            return

        loaded = QPixmap(str(path))
        if loaded.isNull():
            self._set_placeholder(f"预览图加载失败：{path.name}")
            return

        self._pixmap = loaded
        self._fitted_width = -1
        self._image.setText("")
        self._empty.hide()
        self._scroll.show()
        self._footer.show()
        self._fit()
        # 刚切页签时视口宽度还是旧值，等布局跑完再量一次
        QTimer.singleShot(0, self._fit)

    def clear(self) -> None:
        self._skill = None
        self._pixmap = QPixmap()
        self._fitted_width = -1
        self._title.setText("效果预览")
        self._hint.setText(_IDLE)
        self._set_placeholder(_IDLE)
        self._reset_animation()

    @property
    def current_pixmap(self) -> QPixmap:
        """当前展示的原始图（未缩放）；给测试用的只读快照。"""
        return self._pixmap

    # ── 图片 ────────────────────────────────────────────────────
    def _set_placeholder(self, text: str) -> None:
        self._pixmap = QPixmap()
        self._fitted_width = -1
        self._image.clear()
        self._scroll.hide()
        self._empty.setText(text)
        self._empty.show()
        self._footer.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        if self._pixmap.isNull():
            return
        width = self._scroll.viewport().width() - 2
        if width <= 1 or width == self._fitted_width:
            return
        self._fitted_width = width
        height = max(1, round(self._pixmap.height() * width / self._pixmap.width()))
        self._image.setFixedSize(width, height)
        self._image.setPixmap(
            self._pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ── 动画 ────────────────────────────────────────────────────
    def _reset_animation(self) -> None:
        self._set_status(_ANIM_IDLE)
        if self._web_view is not None:
            self._web_view.hide()
            self._anim_placeholder.show()

    def _set_status(self, text: str, tone: str = "muted") -> None:
        """``tone``：muted 日常 / warn 环境不满足 / ok 已开播。"""
        color = {"warn": "#d29922", "ok": "#3fb950"}.get(tone, "#6e7681")
        self._anim_status.setStyleSheet(f"color:{color};font-size:10px;")
        self._anim_status.setText(text)

    def _ensure_web_view(self):
        """真正创建 QWebEngineView——只在这里 import，前面一律不碰。"""
        from PyQt6.QtWebEngineWidgets import QWebEngineView

        view = QWebEngineView()
        self._anim_placeholder.hide()
        self._anim_layout.addWidget(view)
        self._web_view = view
        return view

    def _play_animation(self) -> None:
        if self._skill is None:
            self._set_status("先挑一条技能。", "warn")
            return

        html = animation_html(self._skill)
        if html is None:
            self._set_status("这条技能没有动画样例。", "warn")
            return

        ok, reason = webengine_status()
        if not ok:
            self._set_status(reason, "warn")
            return

        try:
            view = self._web_view or self._ensure_web_view()
        except Exception as exc:  # noqa: BLE001 - 缺 DLL 只报错，不该连带崩面板
            self._set_status(f"WebEngine 加载失败：{exc}", "warn")
            return

        view.show()
        view.setUrl(QUrl.fromLocalFile(str(html)))
        view.page().runJavaScript("window.__play && window.__play()")
        self._set_status(f"正在播放：{html.name}", "ok")

    def _open_in_browser(self) -> None:
        """零风险兜底：交给系统浏览器，不占 WebEngine、不吃内存。"""
        if self._skill is None:
            self._set_status("先挑一条技能。", "warn")
            return
        html = animation_html(self._skill)
        if html is None:
            self._set_status("这条技能没有动画样例。", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(html)))
        self._set_status(f"已在浏览器打开：{html.name}", "ok")
