"""ComfyUI image generation tool — multi-action version.

Actions:
  - generate      生成图片（支持完整 ComfyUI 参数）
  - list_params   列出 ComfyUI 实际可用的 checkpoints/loras/vaes/samplers/schedulers
  - list_history  列出本次及历史会话用过的 prompts
  - get_history   获取某条历史的完整参数

Auto-activated when a local ComfyUI instance is detected on startup.
Uses ComfyUI's native HTTP API — no MCP server required.

历史记录持久化到 workspace/.gangge/comfyui_history.json，跨会话可查。
"""

from __future__ import annotations

import asyncio
import json
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
HISTORY_FILE = ".gangge/comfyui_history.json"
HISTORY_MAX = 50  # 最多保留 50 条历史


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


def _extract_optional_list(info: dict, node_type: str, param_name: str) -> list[str]:
    """Extract from optional inputs (e.g. sampler_name in KSampler)."""
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

        result = {
            "checkpoints": _extract_model_list(info, "CheckpointLoaderSimple", "ckpt_name"),
            "loras": _extract_model_list(info, "LoraLoader", "lora_name"),
            "vaes": _extract_model_list(info, "VAELoader", "vae_name"),
            "samplers": _extract_optional_list(info, "KSampler", "sampler_name"),
            "schedulers": _extract_optional_list(info, "KSampler", "scheduler"),
        }
        return result
    except Exception as e:
        return {"checkpoints": [], "loras": [], "vaes": [], "samplers": [], "schedulers": [], "error": str(e)}


