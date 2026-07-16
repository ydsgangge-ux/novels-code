"""Browser debug tool — Playwright-powered page verification for Agent.

不同于 browser.py（内容抓取），本工具是 Agent 的"运行时感官"：
- 打开预览页 → 读取 console 报错 → 截图 → 据此判断页面是否正常
- 支持点击/填表/滚动等交互，用于 E2E 验证闭环

安全限制：
- 仅允许 localhost / 127.0.0.1 / [::1]（任意端口），禁止访问外部 URL
- 浏览器实例空闲超时自动回收，避免僵尸进程

依赖：pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import time
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_BROWSER_AVAILABLE = False
try:
    from playwright.async_api import async_playwright
    _BROWSER_AVAILABLE = True
except ImportError:
    pass

# ── 安全：仅允许本地地址（任意端口）──
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# ── 空闲超时（秒）：超过后自动关闭浏览器 ──
_IDLE_TIMEOUT = 120

# ── console 日志最大条数（防止爆内存）──
_MAX_CONSOLE_ENTRIES = 200


def _is_localhost(url: str) -> bool:
    """检查 URL 是否指向本地（安全限制）。"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _is_unavailable() -> str:
    if not _BROWSER_AVAILABLE:
        return "Playwright 未安装。运行: pip install playwright && playwright install chromium"
    return ""


class _BrowserSession:
    """持久化浏览器会话 — 跨工具调用复用，空闲超时自动回收。

    单例设计：同一时间只有一个浏览器实例，避免多 Agent 任务抢占资源。
    """

    _instance: _BrowserSession | None = None

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None
        self._console_logs: list[dict[str, str]] = []
        self._last_activity: float = 0.0
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    @classmethod
    def get_instance(cls) -> _BrowserSession:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _touch(self) -> None:
        """更新最后活动时间。"""
        self._last_activity = time.time()

    def _on_console(self, msg) -> None:
        """console 消息回调 — 收集 log/warn/error。"""
        if len(self._console_logs) < _MAX_CONSOLE_ENTRIES:
            self._console_logs.append({
                "type": msg.type,  # log / warning / error / info
                "text": msg.text,
            })

    async def get_page(self, headless: bool = True) -> Any:
        """获取当前页面（惰性初始化浏览器）。"""
        async with self._lock:
            # 取消之前的清理任务（有新活动了）
            if self._cleanup_task and not self._cleanup_task.done():
                self._cleanup_task.cancel()
                self._cleanup_task = None

            if self._browser is None or not self._browser.is_connected():
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                logger.info("[BrowserDebug] 浏览器已启动 (headless=%s)", headless)

            if self._page is None or self._page.is_closed():
                context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 800},
                )
                self._page = await context.new_page()
                # 绑定 console 监听
                self._page.on("console", self._on_console)
                self._console_logs.clear()
                logger.info("[BrowserDebug] 新页面已创建")

            self._touch()
            return self._page

    async def close(self) -> None:
        """关闭浏览器，释放资源。"""
        async with self._lock:
            if self._page and not self._page.is_closed():
                try:
                    await self._page.close()
                except Exception:
                    pass
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._page = None
            self._browser = None
            self._playwright = None
            self._console_logs.clear()
            logger.info("[BrowserDebug] 浏览器已关闭")

    def get_console_logs(self, filter_type: str = "") -> list[dict[str, str]]:
        """获取已收集的 console 日志。filter_type='error' 只返回错误。"""
        self._touch()
        if filter_type:
            return [l for l in self._console_logs if l["type"] == filter_type]
        return list(self._console_logs)

    def clear_console_logs(self) -> None:
        """清空 console 日志（navigate 后自动调用）。"""
        self._console_logs.clear()

    def schedule_cleanup(self) -> None:
        """安排空闲超时后的自动清理。"""
        async def _cleanup_after_idle():
            try:
                await asyncio.sleep(_IDLE_TIMEOUT)
                logger.info("[BrowserDebug] 空闲超时 %ds，自动关闭浏览器", _IDLE_TIMEOUT)
                await self.close()
            except asyncio.CancelledError:
                pass

        self._cleanup_task = asyncio.create_task(_cleanup_after_idle())


