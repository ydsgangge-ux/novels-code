"""demo_video — turn a topic (or a hand-written storyboard) into a demo video.

The tool deliberately owns very little logic: it either asks the model for a
storyboard, or takes one the caller already has, and hands it to
``layer4_tools.storyboard``. Everything visual lives there, driven by data.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer5_llm.base import ContentBlock, ContentType, Message, Role

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _draft_prompt(topic: str) -> str:
    return f"""你是短视频分镜师。把主题写成一条 20-45 秒的讲解短视频分镜，只输出 JSON。

JSON 结构：
{{"title": "视频标题", "scenes": [{{...}}, ...]}}

template 只能取以下五个值，字段含义如下：
- "title"   标题卡：title（主标题 ≤14字）、text（副标题 ≤24字）
- "bullets" 要点：  title（≤16字）、items（2-4 条，每条 ≤16字）
- "flow"    流程：  title（≤16字）、nodes（2-4 个，每个 ≤8字）
- "quote"   金句：  text（≤36字）、title（出处或作者）
- "outro"   结尾：  title（≤14字）、text（≤24字）、items（第一个元素是行动号召 ≤10字）

规则：
1. 共 4-6 个镜头。第一个必须是 title，最后一个必须是 outro，中间用 bullets / flow / quote。
2. 每个镜头必须有 voiceover：口语化讲解，25-45 个汉字，承接上一镜头的语义，念出来要通顺。
3. 不要写 duration 字段，镜头时长由配音决定。
4. 文案要具体，避免「赋能」「抓手」这类空话。
5. 只输出 JSON 本身，不要解释，不要 markdown 代码块。

