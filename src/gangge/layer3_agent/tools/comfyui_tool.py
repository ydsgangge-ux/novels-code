"""ComfyUI image generation tool.

Auto-activated when a local ComfyUI instance is detected on startup.
Uses ComfyUI's native HTTP API — no MCP server required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

COMFYUI_DEFAULT_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
DEFAULT_TIMEOUT = 10
GENERATE_TIMEOUT = 300
POLL_INTERVAL = 2


def _get_requests():
    try:
        import requests
        return requests
    except ImportError:
        return None


def is_comfyui_running(base_url: str = COMFYUI_DEFAULT_URL) -> bool:
    requests = _get_requests()
    if requests is None:
        return False
    try:
        resp = requests.get(f"{base_url}/system_stats", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def _extract_model_list(info: dict, node_type: str, param_name: str) -> list[str]:
    """Safely extract a model list from ComfyUI object_info response."""
    try:
        node = info.get(node_type, {})
        required = node.get("input", {}).get("required", {})
        param = required.get(param_name, [])
        if isinstance(param, list) and len(param) > 0:
            candidates = param[0]
            if isinstance(candidates, list):
                return [str(x) for x in candidates]
        return []
    except Exception:
        return []


def get_available_models(base_url: str = COMFYUI_DEFAULT_URL) -> dict[str, list[str]]:
    requests = _get_requests()
    if requests is None:
        return {"checkpoints": [], "loras": [], "vaes": [], "error": "requests not installed"}
    try:
        resp = requests.get(f"{base_url}/object_info", timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        info = resp.json()

        return {
            "checkpoints": _extract_model_list(info, "CheckpointLoaderSimple", "ckpt_name"),
            "loras": _extract_model_list(info, "LoraLoader", "lora_name"),
            "vaes": _extract_model_list(info, "VAELoader", "vae_name"),
        }
    except Exception as e:
        return {"checkpoints": [], "loras": [], "vaes": [], "error": str(e)}


def _submit_workflow(workflow: dict, base_url: str) -> tuple[str | None, str]:
    """Submit workflow to ComfyUI.

    Returns (prompt_id, error_message).
    On success: (prompt_id, "")
    On failure: (None, detailed_error_message)
    """
    requests = _get_requests()
    if requests is None:
        return None, "requests library not installed"
    client_id = str(uuid.uuid4())
    payload = {"prompt": workflow, "client_id": client_id}
    try:
        resp = requests.post(f"{base_url}/prompt", json=payload, timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            error_detail = ""
            try:
                error_body = resp.json()
                top_error = error_body.get("error", {})
                if isinstance(top_error, dict):
                    error_detail = (
                        f"type={top_error.get('type','')} "
                        f"message={top_error.get('message','')}"
                    )
                elif isinstance(top_error, str):
                    error_detail = top_error
                node_errors = error_body.get("node_errors", {})
                if node_errors:
                    ne_parts = []
                    for nid, nerr in node_errors.items():
                        ne_parts.append(f"node_{nid}: {str(nerr)[:200]}")
                    error_detail += f" | node_errors: {'; '.join(ne_parts)}"
                if not error_detail:
                    error_detail = str(error_body)[:500]
            except Exception:
                error_detail = resp.text[:500]
            return None, f"ComfyUI returned HTTP {resp.status_code}: {error_detail}"
        data = resp.json()
        if "error" in data:
            return None, f"ComfyUI workflow error: {data['error']}"
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return None, f"ComfyUI returned no prompt_id: {str(data)[:300]}"
        return prompt_id, ""
    except requests.exceptions.ConnectionError:
        return None, f"Cannot connect to ComfyUI at {base_url}. Is it running?"
    except requests.exceptions.Timeout:
        return None, f"ComfyUI request timed out ({DEFAULT_TIMEOUT}s)"
    except Exception as e:
        return None, f"Submit failed: {type(e).__name__}: {e}"


def _wait_for_result(prompt_id: str, base_url: str, timeout: int = GENERATE_TIMEOUT) -> tuple[dict | None, str]:
    """Poll ComfyUI for generation result.

    Returns (result_dict, error_message).
    On success: ({"prompt_id": ..., "images": ...}, "")
    On failure: (None, error_description)
    """
    requests = _get_requests()
    if requests is None:
        return None, "requests library not installed"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{base_url}/history/{prompt_id}", timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                task = history[prompt_id]
                status = task.get("status", {})
                if status.get("status_str") == "error":
                    msgs = status.get("messages", [])
                    return None, f"ComfyUI execution error: {str(msgs)[:500]}"
                outputs = task.get("outputs", {})
                for node_id, node_output in outputs.items():
                    images = node_output.get("images", [])
                    if images:
                        return {"prompt_id": prompt_id, "images": images, "node_id": node_id}, ""
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    return None, f"Generation timed out ({timeout}s). prompt_id: {prompt_id}"


def _download_image(filename: str, subfolder: str, save_path: Path, base_url: str) -> bool:
    requests = _get_requests()
    if requests is None:
        return False
    params: dict[str, str] = {"filename": filename, "type": "output"}
    if subfolder:
        params["subfolder"] = subfolder
    try:
        resp = requests.get(f"{base_url}/view", params=params, timeout=60)
        resp.raise_for_status()
        save_path.write_bytes(resp.content)
        return True
    except Exception:
        return False


class ComfyUITool(BaseTool):
    """Generate images via local ComfyUI instance."""

    def __init__(self, workspace: str = "", base_url: str = ""):
        self.workspace = workspace
        self.base_url = base_url or COMFYUI_DEFAULT_URL
        self._models_cache: dict | None = None

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def description(self) -> str:
        return (
            "Generate images using local ComfyUI. "
            "Supports specifying style, size, and model. "
            "Images are saved to the project's images/ directory. "
            "Use when user says 'draw', 'generate image', 'paint', etc."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Image description (positive prompt). English works best. "
                        "e.g. 'cyberpunk city at night, neon lights, rain, ultra detailed, 8k'"
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "What to avoid in the image",
                    "default": "blurry, low quality, distorted, ugly, bad anatomy",
                },
                "style": {
                    "type": "string",
                    "description": "Image style",
                    "enum": ["realistic", "anime", "painting", "pixel", "sketch", "default"],
                    "default": "default",
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels (512/768/1024)",
                    "default": 512,
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels (512/768/1024)",
                    "default": 512,
                },
                "filename": {
                    "type": "string",
                    "description": "Output filename (without path), auto-generated if empty",
                    "default": "",
                },
                "steps": {
                    "type": "integer",
                    "description": "Sampling steps (20-40 recommended)",
                    "default": 25,
                },
                "cfg_scale": {
                    "type": "number",
                    "description": "Prompt adherence (6.0-8.0 recommended)",
                    "default": 7.0,
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs["prompt"]
        negative_prompt = kwargs.get("negative_prompt", "blurry, low quality, distorted, ugly, bad anatomy")
        style = kwargs.get("style", "default")
        width = kwargs.get("width", 512)
        height = kwargs.get("height", 512)
        filename = kwargs.get("filename", "")
        steps = kwargs.get("steps", 25)
        cfg_scale = kwargs.get("cfg_scale", 7.0)

        if _get_requests() is None:
            return ToolResult(
                output="requests library not installed. Run: pip install requests",
                is_error=True,
            )

        loop = asyncio.get_running_loop()

        models = await loop.run_in_executor(None, self._get_models)
        if not models["checkpoints"]:
            error_hint = models.get("error", "")
            msg = "No checkpoints found in ComfyUI.\nPlease download a model into ComfyUI/models/checkpoints/"
            if error_hint:
                msg += f"\nDebug info: {error_hint}"
            return ToolResult(output=msg, is_error=True)

        checkpoint = self._select_model(models["checkpoints"], style)

        is_xl = "xl" in checkpoint.lower()
        if is_xl:
            width = max(width, 1024)
            height = max(height, 1024)

        from gangge.layer3_agent.tools.comfyui_workflows import build_txt2img_workflow
        workflow = build_txt2img_workflow(
            checkpoint=checkpoint,
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            cfg_scale=cfg_scale,
        )

        prompt_id, submit_error = await loop.run_in_executor(
            None, _submit_workflow, workflow, self.base_url
        )
        if not prompt_id:
            return ToolResult(
                output=(
                    f"Workflow submission failed\n"
                    f"Error: {submit_error}\n"
                    f"Model: {checkpoint}, Size: {width}x{height}\n"
                    f"Tip: Use bash to call ComfyUI API directly for debugging:\n"
                    f"  Invoke-RestMethod http://127.0.0.1:8188/object_info | ConvertTo-Json -Depth 1"
                ),
                is_error=True,
            )

        result, wait_error = await loop.run_in_executor(None, _wait_for_result, prompt_id, self.base_url)
        if not result:
            return ToolResult(
                output=f"Image generation failed\nError: {wait_error}",
                is_error=True,
            )

        output_dir = Path(self.workspace) / "images"
        output_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            filename = f"image_{int(time.time())}.png"
        elif not filename.endswith(".png"):
            filename += ".png"

        save_path = output_dir / filename
        images = result.get("images", [])

        if not images:
            return ToolResult(output="Generation completed but no output images found", is_error=True)

        img_info = images[0]
        success = await loop.run_in_executor(
            None,
            _download_image,
            img_info["filename"],
            img_info.get("subfolder", ""),
            save_path,
            self.base_url,
        )

        if success:
            return ToolResult(
                output=(
                    f"Image generated and saved\n"
                    f"Path: images/{filename}\n"
                    f"Size: {width}x{height}\n"
                    f"Model: {checkpoint}\n"
                    f"Prompt: {prompt[:100]}..."
                ),
            )
        else:
            return ToolResult(
                output=f"Image download failed, but it exists in ComfyUI output (prompt_id: {prompt_id})",
                is_error=True,
            )

    def _get_models(self) -> dict:
        if self._models_cache is None:
            self._models_cache = get_available_models(self.base_url)
        return self._models_cache

    def _select_model(self, checkpoints: list[str], style: str) -> str:
        style_keywords = {
            "realistic": ["realistic", "realism", "photo", "real", "vision"],
            "anime": ["anime", "animagine", "anything", "counterfeit", "waifu"],
            "painting": ["painting", "artistic", "art", "dream", "mix"],
            "pixel": ["pixel", "pixelart"],
            "sketch": ["sketch", "lineart", "line"],
        }
        keywords = style_keywords.get(style, [])
        if keywords:
            for ckpt in checkpoints:
                if any(kw in ckpt.lower() for kw in keywords):
                    return ckpt
        return checkpoints[0]