class BrowserDebugTool(BaseTool):
    """浏览器调试工具 — Agent 的运行时感官。

    用途：改完前端代码后，自主打开预览页、读取 console 报错、截图验证，
    完成"改代码 → 验证 → 再改"的闭环，无需人工介入。

    ⚠️ 仅允许访问 localhost / 127.0.0.1（任意端口），禁止外部 URL。
    """

    @property
    def name(self) -> str:
        return "browser_debug"

    @property
    def description(self) -> str:
        return (
            "浏览器调试工具 — 打开本地预览页并读取运行时状态（console 日志、截图、DOM），"
            "用于验证前端代码修改后页面是否正常渲染。"
            "支持点击、填表、滚动等交互操作。"
            "支持 localhost 任意端口（如 localhost:3000、localhost:5173、localhost:8080 等）。"
            "支持 action: navigate(跳转) / screenshot(截图) / console_logs(读取日志) "
            "/ dom_snapshot(DOM快照) / click(点击) / fill(填表) / scroll(滚动) "
            "/ evaluate(执行JS) / close(关闭浏览器)。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "screenshot", "console_logs", "dom_snapshot",
                             "click", "fill", "scroll", "evaluate", "close"],
                    "description": "要执行的操作",
                },
                "url": {
                    "type": "string",
                    "description": "[navigate] 要打开的 URL，localhost 任意端口（如 http://localhost:3000）",
                },
                "selector": {
                    "type": "string",
                    "description": "[click/fill] CSS 选择器，如 '#btn' / '.content'",
                },
                "value": {
                    "type": "string",
                    "description": "[fill] 要填入的文本",
                },
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "[scroll] 滚动方向，默认 down",
                },
                "js_code": {
                    "type": "string",
                    "description": "[evaluate] 在页面上下文执行的 JS 代码（表达式或语句）",
                },
                "filter_type": {
                    "type": "string",
                    "enum": ["", "error", "warning", "log", "info"],
                    "description": "[console_logs] 只返回指定类型，留空返回全部",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "[screenshot] 是否截取整个页面（含滚动区），默认 false",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        unavailable = _is_unavailable()
        if unavailable:
            return ToolResult(output=unavailable, is_error=True)

        action = kwargs["action"]
        session = _BrowserSession.get_instance()

        try:
            if action == "navigate":
                return await self._navigate(session, kwargs.get("url", ""))
            elif action == "screenshot":
                return await self._screenshot(session, kwargs.get("full_page", False))
            elif action == "console_logs":
                return self._console_logs(session, kwargs.get("filter_type", ""))
            elif action == "dom_snapshot":
                return await self._dom_snapshot(session)
            elif action == "click":
                return await self._click(session, kwargs.get("selector", ""))
            elif action == "fill":
                return await self._fill(session, kwargs.get("selector", ""),
                                        kwargs.get("value", ""))
            elif action == "scroll":
                return await self._scroll(session, kwargs.get("direction", "down"))
            elif action == "evaluate":
                return await self._evaluate(session, kwargs.get("js_code", ""))
            elif action == "close":
                await session.close()
                return ToolResult(output="浏览器已关闭")
            else:
                return ToolResult(output=f"未知 action: {action}", is_error=True)
        finally:
            # 每次操作后安排空闲清理
            session.schedule_cleanup()

    # ── 各 action 实现 ──

    async def _navigate(self, session: _BrowserSession, url: str) -> ToolResult:
        if not url:
            return ToolResult(output="navigate 需要 url 参数", is_error=True)
        if not _is_localhost(url):
            return ToolResult(
                output=f"安全限制：仅允许访问 localhost（任意端口）。被拒绝的 URL: {url}",
                is_error=True,
            )

        page = await session.get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            # 清空旧的 console 日志（只保留本次导航后的）
            session.clear_console_logs()
            title = await page.title()
            return ToolResult(output=f"已导航到: {url}\n页面标题: {title}")
        except asyncio.TimeoutError:
            return ToolResult(output=f"页面加载超时: {url}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"导航失败: {e}", is_error=True)

    async def _screenshot(self, session: _BrowserSession, full_page: bool) -> ToolResult:
        page = await session.get_page()
        if page.url == "about:blank":
            return ToolResult(output="当前没有打开的页面，请先 navigate", is_error=True)

        # 截图保存到 workspace/.gangge/screenshots/
        workspace = os.environ.get("GANGGE_WORKSPACE", ".")
        shot_dir = Path(workspace) / ".gangge" / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot_path = shot_dir / f"debug_{int(time.time())}.png"

        try:
            await page.screenshot(path=str(shot_path), full_page=full_page)
            size_kb = shot_path.stat().st_size // 1024
            return ToolResult(
                output=(
                    f"截图已保存: {shot_path}\n"
                    f"尺寸: {size_kb} KB | 全页: {'是' if full_page else '否'}\n"
                    f"→ 可用 vision 工具分析此截图，或告知用户查看。"
                ),
                metadata={"screenshot_path": str(shot_path)},
            )
        except Exception as e:
            return ToolResult(output=f"截图失败: {e}", is_error=True)

    def _console_logs(self, session: _BrowserSession, filter_type: str) -> ToolResult:
        logs = session.get_console_logs(filter_type=filter_type)
        if not logs:
            return ToolResult(output="无 console 日志（页面可能未报错，或尚未 navigate）")

        type_icon = {"error": "❌", "warning": "⚠️", "log": "📝", "info": "ℹ️"}
        lines = [f"{type_icon.get(l['type'], '•')} [{l['type']}] {l['text']}" for l in logs]
        header = f"Console 日志 ({len(logs)} 条"
        if filter_type:
            header += f", 类型={filter_type}"
        header += "):\n"
        return ToolResult(output=header + "\n".join(lines))

    async def _dom_snapshot(self, session: _BrowserSession) -> ToolResult:
        page = await session.get_page()
        if page.url == "about:blank":
            return ToolResult(output="当前没有打开的页面，请先 navigate", is_error=True)

        try:
            # Playwright 1.60+ 用 aria_snapshot()（返回 Yaml 格式的 accessibility tree）
            snap = await page.aria_snapshot()
            if snap:
                if len(snap) > 8000:
                    snap = snap[:8000] + "\n... (已截断)"
                return ToolResult(output=f"DOM 快照 (aria snapshot):\n{snap}")

            # 退化方案：返回简化后的 body innerHTML
            html = await page.evaluate("document.body ? document.body.innerHTML : ''")
            if html:
                import re
                clean = re.sub(r"\s+", " ", html).strip()
                if len(clean) > 6000:
                    clean = clean[:6000] + "\n... (已截断)"
                return ToolResult(output=f"DOM 快照 (innerHTML 退化方案):\n{clean}")
            return ToolResult(output="无法获取 DOM 快照（页面可能为空）")
        except Exception as e:
            return ToolResult(output=f"DOM 快照失败: {e}", is_error=True)

    async def _click(self, session: _BrowserSession, selector: str) -> ToolResult:
        if not selector:
            return ToolResult(output="click 需要 selector 参数", is_error=True)
        page = await session.get_page()
        try:
            await page.click(selector, timeout=5000)
            return ToolResult(output=f"已点击: {selector}")
        except asyncio.TimeoutError:
            return ToolResult(output=f"未找到元素或点击超时: {selector}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"点击失败: {e}", is_error=True)

    async def _fill(self, session: _BrowserSession, selector: str, value: str) -> ToolResult:
        if not selector:
            return ToolResult(output="fill 需要 selector 参数", is_error=True)
        page = await session.get_page()
        try:
            await page.fill(selector, value, timeout=5000)
            return ToolResult(output=f"已填入 {selector}: {value[:50]}")
        except asyncio.TimeoutError:
            return ToolResult(output=f"未找到元素或填表超时: {selector}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"填表失败: {e}", is_error=True)

    async def _scroll(self, session: _BrowserSession, direction: str) -> ToolResult:
        page = await session.get_page()
        delta = 500 if direction == "down" else -500
        try:
            await page.mouse.wheel(0, delta)
            return ToolResult(output=f"已向{direction}滚动 {abs(delta)}px")
        except Exception as e:
            return ToolResult(output=f"滚动失败: {e}", is_error=True)

    async def _evaluate(self, session: _BrowserSession, js_code: str) -> ToolResult:
        if not js_code:
            return ToolResult(output="evaluate 需要 js_code 参数", is_error=True)
        page = await session.get_page()
        try:
            result = await page.evaluate(js_code)
            # 序列化结果
            if result is None:
                text = "null"
            elif isinstance(result, (dict, list)):
                import json
                text = json.dumps(result, ensure_ascii=False, indent=2)
            else:
                text = str(result)
            if len(text) > 4000:
                text = text[:4000] + "\n... (已截断)"
            return ToolResult(output=f"JS 执行结果:\n{text}")
        except Exception as e:
            return ToolResult(output=f"JS 执行失败: {e}", is_error=True)