主题：{topic}"""


def _loads(text: str) -> Any:
    """Parse JSON out of a model answer that may wrap it in prose or fences."""
    text = (text or "").strip()
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出里找不到合法 JSON")


def _slug(topic: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", topic).strip("-")
    return (cleaned[:16] or "demo") + "-" + time.strftime("%Y%m%d-%H%M%S")


class DemoVideoTool(BaseTool):
    """Generate a narrated demo video from a topic or a storyboard JSON."""

    def __init__(self, llm: Any = None, workspace: str = ""):
        self.llm = llm
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "demo_video"

    @property
    def description(self) -> str:
        return (
            "生成带文案、动画和 TTS 配音的演示短视频（mp4）。"
            "action: write(只写分镜) / preview(生成可在浏览器播放的 HTML) / "
            "render(渲染完整视频) / auto(写分镜并渲染，默认)。"
            "给 topic 会自动写分镜；也可以直接传 scenes 数组自行控制每一帧。"
            "渲染耗时约每 10 秒成片 30-60 秒。"
        )

    @property
    def tags(self) -> tuple[str, ...]:
        return ("video", "视频", "动画", "配音", "tts", "短视频", "demo", "宣传片")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["auto", "write", "preview", "render"],
                    "description": "默认 auto：写分镜并渲染",
                },
                "topic": {
                    "type": "string",
                    "description": "视频主题，如「三步做出演示动画」。给了它就不需要 scenes",
                },
                "scenes": {
                    "type": "array",
                    "description": "手写分镜数组，元素如 {\"template\":\"title\",\"title\":\"...\"}",
                    "items": {"type": "object"},
                },
                "storyboard_file": {
                    "type": "string",
                    "description": "已有分镜 JSON 文件路径（与 scenes 二选一）",
                },
                "output_dir": {
                    "type": "string",
                    "description": "输出目录，默认 <workspace>/.gangge/demo_videos/<主题>-<时间>",
                },
                "style": {
                    "type": "string",
                    "enum": ["dark", "neon", "paper", "glass", "brutal"],
                    "description": (
                        "视觉风格：dark 克制深色 / neon 发光科技 / paper 纸张书卷 / "
                        "glass 玻璃拟态 / brutal 撞色粗框。用户要「华丽」时用 neon 或玻璃"
                    ),
                },
                "decor": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["glow", "grid", "grain", "particles", "scanline"],
                    },
                    "description": "背景装饰层，留空则用该风格的默认组合",
                },
                "voice": {
                    "type": "string",
                    "description": "edge-tts 音色，默认 zh-CN-XiaoxiaoNeural",
                    "default": "zh-CN-XiaoxiaoNeural",
                },
                "narration": {
                    "type": "boolean",
                    "description": "是否合成配音，默认 true；关闭则按文字估算时长",
                    "default": True,
                },
                "keep_frames": {
                    "type": "boolean",
                    "description": "保留 PNG 帧序列用于排查，默认 false",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    # ── storyboard acquisition ──────────────────────────────────

    async def _draft(self, topic: str) -> dict[str, Any]:
        """Ask the model for a storyboard, retrying once with the parse error."""
        if self.llm is None:
            raise ValueError("当前没有可用的主模型，无法自动写分镜；请直接传 scenes")

        prompt = _draft_prompt(topic)
        last_error = ""
        for attempt in range(2):
            ask = prompt if attempt == 0 else (
                f"{prompt}\n\n上一次输出无法解析（{last_error}），请只输出合法 JSON。"
            )
            msg = Message(
                role=Role.USER,
                content=[ContentBlock(type=ContentType.TEXT, text=ask)],
            )
            resp = await self.llm.chat(messages=[msg])
            try:
                return _loads(resp.text)
            except ValueError as e:
                last_error = str(e)
                logger.warning("[demo_video] storyboard parse failed: %s", e)
        raise ValueError(f"模型两次都没有给出合法分镜 JSON：{last_error}")

    async def _resolve_storyboard(self, kwargs: dict[str, Any], need_draft: bool):
        from gangge.layer4_tools.storyboard import Storyboard

        if kwargs.get("scenes"):
            return Storyboard.from_dict({"scenes": kwargs["scenes"]})

        path = str(kwargs.get("storyboard_file") or "").strip()
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return Storyboard.from_dict(data)

        topic = str(kwargs.get("topic") or "").strip()
        if not topic:
            raise ValueError("需要 topic、scenes 或 storyboard_file 之一")
        if not need_draft:
            raise ValueError("preview/render 需要 scenes 或 storyboard_file")
        return Storyboard.from_dict(await self._draft(topic))

    def _out_dir(self, kwargs: dict[str, Any], topic: str) -> Path:
        explicit = str(kwargs.get("output_dir") or "").strip()
        if explicit:
            return Path(explicit)
        base = Path(self.workspace or ".") / ".gangge" / "demo_videos"
        return base / _slug(topic or "storyboard")

    # ── execution ───────────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> ToolResult:
        from gangge.layer4_tools.storyboard import (
            build_video,
            save_storyboard,
            write_preview,
        )

        action = str(kwargs.get("action") or "auto").strip().lower()
        if action not in ("auto", "write", "preview", "render"):
            return ToolResult(output=f"未知 action: {action}", is_error=True)

        topic = str(kwargs.get("topic") or "").strip()
        try:
            storyboard = await self._resolve_storyboard(
                kwargs, need_draft=action in ("auto", "write") or bool(topic)
            )
        except Exception as e:  # noqa: BLE001 - parse errors are model-facing
            return ToolResult(output=f"❌ 分镜无效：{e}", is_error=True)

        if kwargs.get("voice"):
            storyboard.voice = str(kwargs["voice"])
        if kwargs.get("style"):
            storyboard.style = str(kwargs["style"]).strip().lower()
        if kwargs.get("decor"):
            from gangge.layer4_tools.storyboard.schema import DECOR_NAMES

            storyboard.decor = [str(d) for d in kwargs["decor"] if str(d) in DECOR_NAMES]
        # Name the folder after whatever the caller actually gave us: the topic
        # if there was one, else the storyboard title, else the first shot's.
        label = topic or storyboard.title or next(
            (s.title for s in storyboard.scenes if s.title), ""
        )
        out_dir = self._out_dir(kwargs, label)

        # Persist the resolved storyboard first: it is the smallest artifact and
        # the one worth editing by hand when the wording is off.
        try:
            sb_path = save_storyboard(storyboard, out_dir)
        except OSError as e:
            return ToolResult(output=f"❌ 无法写入输出目录 {out_dir}: {e}", is_error=True)

        if action == "write":
            return ToolResult(
                output=(
                    f"✅ 分镜已生成（{len(storyboard.scenes)} 个镜头，"
                    f"预计 {storyboard.total_duration:.1f}s）\n"
                    f"- 分镜: {sb_path}\n"
                    f"下一步可用 action=render 渲染，或先改分镜里的文案。"
                ),
                metadata={"storyboard": storyboard.to_dict(), "path": str(sb_path)},
            )

        if action == "preview":
            try:
                html = write_preview(storyboard, out_dir)
            except OSError as e:
                return ToolResult(output=f"❌ 生成预览失败: {e}", is_error=True)
            return ToolResult(
                output=(
                    f"✅ 预览页面已生成\n- HTML: {html}\n"
                    f"- 预计时长: {storyboard.total_duration:.1f}s（未配音，按文字估算）\n"
                    f"用浏览器打开即可逐帧查看；确认无误后 action=render 出片。"
                ),
                metadata={"html": str(html), "storyboard": storyboard.to_dict()},
            )

        result = await build_video(
            storyboard,
            out_dir,
            narration=bool(kwargs.get("narration", True)),
            keep_frames=bool(kwargs.get("keep_frames", False)),
        )
        data = result.to_dict()

        if result.video is None:
            lines = ["⚠️ 视频未生成，但产物已保留："]
            lines += [f"- {note}" for note in result.notes]
            lines.append(f"- 分镜页: {result.html}")
            if result.audio:
                lines.append(f"- 配音: {result.audio}")
            if result.srt:
                lines.append(f"- 字幕: {result.srt}")
            if result.frames_dir:
                lines.append(f"- 帧序列: {result.frames_dir}")
            return ToolResult(output="\n".join(lines), is_error=True, metadata=data)

        lines = [
            "✅ 演示视频已生成",
            f"- 文件: {result.video}",
            f"- 时长: {result.duration:.1f}s | {result.fps}fps | {result.scenes} 个镜头",
            f"- 配音: {result.tts_engine}",
        ]
        if result.srt:
            lines.append(f"- 字幕: {result.srt}")
        lines.append(f"- 分镜页: {result.html}")
        lines += [f"- 提示: {note}" for note in result.notes]
        return ToolResult(output="\n".join(lines), metadata=data)
