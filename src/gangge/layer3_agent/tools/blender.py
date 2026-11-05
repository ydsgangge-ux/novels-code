"""Blender 3D 建模工具 —— 内置工具，无需 MCP 配置。

设计取舍：
- 用 ``blender -b -P`` 子进程执行，而不是 Blender 官方的 socket addon。
  理由是本机/企业环境里安全软件的 WFP 过滤驱动会吞掉 blender.exe 的 TCP
  监听（bind/listen 成功、netstat 显示 LISTENING，但 SYN 无响应，出站却正常），
  socket 方案不可靠；子进程方式零网络依赖，跨平台一致。
- 场景状态通过 ``<workspace>/.gangge/blender/workspace.blend`` 在多次调用之间
  持久化，所以可以一步一步地建模，而不必每轮重建。

Blender 路径探测顺序：``BLENDER_PATH`` 环境变量 → PATH → 各平台常见安装目录。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import string
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

# 探测结果缓存（进程内只探测一次）
_PROBED = False
_CACHED: str | None = None


def find_blender() -> str | None:
    """返回 Blender 可执行文件路径；找不到返回 None。"""
    global _PROBED, _CACHED
    if _PROBED:
        return _CACHED

    _PROBED = True

    for env_key in ("BLENDER_PATH", "BLENDER_EXECUTABLE", "BLENDER_BIN"):
        value = os.environ.get(env_key, "").strip()
        if value and Path(value).exists():
            _CACHED = value
            return _CACHED

    which = shutil.which("blender")
    if which:
        _CACHED = which
        return _CACHED

    candidates: list[Path] = []

    if sys.platform == "win32":
        for drive in string.ascii_uppercase:
            root = Path(f"{drive}:/")
            try:
                if not root.exists():
                    continue
            except OSError:
                continue
            for pattern in (
                "Program Files/Blender Foundation/Blender */blender.exe",
                "Program Files/Blender Foundation/Blender/blender.exe",
                "Blender */blender.exe",
            ):
                try:
                    candidates.extend(sorted(root.glob(pattern), reverse=True))
                except OSError:
                    pass
    elif sys.platform == "darwin":
        app = Path("/Applications/Blender.app/Contents/MacOS/Blender")
        if app.exists():
            candidates.append(app)
        try:
            candidates.extend(Path("/Applications").glob("Blender*.app/Contents/MacOS/Blender"))
        except OSError:
            pass
    else:
        for path in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"):
            p = Path(path)
            if p.exists():
                candidates.append(p)
        try:
            candidates.extend(Path("/opt").glob("blender*/blender"))
        except OSError:
            pass

    for candidate in candidates:
        if candidate and candidate.exists():
            _CACHED = str(candidate)
            return _CACHED

    _CACHED = None
    return None


WRAPPER = '''# -*- coding: utf-8 -*-
"""由 BlenderTool 生成的一次性执行器。"""
import bpy, sys, traceback

args = sys.argv[sys.argv.index("--") + 1:]
work_blend, user_path = args[0], args[1]

with open(user_path, "r", encoding="utf-8") as f:
    code = f.read()


def bsdf_of(mat):
    """按节点类型取原理化 BSDF。

    中文版 Blender 会把节点名本地化成「原理化 BSDF」，所以不能按名字取。
    """
    try:
        if not mat.use_nodes:
            mat.use_nodes = True
    except Exception:
        pass
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _set_input(node, name, value):
    if node is None:
        return False
    for socket in node.inputs:
        if socket.identifier == name or socket.name == name:
            try:
                socket.default_value = value
                return True
            except Exception:
                return False
    return False


def new_material(name, color=(0.8, 0.8, 0.8, 1.0), roughness=0.5,
                 metallic=0.0, alpha=1.0, transmission=0.0):
    """建材质，规避中文界面下节点名本地化的问题。"""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    node = bsdf_of(mat)
    _set_input(node, "Base Color", color)
    _set_input(node, "Roughness", roughness)
    _set_input(node, "Metallic", metallic)
    _set_input(node, "Alpha", alpha)
    if transmission:
        for key in ("Transmission Weight", "Transmission"):
            if _set_input(node, key, transmission):
                break
    return mat


GLOBALS = {
    "bpy": bpy,
    "__name__": "__mcp__",
    "bsdf_of": bsdf_of,
    "new_material": new_material,
}

print("__BLENDER_BEGIN__", flush=True)
ok = True
try:
    exec(compile(code, "<blender_user_code>", "exec"), GLOBALS)
except Exception:
    # 必须写 stdout：stderr 不会被回收进结果里
    traceback.print_exc(file=sys.stdout)
    ok = False
print("__BLENDER_END__", flush=True)

try:
    bpy.ops.wm.save_mainfile(filepath=work_blend)
    print("__SAVED__", flush=True)
except Exception:
    traceback.print_exc(file=sys.stdout)

sys.exit(0 if ok else 1)
'''

SCENE_INFO_CODE = '''
import bpy, json

scene = bpy.context.scene
objects = []
for obj in scene.objects:
    materials = []
    if getattr(obj, "data", None) is not None and hasattr(obj.data, "materials"):
        materials = [mat.name for mat in obj.data.materials]
    objects.append({
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 3) for v in obj.location],
        "dimensions": [round(v, 3) for v in obj.dimensions],
        "materials": materials,
    })

print(json.dumps({
    "blend_file": bpy.data.filepath,
    "engine": scene.render.engine,
    "unit_scale": scene.unit_settings.scale_length,
    "unit_length": scene.unit_settings.length_unit,
    "object_count": len(objects),
    "objects": objects,
}, ensure_ascii=False, indent=2))
'''


def _object_info_code(name: str) -> str:
    return '''
import bpy, json

obj = bpy.data.objects.get(%r)
if obj is None:
    print(json.dumps({"error": "对象不存在: " + %r}, ensure_ascii=False))
else:
    print(json.dumps({
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 4) for v in obj.location],
        "rotation": [round(v, 4) for v in obj.rotation_euler],
        "dimensions": [round(v, 4) for v in obj.dimensions],
        "scale": [round(v, 4) for v in obj.scale],
        "materials": [m.name for m in obj.data.materials] if hasattr(obj.data, "materials") else [],
        "modifiers": [m.type for m in obj.modifiers],
        "vertices": len(obj.data.vertices) if obj.type == "MESH" else None,
    }, ensure_ascii=False, indent=2))
''' % (name, name)


def _render_code(path: str, res_x: int, res_y: int, samples: int, engine: str) -> str:
    return '''
import bpy, math
from mathutils import Vector

scene = bpy.context.scene
scene.render.filepath = r"{path}"
scene.render.resolution_x = {rx}
scene.render.resolution_y = {ry}
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
try:
    scene.render.engine = "{engine}"
    if "{engine}" == "CYCLES":
        scene.cycles.samples = {samples}
        scene.cycles.device = "CPU"
except Exception as exc:
    print("engine set failed:", exc)

# 按场景包围盒自动取景，避免物体在画面外
points = []
for obj in scene.objects:
    if obj.type == "MESH":
        points.extend([obj.matrix_world @ Vector(c) for c in obj.bound_box])

if points:
    mn = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    mx = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center = (mn + mx) / 2
    radius = max((mx - mn).length / 2, 1e-4)
else:
    center, radius = Vector((0, 0, 0)), 10.0

cam = scene.camera
if cam is None or cam.name.startswith("BlenderTool_Cam"):
    if cam is None:
        cam_data = bpy.data.cameras.new("BlenderTool_Cam")
        cam = bpy.data.objects.new("BlenderTool_Cam", cam_data)
        scene.collection.objects.link(cam)
    cam.data.lens = 85
    cam.location = center + Vector((-1.15, -1.45, 1.05)) * (radius * 2.6)
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    print("camera framed, radius=%.3f" % radius)

# 没灯就补三盏太阳光，否则渲染结果是全黑
if not [o for o in scene.objects if o.type == "LIGHT"]:
    for light_name, direction, energy in (
        ("BlenderTool_Key", (-1.0, -1.2, 1.3), 4.0),
        ("BlenderTool_Fill", (1.2, -0.8, 0.4), 1.4),
        ("BlenderTool_Rim", (0.3, 1.3, 0.9), 2.5),
    ):
        light_data = bpy.data.lights.new(light_name, "SUN")
        light_data.energy = energy
        light_data.angle = math.radians(12)
        light = bpy.data.objects.new(light_name, light_data)
        scene.collection.objects.link(light)
        light.location = center + Vector(direction) * (radius * 3.0)
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
    print("added 3 sun lights")

if scene.world is None:
    world = bpy.data.worlds.new("BlenderTool_World")
    world.use_nodes = True
    scene.world = world
try:
    background = scene.world.node_tree.nodes.get("Background")
    if background:
        background.inputs[1].default_value = max(background.inputs[1].default_value, 0.6)
except Exception:
    pass

bpy.ops.render.render(write_still=True)
print("rendered ->", r"{path}")
'''.format(path=path.replace("\\", "/"), rx=res_x, ry=res_y,
           samples=samples, engine=engine)


class BlenderTool(BaseTool):
    """在 Blender 中执行 bpy 代码，完成建模与渲染。"""

    def __init__(self, workspace: str | Path = ".") -> None:
        self.workspace = Path(workspace) if workspace else Path(".")
        self.blender = find_blender()
        self.work_dir = self.workspace / ".gangge" / "blender"
        self.blend_file = self.work_dir / "workspace.blend"
        self.timeout = int(os.environ.get("BLENDER_TIMEOUT", "300"))

    # ---------------------------------------------------------------- 声明

    @property
    def name(self) -> str:
        return "blender"

    @property
    def description(self) -> str:
        return (
            "在 Blender 中建模与渲染。执行一段 bpy Python 代码，"
            "场景状态会持久保存到 .gangge/blender/workspace.blend，可多轮增量建模。\n\n"
            "action 取值：\n"
            "- execute：执行 code（最常用）\n"
            "- scene_info：列出当前场景里的对象、尺寸、材质\n"
            "- object_info：查看单个对象详情（需 name）\n"
            "- render：渲染成 PNG（需 output_path，可传 resolution_x/y、samples）\n"
            "- reset：清空场景重新开始\n\n"
            "写代码要点：\n"
            "1. 精确尺寸：先 primitive_cube_add(size=1)，再设 scale，最后 "
            "transform_apply(location=False, rotation=False, scale=True)\n"
            "2. 材质用注入的 new_material(name, color, roughness, metallic)，"
            "不要用 nodes.get('Principled BSDF')——中文版 Blender 会把节点名"
            "本地化成「原理化 BSDF」，按名字取会拿到 None\n"
            "3. 布尔、倒角等用 modifiers.new(...) 后 modifier_apply\n"
            "4. 每次只做一件小事，用 print() 打出关键尺寸，便于核对"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["execute", "scene_info", "object_info", "render", "reset"],
                    "description": "要执行的操作，默认 execute",
                    "default": "execute",
                },
                "code": {
                    "type": "string",
                    "description": "[execute] 要执行的 bpy Python 代码",
                },
                "name": {
                    "type": "string",
                    "description": "[object_info] 对象名称",
                },
                "output_path": {
                    "type": "string",
                    "description": "[render] PNG 保存路径，默认 .gangge/blender/render.png",
                },
                "resolution_x": {
                    "type": "integer",
                    "description": "[render] 输出宽度，默认 1200",
                    "default": 1200,
                },
                "resolution_y": {
                    "type": "integer",
                    "description": "[render] 输出高度，默认 900",
                    "default": 900,
                },
                "samples": {
                    "type": "integer",
                    "description": "[render] Cycles 采样数，默认 32，越大越慢越干净",
                    "default": 32,
                },
            },
        }

    @property
    def tags(self) -> tuple[str, ...]:
        return ("blender", "3d", "建模", "model", "render", "渲染", "mesh", "bpy")

    # ---------------------------------------------------------------- 执行

    async def execute(self, **kwargs: Any) -> ToolResult:
        if not self.blender:
            return ToolResult(
                output=(
                    "未找到 Blender。请安装 Blender 后重试，或用环境变量指定路径：\n"
                    "  Windows:  set BLENDER_PATH=D:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe\n"
                    "  macOS:    export BLENDER_PATH=/Applications/Blender.app/Contents/MacOS/Blender\n"
                    "  Linux:    export BLENDER_PATH=/usr/bin/blender"
                ),
                is_error=True,
            )

        action = (kwargs.get("action") or "execute").strip()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._dispatch, action, kwargs)

    def _dispatch(self, action: str, kwargs: dict[str, Any]) -> ToolResult:
        try:
            if action == "execute":
                code = (kwargs.get("code") or "").strip()
                if not code:
                    return ToolResult(output="execute 需要 code 参数（bpy Python 代码）", is_error=True)
                return self._run_code(code)

            if action == "scene_info":
                return self._run_code(SCENE_INFO_CODE)

            if action == "object_info":
                name = (kwargs.get("name") or "").strip()
                if not name:
                    return ToolResult(output="object_info 需要 name 参数", is_error=True)
                return self._run_code(_object_info_code(name))

            if action == "render":
                output = kwargs.get("output_path") or str(self.work_dir / "render.png")
                return self._run_code(_render_code(
                    str(output),
                    int(kwargs.get("resolution_x") or 1200),
                    int(kwargs.get("resolution_y") or 900),
                    int(kwargs.get("samples") or 32),
                    "CYCLES",
                ))

            if action == "reset":
                if self.blend_file.exists():
                    self.blend_file.unlink()
                return ToolResult(output="场景已重置，下次 execute 会从空场景开始")

            return ToolResult(output=f"未知 action: {action}", is_error=True)

        except Exception:
            return ToolResult(output="执行异常:\n" + traceback.format_exc()[-1500:], is_error=True)

    def _run_code(self, code: str) -> ToolResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = self.work_dir / "_wrapper.py"
        code_path = self.work_dir / "_code.py"
        wrapper_path.write_text(WRAPPER, encoding="utf-8")
        code_path.write_text(code, encoding="utf-8")

        # --factory-startup 忽略用户界面语言等偏好，保证新建数据块用英文名，
        # 对模型生成的代码更可预测；不影响加载已有的 workspace.blend。
        cmd = [self.blender, "-b", "--factory-startup"]
        if self.blend_file.exists():
            cmd.append(str(self.blend_file))
        cmd += ["-P", str(wrapper_path), "--", str(self.blend_file), str(code_path)]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output=f"Blender 执行超时（{self.timeout} 秒）", is_error=True)
        except Exception:
            return ToolResult(output="启动 Blender 失败:\n" + traceback.format_exc()[-1000:], is_error=True)

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")

        begin, end = "__BLENDER_BEGIN__", "__BLENDER_END__"
        if begin in stdout and end in stdout:
            body = stdout.split(begin, 1)[1].split(end, 1)[0].strip()
        else:
            body = (stdout or stderr).strip()[-4000:]

        ok = (end in stdout) and proc.returncode == 0
        saved = "__SAVED__" in stdout
        text = f"{'执行成功' if ok else '执行失败'}｜场景已保存: {'是' if saved else '否'}\n{body or '(无输出)'}"
        if not saved and stderr:
            text += "\n--- stderr ---\n" + stderr.strip()[-1200:]
        return ToolResult(output=text, is_error=not ok)
