"""桌宠 —— 透明置顶小窗口，跟着 Agent 的任务状态换动作。

素材由使用者自己提供，放在 .gangge_data/pet/<动作>/ 下（mp4 / webm / mov /
gif / png 都行），程序用 imageio-ffmpeg 逐帧解码后绘到透明窗口上。
仓库里另带一套内置素材（desktop/assets/pet），首次运行会把它复制进素材目录，
所以用户看到、想删的都只是那个目录里的普通文件。

动作约定（目录名即状态名）：
    idle      空闲（必须，其它动作缺失时都用它兜底）
    thinking  已发送任务，模型还在想
    working   模型输出中 / 正在调工具
    success   任务完成
    error     任务失败或被取消
    sleep     长时间没有任务
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover - 没装依赖时降级成占位图
    imageio_ffmpeg = None


ACTIONS = ("idle", "thinking", "working", "success", "error", "sleep")
ACTION_LABELS = {
    "idle": "空闲",
    "thinking": "思考中",
    "working": "干活中",
    "success": "完成",
    "error": "出错",
    "sleep": "睡觉",
}
MEDIA_EXTS = {
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v",
    ".gif", ".png", ".apng", ".webp", ".jpg", ".jpeg",
}
# 能用自动抠像处理的"实心背景"素材（gif/png 已经是图，不处理）
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}

MAX_SIDE = 400      # 解码输出的最大高度，超过就缩小（保护 CPU / 内存）
MAX_FPS = 24        # 播放帧率上限
MAX_ASSET_MB = 60   # 单个素材超过这个体积直接跳过
SEED_MARK = ".builtin_copied"   # 标记：内置素材已经铺过一次，删了就不再自动长回来

_README = """桌宠素材说明
================

每个动作一个文件夹，把视频/动图丢进去就能用（mp4 / webm / mov / gif / png）。
程序自带 idle / working / error 三套内置表情：第一次启动时，某个动作还是空的，
就会把内置表情复制进这个目录（所以你在这里看到的就是它在播的东西）。

不想看内置表情？
  * 直接删掉这里的文件就行，删掉之后程序不会再自动塞回来。
  * 删错了，右键桌宠 → "恢复内置表情"，会把缺的补回来（自己放的文件不受影响）。

  idle/      空闲 —— 必须有，其它动作没有素材时都用它
  thinking/  你按下发送、模型还在想
  working/   模型开始输出、正在调工具干活
  success/   任务完成
  error/     任务失败或被取消
  sleep/     长时间没有任务

格式与透明：
  * 想要"只看到角色本身"→ 用带透明通道的 webm（VP9 + alpha）或 mov（PNG + alpha）
  * mp4 没有透明通道，会是一块实心矩形背景（也能用，只是挡视线）
  * gif 只有 1 位透明，边缘会比较硬

自动抠像：
  * 直接把绿幕/白底/蓝底的视频丢进对应动作文件夹，然后右键窗口 →
    "自动抠像"，程序会按背景色自动分割，生成 <动作>_01.apng（带透明通道），
    原视频被改名成 xxx.mp4.source（不会再被播放，也不会重复处理）。
  * 背景不纯（有渐变、有花纹）时效果会变差，那种素材请自己做好透明再放进来。

建议：
  * 高度 256~512px、单个文件 2~6 秒循环
  * 同一动作放多个文件会轮流播放
  * 右键小窗口可以切换动作、调大小、打开这个文件夹
