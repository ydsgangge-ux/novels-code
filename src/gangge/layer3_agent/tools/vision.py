"""Vision tool — use multimodal LLM to recognize/describe images."""

from __future__ import annotations

from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer5_llm.base import BaseLLM, ContentBlock, ContentType, Message, Role


class VisionTool(BaseTool):
    """Use a multimodal LLM to recognize images.

    The tool sends the image + prompt to a configured multimodal model and returns
    the text description/analysis. This avoids switching the main LLM.
    """

    def __init__(self, multimodal_llm: BaseLLM | None = None):
        self._mm_llm = multimodal_llm
        self._attachments: dict[str, dict] = {}  # filename -> {data, media_type}

    def set_attachments(self, attachments: list[dict]) -> None:
        """Pre-populate image data from the worker's attachment list."""
        self._attachments.clear()
        for att in attachments:
            name = att.get("name", "")
            if name:
                self._attachments[name] = att

    @property
    def name(self) -> str:
        return "vision"

    @property
    def description(self) -> str:
        return (
            "识别图片内容。当用户上传了图片或任务涉及图片分析时，调用此工具。"
            "传入用户上传的图片文件名（image_name），工具会自动分析并返回文字描述。"
        )

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

    async def execute(self, **kwargs: Any) -> ToolResult:
        if not self._mm_llm:
            return ToolResult(
                output="❌ 多模态模型未配置。请在设置中启用并配置多模态模型后重试。",
                is_error=True,
            )

        image_name = kwargs.get("image_name", "") or ""
        image_data = kwargs.get("image_data", "") or ""
        image_path = kwargs.get("image_path", "") or ""
        prompt = kwargs.get("prompt", "") or "详细描述这张图片的内容"

        # Priority: image_name (lookup) > image_data (base64) > image_path (read file)
        if image_name and image_name in self._attachments:
            att = self._attachments[image_name]
            image_data = att.get("data", "") or ""

        if not image_data and image_path:
            import base64
            from pathlib import Path
            p = Path(image_path)
            if not p.exists():
                return ToolResult(output=f"❌ 图片文件不存在: {image_path}", is_error=True)
            ext = p.suffix.lower()
            media_type = f"image/{ext.lstrip('.')}"
            if ext == ".jpg":
                media_type = "image/jpeg"
            try:
                image_data = base64.b64encode(p.read_bytes()).decode("ascii")
            except Exception as e:
                return ToolResult(output=f"❌ 读取图片失败: {e}", is_error=True)
        else:
            # Determine media_type from image_data header (PNG vs JPEG)
            if image_data.startswith("/9j/"):
                media_type = "image/jpeg"
            elif image_data.startswith("iVBOR"):
                media_type = "image/png"
            elif image_data.startswith("R0lGOD"):
                media_type = "image/gif"
            elif image_data.startswith("UklGR"):
                media_type = "image/webp"
            else:
                media_type = "image/png"  # best guess

        msg = Message(role=Role.USER, content=[
            ContentBlock(type=ContentType.TEXT, text=prompt),
            ContentBlock(type=ContentType.IMAGE, media_type=media_type, media_data=image_data),
        ])

        try:
            resp = await self._mm_llm.chat(messages=[msg])
            text = resp.text.strip() if resp.text else "(无输出)"
            return ToolResult(output=text)
        except Exception as e:
            return ToolResult(
                output=f"❌ 多模态模型调用失败: {e}\n请检查多模态模型配置是否正确。",
                is_error=True,
            )