def _submit_workflow(workflow: dict, base_url: str) -> tuple[str | None, str]:
    """Submit workflow to ComfyUI."""
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
    """Poll ComfyUI for generation result."""
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
    """Generate images via local ComfyUI — multi-action tool.

    Actions:
      generate      生成图片（支持完整 ComfyUI 参数）
      list_params   列出 ComfyUI 实际可用的 checkpoints/loras/vaes/samplers/schedulers
      list_history  列出本次及历史会话用过的 prompts
      get_history   获取某条历史的完整参数
    """

    def __init__(self, workspace: str = "", base_url: str = ""):
        self.workspace = workspace or "."
        self.base_url = base_url or COMFYUI_DEFAULT_URL
        self._models_cache: dict | None = None
        self._history_path = Path(self.workspace) / HISTORY_FILE

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def description(self) -> str:
        return (
            "Generate images via local ComfyUI. Multi-action tool.\n"
            "Actions:\n"
            "  generate     — 生成图片。支持 prompt/negative_prompt/size/steps/cfg/sampler/scheduler/lora 等\n"
            "  list_params  — 列出 ComfyUI 实际可用的 checkpoints/loras/vaes/samplers/schedulers\n"
            "  list_history — 列出之前用过的 prompts（跨会话保留）\n"
            "  get_history  — 获取某条历史的完整参数（用于复用）\n"
            "\n"
            "⚠️ 生成前建议先 list_params 看看实际可用的模型和参数。\n"
            "⚠️ 用户说'用前面的提示词'时，用 list_history 查历史，再用 get_history 拿完整参数复用。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["generate", "list_params", "list_history", "get_history"],
                    "description": "Action to perform",
                    "default": "generate",
                },
                # ── generate 参数 ──
                "prompt": {
                    "type": "string",
                    "description": (
                        "[generate] Image description (positive prompt). English works best. "
                        "e.g. 'cyberpunk city at night, neon lights, rain, ultra detailed, 8k'"
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "[generate] What to avoid in the image",
                    "default": "blurry, low quality, distorted, ugly, bad anatomy",
                },
                "style": {
                    "type": "string",
                    "description": "[generate] Image style (用于自动选择 checkpoint)",
                    "enum": ["realistic", "anime", "painting", "pixel", "sketch", "default"],
                    "default": "default",
                },
                "checkpoint": {
                    "type": "string",
                    "description": "[generate] 指定 checkpoint 名称（先用 list_params 查可用列表）。留空自动选择",
                    "default": "",
                },
                "lora": {
                    "type": "string",
                    "description": "[generate] LORA 名称（先用 list_params 查可用列表）。留空不加载",
                    "default": "",
                },
                "lora_strength": {
                    "type": "number",
                    "description": "[generate] LORA 强度 (0.0-1.5)",
                    "default": 0.8,
                },
                "width": {
                    "type": "integer",
                    "description": "[generate] Image width (512/768/1024)",
                    "default": 512,
                },
                "height": {
                    "type": "integer",
                    "description": "[generate] Image height (512/768/1024)",
                    "default": 512,
                },
                "steps": {
                    "type": "integer",
                    "description": "[generate] Sampling steps (20-40 recommended)",
                    "default": 25,
                },
                "cfg_scale": {
                    "type": "number",
                    "description": "[generate] Prompt adherence (6.0-8.0 recommended)",
                    "default": 7.0,
                },
                "sampler": {
                    "type": "string",
                    "description": "[generate] Sampler 名称（先用 list_params 查可用列表）",
                    "default": "dpmpp_2m",
                },
                "scheduler": {
                    "type": "string",
                    "description": "[generate] Scheduler 名称（先用 list_params 查可用列表）",
                    "default": "karras",
                },
                "seed": {
                    "type": "integer",
                    "description": "[generate] 随机种子。-1 = 随机，其他值 = 可复现",
                    "default": -1,
                },
                "filename": {
                    "type": "string",
                    "description": "[generate] 输出文件名（不含路径），留空自动生成",
                    "default": "",
                },
                # ── get_history 参数 ──
                "history_id": {
                    "type": "string",
                    "description": "[get_history] 历史记录 ID（先用 list_history 查列表）",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "generate")

        if action == "list_params":
            return await self._action_list_params()
        elif action == "list_history":
            return await self._action_list_history()
        elif action == "get_history":
            return await self._action_get_history(kwargs.get("history_id", ""))
        elif action == "generate":
            return await self._action_generate(**kwargs)
        else:
            return ToolResult(
                output=f"未知 action: {action}\n支持: generate / list_params / list_history / get_history",
                is_error=True,
            )

    # ──────────────────────────────────────────────
    # Action: list_params — 列出 ComfyUI 实际可用参数
    # ──────────────────────────────────────────────
    async def _action_list_params(self) -> ToolResult:
        loop = asyncio.get_running_loop()
        models = await loop.run_in_executor(None, self._get_models)

        if models.get("error"):
            return ToolResult(
                output=f"无法获取 ComfyUI 参数: {models['error']}\n请确认 ComfyUI 正在运行: {self.base_url}",
                is_error=True,
            )

        lines = ["ComfyUI 实际可用参数:\n"]

        if models["checkpoints"]:
            lines.append("【Checkpoints 模型】")
            for i, ckpt in enumerate(models["checkpoints"], 1):
                lines.append(f"  {i}. {ckpt}")
            lines.append("")

        if models["loras"]:
            lines.append("【LoRAs】")
            for i, lora in enumerate(models["loras"], 1):
                lines.append(f"  {i}. {lora}")
            lines.append("")
        else:
            lines.append("【LoRAs】(无)\n")

        if models["vaes"]:
            lines.append("【VAEs】")
            for i, vae in enumerate(models["vaes"], 1):
                lines.append(f"  {i}. {vae}")
            lines.append("")
        else:
            lines.append("【VAEs】(无，使用 checkpoint 内置 VAE)\n")

        if models["samplers"]:
            lines.append("【Samplers 采样器】")
            lines.append(f"  {', '.join(models['samplers'])}")
            lines.append("")

        if models["schedulers"]:
            lines.append("【Schedulers 调度器】")
            lines.append(f"  {', '.join(models['schedulers'])}")
            lines.append("")

        lines.append("提示: generate 时可指定 checkpoint/lora/sampler/scheduler 参数")
        return ToolResult(output="\n".join(lines))

    # ──────────────────────────────────────────────
    # Action: list_history — 列出历史 prompts
    # ──────────────────────────────────────────────
    def _load_history(self) -> list[dict]:
        if not self._history_path.exists():
            return []
        try:
            return json.loads(self._history_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_history(self, history: list[dict]) -> None:
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            # 只保留最近 HISTORY_MAX 条
            history = history[-HISTORY_MAX:]
            self._history_path.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存 ComfyUI 历史失败: {e}")

    async def _action_list_history(self) -> ToolResult:
        history = self._load_history()
        if not history:
            return ToolResult(output="暂无历史记录。生成图片后会自动记录。")

        lines = [f"历史 prompts ({len(history)} 条，最近在前):\n"]
        # 倒序显示，最近在前
        for i, entry in enumerate(reversed(history), 1):
            ts = entry.get("timestamp", "")
            prompt = entry.get("prompt", "")[:80]
            checkpoint = entry.get("checkpoint", "")
            size = f"{entry.get('width', '?')}x{entry.get('height', '?')}"
            img_path = entry.get("saved_path", "")
            hid = entry.get("id", "")
            lines.append(f"{i}. [{ts}] ID={hid}")
            lines.append(f"   Prompt: {prompt}{'...' if len(entry.get('prompt', '')) > 80 else ''}")
            lines.append(f"   Model: {checkpoint} | Size: {size}")
            if img_path:
                lines.append(f"   Image: {img_path}")
            lines.append("")

        lines.append("提示: 用 get_history + history_id 获取完整参数以复用")
        return ToolResult(output="\n".join(lines))

    # ──────────────────────────────────────────────
    # Action: get_history — 获取某条历史的完整参数
    # ──────────────────────────────────────────────
    async def _action_get_history(self, history_id: str) -> ToolResult:
        if not history_id:
            return ToolResult(output="需要 history_id 参数（先用 list_history 查列表）", is_error=True)

        history = self._load_history()
        entry = next((e for e in history if e.get("id") == history_id), None)

        if not entry:
            return ToolResult(
                output=f"未找到 ID={history_id} 的历史记录",
                is_error=True,
            )

        lines = [f"历史记录详情 (ID={history_id}):\n"]
        lines.append(f"时间: {entry.get('timestamp', '')}")
        lines.append(f"Prompt: {entry.get('prompt', '')}")
        lines.append(f"Negative: {entry.get('negative_prompt', '')}")
        lines.append(f"Checkpoint: {entry.get('checkpoint', '')}")
        lines.append(f"Size: {entry.get('width', '?')}x{entry.get('height', '?')}")
        lines.append(f"Steps: {entry.get('steps', '?')} | CFG: {entry.get('cfg_scale', '?')}")
        lines.append(f"Sampler: {entry.get('sampler', '?')} | Scheduler: {entry.get('scheduler', '?')}")
        if entry.get("lora"):
            lines.append(f"LoRA: {entry.get('lora')} (strength={entry.get('lora_strength', '?')})")
        lines.append(f"Seed: {entry.get('seed', '?')}")
        if entry.get("saved_path"):
            lines.append(f"输出图片: {entry.get('saved_path')}")

        lines.append("\n复用方式: 用这些参数再次调用 generate，或修改部分参数生成变体")
        return ToolResult(output="\n".join(lines))

    # ──────────────────────────────────────────────
    # Action: generate — 生成图片
    # ──────────────────────────────────────────────
    async def _action_generate(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs.get("prompt", "").strip()
        if not prompt:
            return ToolResult(output="[generate] 缺少 prompt 参数", is_error=True)

        negative_prompt = kwargs.get("negative_prompt", "blurry, low quality, distorted, ugly, bad anatomy")
        style = kwargs.get("style", "default")
        checkpoint = kwargs.get("checkpoint", "").strip()
        lora = kwargs.get("lora", "").strip()
        lora_strength = float(kwargs.get("lora_strength", 0.8))
        width = int(kwargs.get("width", 512))
        height = int(kwargs.get("height", 512))
        steps = int(kwargs.get("steps", 25))
        cfg_scale = float(kwargs.get("cfg_scale", 7.0))
        sampler = kwargs.get("sampler", "dpmpp_2m") or "dpmpp_2m"
        scheduler = kwargs.get("scheduler", "karras") or "karras"
        seed = int(kwargs.get("seed", -1))
        filename = kwargs.get("filename", "").strip()

        if _get_requests() is None:
            return ToolResult(
                output="requests library not installed. Run: pip install requests",
                is_error=True,
            )

        loop = asyncio.get_running_loop()

        # 获取 ComfyUI 实际可用参数
        models = await loop.run_in_executor(None, self._get_models)
        if not models["checkpoints"]:
            error_hint = models.get("error", "")
            msg = "No checkpoints found in ComfyUI.\nPlease download a model into ComfyUI/models/checkpoints/"
            if error_hint:
                msg += f"\nDebug info: {error_hint}"
            return ToolResult(output=msg, is_error=True)

        # 选择 checkpoint
        if not checkpoint:
            checkpoint = self._select_model(models["checkpoints"], style)
        elif checkpoint not in models["checkpoints"]:
            # 模糊匹配
            matches = [c for c in models["checkpoints"] if checkpoint.lower() in c.lower()]
            if matches:
                checkpoint = matches[0]
            else:
                return ToolResult(
                    output=f"Checkpoint '{checkpoint}' 不存在。\n可用: {', '.join(models['checkpoints'][:5])}...",
                    is_error=True,
                )

        # 校验 lora
        if lora and models.get("loras"):
            if lora not in models["loras"]:
                matches = [l for l in models["loras"] if lora.lower() in l.lower()]
                if matches:
                    lora = matches[0]
                else:
                    return ToolResult(
                        output=f"LoRA '{lora}' 不存在。\n可用: {', '.join(models['loras'][:5])}...",
                        is_error=True,
                    )

        # 校验 sampler
        if models.get("samplers") and sampler not in models["samplers"]:
            logger.warning(f"Sampler '{sampler}' 不在可用列表中，仍尝试提交")

        # XL 模型自动调整尺寸
        is_xl = "xl" in checkpoint.lower()
        if is_xl:
            width = max(width, 1024)
            height = max(height, 1024)

        # 构建 workflow
        from gangge.layer3_agent.tools.comfyui_workflows import build_txt2img_workflow, build_txt2img_with_lora
        if lora:
            workflow = build_txt2img_with_lora(
                checkpoint=checkpoint,
                lora_name=lora,
                lora_strength=lora_strength,
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                cfg_scale=cfg_scale,
                sampler=sampler,
                scheduler=scheduler,
                seed=seed,
            )
        else:
            workflow = build_txt2img_workflow(
                checkpoint=checkpoint,
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                cfg_scale=cfg_scale,
                sampler=sampler,
                scheduler=scheduler,
                seed=seed,
            )

        # 提交
        prompt_id, submit_error = await loop.run_in_executor(
            None, _submit_workflow, workflow, self.base_url
        )
        if not prompt_id:
            return ToolResult(
                output=(
                    f"Workflow submission failed\n"
                    f"Error: {submit_error}\n"
                    f"Model: {checkpoint}, Size: {width}x{height}\n"
                    f"Tip: Use list_params to check available models/params"
                ),
                is_error=True,
            )

        # 等待结果
        result, wait_error = await loop.run_in_executor(None, _wait_for_result, prompt_id, self.base_url)
        if not result:
            return ToolResult(
                output=f"Image generation failed\nError: {wait_error}",
                is_error=True,
            )

        # 下载图片
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

        # 获取实际使用的 seed（如果原来是 -1，workflow 里会随机生成）
        actual_seed = seed
        for node_id, node in workflow.items():
            if node.get("class_type") == "KSampler":
                actual_seed = node.get("inputs", {}).get("seed", seed)
                break

        # 记录历史
        history_id = str(uuid.uuid4())[:8]
        history_entry = {
            "id": history_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style": style,
            "checkpoint": checkpoint,
            "lora": lora,
            "lora_strength": lora_strength if lora else None,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "sampler": sampler,
            "scheduler": scheduler,
            "seed": actual_seed,
            "saved_path": str(save_path) if success else None,
            "comfyui_prompt_id": prompt_id,
        }
        history = self._load_history()
        history.append(history_entry)
        self._save_history(history)

        if success:
            return ToolResult(
                output=(
                    f"Image generated and saved\n"
                    f"Path: images/{filename}\n"
                    f"Size: {width}x{height}\n"
                    f"Model: {checkpoint}\n"
                    f"Sampler: {sampler}/{scheduler}\n"
                    f"Seed: {actual_seed}\n"
                    f"Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n"
                    f"History ID: {history_id} (可用 get_history 复用)"
                ),
                metadata={"image_path": str(save_path), "history_id": history_id},
            )
        else:
            return ToolResult(
                output=f"Image download failed, but it exists in ComfyUI output (prompt_id: {prompt_id})",
                is_error=True,
            )

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────
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
