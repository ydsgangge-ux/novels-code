"""Vision tool — describe/analyse an image with a vision-capable LLM.

Two routes share one input parser:

* **dedicated** — a separate multimodal adapter (``VISION_MODEL``) is passed as
  ``multimodal_llm``; the main conversation's model is never swapped.
* **main-model** — no dedicated adapter, but the main model advertises vision
  support (``supports_vision``); the image block goes straight to it.

Keeping one parser means ``image_name`` / ``image_data`` / ``image_path`` behave
identically on both routes; only the exit point differs.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer5_llm.base import BaseLLM, ContentBlock, ContentType, Message, Role

logger = logging.getLogger(__name__)

# base64 prefixes -> MIME. Checked in order, so multi-byte prefixes come first.
_MAGIC: tuple[tuple[str, str], ...] = (
    ("/9j/", "image/jpeg"),
    ("iVBOR", "image/png"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
    ("Qk1", "image/bmp"),
    ("Qk0", "image/bmp"),
    ("SUkq", "image/tiff"),
    ("TU0A", "image/tiff"),
)

_DEFAULT_PROMPT = "详细描述这张图片的内容"

_NO_MODEL_HINT = (
    "❌ 当前没有可用的视觉模型：主模型不支持图片输入，也没有配置专用视觉模型。\n"
    "请在 .env 中设置 VISION_MODEL（例如 qwen-vl-max、glm-4v-plus、gpt-4o）后重启。"
)

_NO_VISION_HINT = (
    "❌ 主模型 {model} 不支持图片输入，且未配置专用视觉模型。\n"
    "请在 .env 中设置 VISION_MODEL（例如 qwen-vl-max、glm-4v-plus、gpt-4o）后重启。"
)


def _supports_vision(llm: Any) -> bool:
    """Read ``supports_vision`` without forcing a lazy adapter to resolve twice."""
    if llm is None:
        return False
    try:
        return bool(getattr(llm, "supports_vision", False))
    except Exception:
        return False


def _strip_data_url(data: str) -> tuple[str, str]:
    """Split an optional ``data:image/png;base64,`` prefix off ``data``.

    Returns ``(base64, media_type_from_url)``; the media type is empty when the
    caller passed bare base64.
    """
    data = data.strip()
    if data.startswith("data:") and ";base64," in data:
        header, _, payload = data.partition(";base64,")
        return payload.strip(), header[5:].strip()
    return data, ""


def _sniff_media_type(b64: str, hint: str = "") -> str:
    """Infer the MIME type from base64 magic bytes, falling back to ``hint``."""
    for prefix, mime in _MAGIC:
        if b64.startswith(prefix):
            return mime
    if hint.startswith("image/"):
        return hint
    return "image/png"


class VisionTool(BaseTool):
    """Recognise images via a multimodal model (dedicated or the main one)."""

    def __init__(
        self,
        multimodal_llm: BaseLLM | None = None,
        fallback_llm: Any | None = None,
        attachments: list[dict] | None = None,
    ):
        self._mm_llm = multimodal_llm
        self._fallback_llm = fallback_llm
        self._attachments: dict[str, dict] = {}  # filename -> {data, media_type}
        if attachments:
            self.set_attachments(attachments)

    def set_attachments(self, attachments: list[dict]) -> None:
        """Pre-populate image data from the worker's attachment list."""
        self._attachments.clear()
        for att in attachments:
            name = att.get("name", "")
            if name:
                self._attachments[name] = att

    @property
    def _target(self) -> Any:
        return self._mm_llm or self._fallback_llm

    @property
    def _route(self) -> str:
        return "dedicated" if self._mm_llm is not None else "main"

    @property
    def name(self) -> str:
        return "vision"

    @property
    def description(self) -> str:
        return (
            "识别图片内容。当用户上传了图片或任务涉及图片分析时，调用此工具。"
            "传入用户上传的图片文件名（image_name），工具会自动分析并返回文字描述。"
            "也可以传本地图片的绝对路径（image_path），例如浏览器截图。"
        )

    @property
    def tags(self) -> tuple[str, ...]:
        return ("image", "图片", "截图", "ocr", "识图", "multimodal")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_name": {
                    "type": "string",
                    "description": "用户上传的图片文件名（如 'photo.png'），工具会自动查找并分析",
                },
                "image_data": {
                    "type": "string",
                    "description": "图片的 base64 编码数据（如果无法通过文件名获取时使用）",
                },
                "image_path": {
                    "type": "string",
                    "description": "本地图片文件绝对路径（如果无法通过文件名或 base64 获取时使用）",
                },
                "prompt": {
                    "type": "string",
                    "description": "对图片的提问/指令，如「描述这张图片」「图片里有什么文字」等，默认：详细描述",
                    "default": "详细描述这张图片的内容",
                },
            },
            "anyOf": [
                {"required": ["image_name"]},
                {"required": ["image_data"]},
                {"required": ["image_path"]},
            ],
        }

    # ── input parsing ───────────────────────────────────────────

    def _read_image(self, image_name: str, image_data: str, image_path: str) -> tuple[str, str]:
        """Resolve the three input forms into ``(base64, media_type)``.

        Raises :class:`ValueError` with a model-actionable message; the caller
        turns it into an error result. Never returns empty data — a vision call
        with an empty image block is the failure mode this replaces.
        """
        media_hint = ""

        if image_name:
            att = self._attachments.get(image_name)
            if att is None:
                available = ", ".join(sorted(self._attachments)[:10]) or "无"
                raise ValueError(
                    f"未找到用户上传的图片: {image_name}\n"
                    f"当前可用附件: {available}\n"
                    f"如果图片在本地磁盘，请改用 image_path 传入绝对路径。"
                )
            image_data = str(att.get("data") or "")
            media_hint = str(att.get("media_type") or "")
            if not image_data:
                raise ValueError(f"附件 {image_name} 没有图片数据")
        elif not image_data and image_path:
            p = Path(image_path).expanduser()
            if not p.exists():
                raise ValueError(f"图片文件不存在: {image_path}")
            if not p.is_file():
                raise ValueError(f"不是文件: {image_path}")
            try:
                raw = p.read_bytes()
            except Exception as e:  # noqa: BLE001 - surfaced to the model as-is
                raise ValueError(f"读取图片失败: {e}") from e
            if not raw:
                raise ValueError(f"图片文件为空: {image_path}")
            image_data = base64.b64encode(raw).decode("ascii")
            media_hint = f"image/{p.suffix.lower().lstrip('.')}"
            if media_hint == "image/jpg":
                media_hint = "image/jpeg"

        image_data, url_media = _strip_data_url(image_data)
        if not image_data:
            raise ValueError(
                "没有拿到图片数据。请提供 image_name（用户上传的图片）、"
                "image_data（base64）或 image_path（本地绝对路径）之一。"
            )
        return image_data, _sniff_media_type(image_data, url_media or media_hint)

    # ── execution ───────────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = self._target
        if target is None:
            return ToolResult(output=_NO_MODEL_HINT, is_error=True)

        if self._mm_llm is None and not _supports_vision(target):
            model_name = getattr(target, "model", None) or "当前模型"
            return ToolResult(output=_NO_VISION_HINT.format(model=model_name), is_error=True)

        prompt = str(kwargs.get("prompt") or "").strip() or _DEFAULT_PROMPT

        try:
            image_data, media_type = self._read_image(
                str(kwargs.get("image_name") or "").strip(),
                str(kwargs.get("image_data") or ""),
                str(kwargs.get("image_path") or "").strip(),
            )
        except ValueError as e:
            return ToolResult(output=f"❌ {e}", is_error=True)

        msg = Message(role=Role.USER, content=[
            ContentBlock(type=ContentType.TEXT, text=prompt),
            ContentBlock(type=ContentType.IMAGE, media_type=media_type, media_data=image_data),
        ])

        try:
            resp = await target.chat(messages=[msg])
            text = resp.text.strip() if resp.text else "(无输出)"
            return ToolResult(
                output=text,
                metadata={"vision_route": self._route, "media_type": media_type},
            )
        except Exception as e:  # noqa: BLE001 - reported to the model
            logger.warning("[Vision] call failed (route=%s): %s", self._route, e)
            return ToolResult(
                output=(
                    f"❌ 多模态模型调用失败: {e}\n"
                    f"当前路由: {'专用视觉模型' if self._route == 'dedicated' else '主模型'}\n"
                    f"请检查多模态模型配置是否正确。"
                ),
                is_error=True,
            )