"""


def default_asset_root() -> Path:
    """素材默认目录：项目 .gangge_data/pet，不可写时退回用户目录。"""
    project_local = Path(__file__).resolve().parent.parent / ".gangge_data" / "pet"
    try:
        project_local.mkdir(parents=True, exist_ok=True)
        probe = project_local / ".write_test"
        probe.touch()
        probe.unlink()
        return project_local
    except OSError:
        home = Path.home() / ".gangge" / "pet"
        home.mkdir(parents=True, exist_ok=True)
        return home


def bundled_asset_root() -> Path:
    """随仓库发布的内置素材（desktop/assets/pet），保证开箱就有东西可播。"""
    return Path(__file__).resolve().parent / "assets" / "pet"


class PetAssets:
    """扫描素材目录，按动作归类。

    内置素材（desktop/assets/pet）不直接参与播放，而是首次运行时复制进用户的
    素材目录 —— 这样"删掉不想要的内置表情"就是删一个能看见的文件。
    """

    def __init__(self, root: Path | None = None):
        self.root = root or default_asset_root()
        self.bundled = bundled_asset_root()
        self.ensure()
        self.seed()

    def ensure(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for action in ACTIONS:
                (self.root / action).mkdir(exist_ok=True)
            readme = self.root / "README.txt"
            if not readme.exists():
                readme.write_text(_README, encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _scan(folder: Path) -> list[Path]:
        if not folder.is_dir():
            return []
        try:
            items = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
        except OSError:
            return []
        result = []
        for p in items:
            try:
                if p.stat().st_size > MAX_ASSET_MB * 1024 * 1024:
                    continue        # 超大素材跳过，避免一开桌宠就卡死
            except OSError:
                continue
            result.append(p)
        return sorted(result, key=lambda p: p.name.lower())

    def seed(self, force: bool = False) -> int:
        """把内置素材复制进用户素材目录，返回复制了几个文件。

        只在"这个动作还没有任何素材"时自动铺一次（留下标记，删掉之后不会再自己长回来）；
        force=True 用于右键菜单的"恢复内置素材"，只补缺失的文件。
        """
        if self.bundled == self.root or not self.bundled.is_dir():
            return 0
        marker = self.root / SEED_MARK
        if marker.exists() and not force:
            return 0

        copied = 0
        for action in ACTIONS:
            src_dir = self.bundled / action
            if not src_dir.is_dir():
                continue
            if not force and self._scan(self.root / action):
                continue                    # 用户已经放了自己的素材，不打扰
            for src in self._scan(src_dir):
                dst = self.root / action / src.name
                if dst.exists():
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
                except OSError:
                    pass
        try:
            marker.write_text("内置素材已铺到本目录，删掉其中文件即可不再显示。\n", encoding="utf-8")
        except OSError:
            pass
        return copied

    def files(self, action: str) -> list[Path]:
        return self._scan(self.root / action)

    def count(self, action: str) -> int:
        return len(self.files(action))


def _ffmpeg_exe() -> str:
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return "ffmpeg"


def _popen_flags() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def probe_video(path: str | Path) -> tuple[int, int, float]:
    """用 ffmpeg 的流信息读分辨率与帧率。

    imageio 的 read_frames 只给 rgb24，读透明素材会踩内存，因此这里自己解析。
    """
    try:
        proc = subprocess.run(
            [_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, errors="replace", timeout=20,
            **_popen_flags(),
        )
        text = proc.stderr or ""
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"无法启动 ffmpeg：{e}") from e

    size = None
    for m in re.finditer(r"(\d{2,5})x(\d{2,5})", text):
        w, h = int(m.group(1)), int(m.group(2))
        if 16 <= w <= 8192 and 16 <= h <= 8192:
            size = (w, h)
            break
    if size is None:
        raise RuntimeError("无法读取视频尺寸（可能是坏文件）")

    fps = 15.0
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if fps_match:
        try:
            fps = float(fps_match.group(1))
        except ValueError:
            pass
    if not (1.0 <= fps <= 120.0):
        fps = 15.0
    return size[0], size[1], fps


def probe_video_size(path: str | Path) -> tuple[int, int]:
    w, h, _ = probe_video(path)
    return w, h


def _decoder_candidates(path: str) -> list[list[str]]:
    """带 alpha 的 webm 必须用 libvpx 解码器，ffmpeg 内置解码器会丢掉透明通道。"""
    if Path(path).suffix.lower() == ".webm":
        return [["-c:v", "libvpx-vp9"], ["-c:v", "libvpx"], []]
    return [[]]


class FrameThread(QThread):
    """后台解码：自己起 ffmpeg 输出 RGBA rawvideo，逐帧推给 UI。

    不用 imageio_ffmpeg.read_frames —— 它固定按 rgb24 输出，
    按 RGBA 去读会让缓冲区越界（之前卡死的原因）。
    """

    frame = pyqtSignal(QImage)
    failed = pyqtSignal(str)

    def __init__(self, path: Path, loop: bool = True, parent=None):
        super().__init__(parent)
        self._path = str(path)
        self._loop = loop
        self._stop = False
        self._ack = threading.Event()

    def stop(self):
        self._stop = True
        self._ack.set()

    def ack(self):
        self._ack.set()

    def run(self):
        try:
            src_w, src_h, src_fps = probe_video(self._path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
            return

        # 统一缩到 MAX_SIDE 高以内（小图不放大），并按 2 的倍数对齐
        out_h = min(MAX_SIDE, src_h)
        out_w = max(2, int(round(src_w * out_h / src_h / 2)) * 2)
        stride = out_w * 4
        frame_bytes = stride * out_h
        # 目标帧率：不超过 24，也不超过素材本身（避免给 15fps 素材插帧、白白多解码）
        target_fps = max(1.0, min(float(MAX_FPS), src_fps))
        frame_interval = 1.0 / target_fps

        candidates = _decoder_candidates(self._path)
        cand = 0
        while not self._stop:
            cmd = [
                _ffmpeg_exe(), "-hide_banner", "-v", "error",
                *candidates[cand],
                "-i", self._path,
                "-an",                                        # 丢掉音轨，避免干扰数据流
                "-vf", f"scale=-2:min({MAX_SIDE}\\,ih)",       # 大视频先缩小再传
                "-r", str(int(round(target_fps))),
                "-f", "rawvideo", "-pix_fmt", "rgba", "-",
            ]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    **_popen_flags(),
                )
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e))
                return

            frames = 0
            next_at = time.perf_counter()
            try:
                while not self._stop:
                    buf = proc.stdout.read(frame_bytes)
                    if not buf or len(buf) < frame_bytes:
                        break
                    img = QImage(buf, out_w, out_h, stride, QImage.Format.Format_RGBA8888).copy()
                    self._ack.clear()
                    self.frame.emit(img)
                    self._ack.wait(0.25)      # 等 UI 画完再取下一帧，避免事件队列堆积
                    frames += 1
                    # 按目标帧率节流；已经落后就直接重置节奏，不做"追赶"式狂刷
                    next_at += frame_interval
                    wait = next_at - time.perf_counter()
                    if wait > 0:
                        self.msleep(int(wait * 1000))
                    else:
                        next_at = time.perf_counter()
            except Exception as e:  # noqa: BLE001
                if not self._stop:
                    self.failed.emit(str(e))
                return
            finally:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

            if frames == 0 and cand + 1 < len(candidates):
                cand += 1          # 这个解码器一帧都没解出来，换下一个
                continue
            if self._stop or not self._loop or frames <= 1:
                return
            self.msleep(40)


def load_matte():
    """按"直接运行"和"当包导入"两种方式找 pet_matte；找不到返回 None。"""
    try:
        import pet_matte  # type: ignore[import-not-found]
        return pet_matte
    except Exception:  # noqa: BLE001
        pass
    try:
        from desktop import pet_matte as module  # type: ignore[import-not-found]
        return module
    except Exception:  # noqa: BLE001
        return None


def matte_available() -> bool:
    """自动抠像依赖 cv2 / numpy，缺了就把菜单项藏起来。"""
    return load_matte() is not None


class MatteThread(QThread):
    """后台批量抠像：把各动作文件夹里的实心背景视频转成透明 APNG。

    成功后把原视频改名为 xxx.mp4.source —— 后缀不在 MEDIA_EXTS 里，
    桌宠不会再去播放那块不透明矩形，但用户随时可以改回来。
    """

    progress = pyqtSignal(int, str)
    done = pyqtSignal(int, int)

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self._root = Path(root)
        self._stop = False

    def stop(self):
        self._stop = True

    def _jobs(self) -> list[tuple[str, Path]]:
        jobs: list[tuple[str, Path]] = []
        for action in ACTIONS:
            folder = self._root / action
            if not folder.is_dir():
                continue
            try:
                items = sorted(folder.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for p in items:
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    jobs.append((action, p))
        return jobs

    @staticmethod
    def _target(action: str, src: Path) -> Path:
        """输出名沿用 idle_01.apng 这种格式，已存在就往后顺延。"""
        used = set()
        try:
            used = {p.name.lower() for p in src.parent.iterdir()}
        except OSError:
            pass
        n = 1
        while f"{action}_{n:02d}.apng" in used:
            n += 1
        return src.parent / f"{action}_{n:02d}.apng"

    def run(self):
        jobs = self._jobs()
        if not jobs:
            self.done.emit(0, 0)
            return

        matte = load_matte()
        if matte is None:
            self.progress.emit(0, "缺少抠像依赖（需要 opencv-python 和 numpy）")
            self.done.emit(0, 0)
            return

        ok = 0
        for idx, (action, src) in enumerate(jobs):
            if self._stop:
                break
            base = int(idx * 100 / len(jobs))
            span = max(1, int(100 / len(jobs)))

            def report(pct: int, msg: str, _b=base, _s=span, _n=src.name):
                self.progress.emit(min(99, _b + int(pct * _s / 100)), f"{_n} · {msg}")

            result = matte.auto_matte(
                src, self._target(action, src),
                progress=report, should_stop=lambda: self._stop,
            )
            if not result.ok:
                if self._stop:
                    break
                self.progress.emit(min(99, base + span), f"{src.name} 抠像失败：{result.message}")
                continue

            ok += 1
            try:
                src.rename(src.with_name(src.name + ".source"))
            except OSError:
                pass
            self.progress.emit(min(99, base + span), f"完成 {src.name} → {Path(result.output).name}")
        self.done.emit(ok, len(jobs))


class PetWindow(QWidget):
    """透明、无边框、置顶的小窗口：可拖动、缩放，右键切动作。"""

    SIZES = (120, 160, 220, 300)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("桌宠")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self._settings = QSettings("Gangge", "GanggeCode")
        self._assets = PetAssets()
        self._thread: FrameThread | None = None
        self._pixmap: QPixmap | None = None
        self._aspect = 1.0          # 素材宽/高，用来把窗口调成合适的比例
        self._state = ""
        self._file_idx = 0
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None
        self._notice = ""
        self._matte_thread: MatteThread | None = None
        self._matte_note = ""       # 抠像进度提示，画在画面底部

        size = int(self._settings.value("pet/size", 220) or 220)
        self.resize(size, size)

        self._revert_timer = QTimer(self)
        self._revert_timer.setSingleShot(True)
        self._revert_timer.timeout.connect(lambda: self.set_state("idle", force=True))

        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._maybe_sleep)

        self._restore_pos()
        self.set_state("idle", force=True)

    # ── 位置 ──
    def _restore_pos(self):
        pos = self._settings.value("pet/pos", None)
        if pos is not None:
            try:
                self.move(pos)
                return
            except Exception:
                pass
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 60, geo.bottom() - self.height() - 80)

    def _save_pos(self):
        self._settings.setValue("pet/pos", self.pos())

    # ── 状态机 ──
    def set_state(self, state: str, force: bool = False):
        """切换动作：thinking / working / success / error / idle / sleep。"""
        state = state if state in ACTIONS else "idle"
        if not force and state == self._state:
            return
        self._state = state
        self._file_idx = 0
        self._revert_timer.stop()
        self._sleep_timer.stop()
        self._start_current()

        if state in ("success", "error"):
            self._revert_timer.start(5000)      # 播完 5 秒回空闲
        elif state in ("thinking", "working"):
            self._sleep_timer.start(300_000)    # 5 分钟没有新动作就睡觉
        elif state == "idle":
            self._sleep_timer.start(180_000)    # 空闲 3 分钟睡觉

    def _maybe_sleep(self):
        if self._state in ("idle", "thinking", "working"):
            self.set_state("sleep", force=True)

    def _pick_file(self, state: str) -> Path | None:
        files = self._assets.files(state)
        if not files and state != "idle":
            files = self._assets.files("idle")   # 缺动作时用空闲兜底
        if not files:
            return None
        return files[self._file_idx % len(files)]

    def _start_current(self):
        self._stop_thread()
        if not self.isVisible():
            # 窗口没显示就别解码，等 showEvent 再启动
            self._pixmap = None
            return
        path = self._pick_file(self._state)
        if path is None:
            self._pixmap = None
            self._notice = "把视频放进素材文件夹\n右键 → 打开素材文件夹"
            self.update()
            return

        self._notice = ""
        loop = self._state not in ("success", "error")
        self._thread = FrameThread(path, loop=loop, parent=self)
        self._thread.frame.connect(self._on_frame)
        self._thread.failed.connect(self._on_decode_error)
        self._thread.start()

    def _stop_thread(self):
        if self._thread is not None:
            self._thread.stop()
            self._thread.wait(1500)
            self._thread = None

    def _on_frame(self, img: QImage):
        self._pixmap = QPixmap.fromImage(img)
        # 按素材宽高比调整窗口，别让角色被挤成很小一块
        if img.height() > 0:
            aspect = img.width() / img.height()
            if abs(aspect - self._aspect) > 0.01:
                self._aspect = aspect
                self.resize(max(48, int(round(self.height() * aspect))), self.height())
        self.update()
        if self._thread is not None:
            self._thread.ack()      # 告诉解码线程可以取下一帧了

    def _on_decode_error(self, message: str):
        self._pixmap = None
        self._notice = f"素材读取失败\n{message[:60]}"
        self.update()

    # ── 绘制 ──
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            self._draw_matte_note(painter)
            return

        # 没素材时的占位（一条简笔鱼）+ 提示
        w, h = self.width(), self.height()
        body = QRect(int(w * 0.18), int(h * 0.34), int(w * 0.5), int(h * 0.3))
        painter.setPen(QPen(QColor("#58a6ff"), 2))
        painter.setBrush(QColor(88, 166, 255, 40))
        painter.drawEllipse(body)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(88, 166, 255, 60))
        tail_x, tail_y = body.right() - 4, body.center().y()
        painter.drawPolygon(QPolygon([
            QPoint(tail_x, tail_y),
            QPoint(tail_x + int(w * 0.12), tail_y - int(h * 0.07)),
            QPoint(tail_x + int(w * 0.12), tail_y + int(h * 0.07)),
        ]))
        painter.setBrush(QColor("#f0f6fc"))
        eye = max(3, int(w * 0.02))
        painter.drawEllipse(QPoint(body.left() + int(w * 0.1), body.center().y() - int(h * 0.04)), eye, eye)

        painter.setPen(QColor("#8b949e"))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        flags = (
            int(Qt.AlignmentFlag.AlignHCenter)
            | int(Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap)
        )
        painter.drawText(QRect(0, int(h * 0.68), w, int(h * 0.30)), flags, self._notice)
        self._draw_matte_note(painter)

    def _draw_matte_note(self, painter: QPainter):
        """抠像进度：半透明黑底 + 一行小字，压在画面底部。"""
        if not self._matte_note:
            return
        w, h = self.width(), self.height()
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        box = QRect(4, h - max(26, int(h * 0.24)), w - 8, max(26, int(h * 0.24)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(13, 17, 23, 205))
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QColor("#c9d1d9"))
        flags = (
            int(Qt.AlignmentFlag.AlignHCenter)
            | int(Qt.AlignmentFlag.AlignVCenter)
            | int(Qt.TextFlag.TextWordWrap)
        )
        painter.drawText(box, flags, self._matte_note)

    # ── 交互 ──
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            release = event.globalPosition().toPoint()
            moved = self._press_pos is not None and (release - self._press_pos).manhattanLength() > 4
            self._drag_offset = None
            self._press_pos = None
            self._save_pos()
            if not moved:
                self._poke()
            event.accept()

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            sizes = self.SIZES
            idx = min(range(len(sizes)), key=lambda i: abs(sizes[i] - self.width()))
            idx = max(0, min(len(sizes) - 1, idx + (-1 if event.angleDelta().y() > 0 else 1)))
            self._resize_to(sizes[idx])
            event.accept()

    def _poke(self):
        """点一下：换个动作卖个萌。"""
        if self._thread is not None:
            self._file_idx += 1        # 同一动作里轮换下一个素材
            self._start_current()
            return
        for candidate in ("success", "working", "idle"):
            if self._assets.files(candidate):
                self.set_state(candidate, force=True)
                return
        self._notice = "还没有素材\n右键 → 打开素材文件夹"
        self.update()

    def _resize_to(self, size: int):
        """size 指高度，宽度按素材比例算。"""
        self.resize(max(48, int(round(size * self._aspect))), size)
        self._settings.setValue("pet/size", size)
        self.update()

    def contextMenuEvent(self, event):  # noqa: N802
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:4px;}"
            "QMenu::item{padding:5px 18px;border-radius:3px;}"
            "QMenu::item:selected{background:#1f6feb;color:#fff;}"
        )
        for action in ACTIONS:
            act = menu.addAction(f"{ACTION_LABELS[action]}（{self._assets.count(action)} 个素材）")
            act.setCheckable(True)
            act.setChecked(self._state == action)
            act.triggered.connect(lambda _checked=False, a=action: self.set_state(a, force=True))
        menu.addSeparator()
        menu.addAction("📂 打开素材文件夹（删表情就在这里删）", self._open_assets)
        menu.addAction("♻️ 恢复内置表情", self._restore_bundled)
        if matte_available():
            if self._matte_thread is None:
                act = menu.addAction("✂️ 自动抠像（去掉纯色背景）", self._start_matte)
                act.setEnabled(self._has_pending_video())
            else:
                menu.addAction("⏹ 停止抠像", self._stop_matte)
        size_menu = menu.addMenu("大小")
        for size in self.SIZES:
            act = size_menu.addAction(f"{size} px")
            act.setCheckable(True)
            act.setChecked(self.width() == size)
            act.triggered.connect(lambda _checked=False, s=size: self._resize_to(s))
        menu.addAction("❌ 关闭桌宠", self.hide)
        menu.exec(event.globalPos())

    def _open_assets(self):
        try:
            self._assets.ensure()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._assets.root)))
        except Exception:
            pass

    def _restore_bundled(self):
        """把仓库内置的萌宠表情补回素材目录（只补缺失的文件）。"""
        copied = self._assets.seed(force=True)
        self._file_idx = 0
        self._start_current()
        self._flash(f"已恢复 {copied} 个内置表情" if copied else "内置表情都在，没有需要恢复的")

    def _flash(self, text: str, ms: int = 4000):
        """在画面底部闪一条提示。"""
        self._matte_note = text
        self.update()
        QTimer.singleShot(ms, self._clear_matte_note)

    # ── 自动抠像 ──────────────────────────────────────────────
    def _has_pending_video(self) -> bool:
        for action in ACTIONS:
            for p in self._assets.files(action):
                if p.suffix.lower() in VIDEO_EXTS:
                    return True
        return False

    def _start_matte(self):
        if self._matte_thread is not None:
            return
        self._stop_thread()          # 解码线程别再抢同一个文件
        self._matte_note = "准备抠像…"
        self.update()
        self._matte_thread = MatteThread(self._assets.root, parent=self)
        self._matte_thread.progress.connect(self._on_matte_progress)
        self._matte_thread.done.connect(self._on_matte_finished)
        self._matte_thread.start()

    def _stop_matte(self):
        if self._matte_thread is not None:
            self._matte_thread.stop()
            self._matte_note = "正在停止…"
            self.update()

    def _on_matte_progress(self, pct: int, message: str):
        self._matte_note = f"抠像 {pct}%\n{message}"
        self.update()

    def _on_matte_finished(self, ok: int, total: int):
        thread = self._matte_thread
        self._matte_thread = None
        if thread is not None:
            thread.wait(2000)
            thread.deleteLater()
        if total == 0:
            self._matte_note = "没有需要抠像的视频"
        else:
            self._matte_note = f"抠像结束：{ok}/{total} 个成功"
        self._pixmap = None
        self._start_current()
        QTimer.singleShot(6000, self._clear_matte_note)

    def _clear_matte_note(self):
        if self._matte_thread is None:
            self._matte_note = ""
            self.update()

    # ── 显隐 ──
    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.set_state(self._state or "idle", force=True)

    def hideEvent(self, event):  # noqa: N802
        self._stop_thread()
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802
        self._save_pos()
        self._stop_thread()
        if self._matte_thread is not None:
            self._matte_thread.stop()
            self._matte_thread.wait(3000)
            self._matte_thread = None
        super().closeEvent(event)
