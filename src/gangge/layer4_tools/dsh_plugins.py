"""DSH / Cordis 插件导入与分类（方案 A 阶段 1）。

**不实现 Cordis 宿主。** 这里只做三件确定的事：

1. 把插件包拿到本地缓存（本地目录 / ``.tgz`` / npm 包名）；
2. 读清单（``package.json`` + ``cordis.patch.yml`` / ``SKILL.md``）判定类别；
3. 汇总成支持率报表，用来回答一个具体问题——
   "这批插件里，有多少条路靠 MCP 就够走通"。

分类结果只有三种，没有第四种模糊态：

- ``MCP`` —— 补丁里有 mcp-client 行，能直接变成 ``MCPClientManager`` 的 server 条目；
- ``SKILL`` —— 带 ``SKILL.md``，能变成提示词资产；
- ``UNSUPPORTED`` —— 含代码、需要 ``ctx.fs`` / ``ctx.llm`` / ``ctx.session`` 之类宿主服务。
  明确报出来，不假装支持。

补丁格式与 ``gangge/runtime/patch.py`` 是同一套 Cordis 语义（``insert`` / ``patch`` /
``remove`` 操作 + 带 ``id`` / ``name`` / ``config`` 的行），但这里刻意**不**复用那套
严格校验：第三方清单是"别人的数据"，畸形行应当降级成一条 reason，而不是让整次扫描失败。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_CACHE = Path(".gangge") / "dsh" / "packages"

#: 补丁文件名候选，按优先级排列（package.json 里显式声明的最优先）。
PATCH_FILE_NAMES = (
    "cordis.patch.yml",
    "cordis.patch.yaml",
    "dsh.patch.yml",
    "dsh.patch.yaml",
    "patch.yml",
    "patch.yaml",
)

#: 一个补丁行是不是"MCP 客户端行"，看名字。DSH 生态里叫 dsh-mcp-client / mcp-client 都有。
_MCP_ROW_RE = re.compile(r"mcp[-_ ]?(client|server|host)", re.IGNORECASE)

_IGNORE_GIT = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo")

#: 传输支持以 ``MCPClientManager`` 为准，这里只做转发，避免两边各写一份。
from gangge.layer4_tools.mcp_client import SUPPORTED_TRANSPORTS  # noqa: E402


class PluginKind(str, Enum):
    MCP = "mcp"
    SKILL = "skill"
    UNSUPPORTED = "unsupported"


class InstallError(RuntimeError):
    """插件拿不到本地（路径不存在、npm 失败、包格式不认）。"""


# ─────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────


@dataclass
class MCPServerSpec:
    """从补丁行里还原出来的一个 MCP server 定义。"""

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    timeout: int = 30
    source_row: str = ""
    #: command/url/args 里有 ``!!js`` 运行时表达式，静态拿到的是表达式文本而非真值
    runtime_expr: bool = False

    @property
    def writable(self) -> bool:
        """能否直接写进 ``mcp_servers.json`` 并连上。"""
        return not self.runtime_expr and self.transport in SUPPORTED_TRANSPORTS

    def to_config_dict(self) -> dict[str, Any]:
        """转成 ``.gangge/mcp_servers.json`` 里 ``servers`` 数组的一项。"""
        out: dict[str, Any] = {
            "name": self.name,
            "transport": self.transport,
            "enabled": True,
            "timeout": self.timeout,
        }
        if self.transport == "stdio":
            out["command"] = self.command
            out["args"] = list(self.args)
            if self.env:
                out["env"] = dict(self.env)
        else:
            out["url"] = self.url
        return out


@dataclass
class SkillSpec:
    """``SKILL.md`` 的 frontmatter + 正文路径。"""

    name: str
    description: str
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": str(self.path)}


@dataclass
class PluginInfo:
    """一个插件包的分类结果。"""

    name: str
    version: str = ""
    path: Path = field(default_factory=Path)
    kind: PluginKind = PluginKind.UNSUPPORTED
    reasons: list[str] = field(default_factory=list)
    mcp_servers: list[MCPServerSpec] = field(default_factory=list)
    skills: list[SkillSpec] = field(default_factory=list)
    patch_file: str = ""
    engines: dict[str, str] = field(default_factory=dict)
    #: 补丁里引用到的、但我们跑不起来的行名
    unhosted_rows: list[str] = field(default_factory=list)

    @property
    def static_servers(self) -> list[MCPServerSpec]:
        """能直接写进配置、且我们的客户端能连上的 server。

        双重门槛：command/url 不是 ``!!js`` 运行时表达式，且 transport 在客户端支持的
        范围内（stdio / sse）。实测 DSH 还有 ``transport: streamable-http``，写进配置
        只会得到"不支持的传输类型"。
        """
        return [s for s in self.mcp_servers if s.writable]

    @property
    def usable(self) -> bool:
        """是否真的产出了可以立刻用的东西。"""
        if self.kind is PluginKind.SKILL:
            return bool(self.skills)
        if self.kind is PluginKind.MCP:
            return bool(self.static_servers)
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "path": str(self.path),
            "kind": self.kind.value,
            "usable": self.usable,
            "reasons": list(self.reasons),
            "patch_file": self.patch_file,
            "engines": dict(self.engines),
            "mcp_servers": [s.to_config_dict() for s in self.static_servers],
            "runtime_expr_servers": [s.to_config_dict() for s in self.mcp_servers if s.runtime_expr],
            "skills": [s.to_dict() for s in self.skills],
            "unhosted_rows": list(self.unhosted_rows),
        }


# ─────────────────────────────────────────
# 清单读取
# ─────────────────────────────────────────


def read_package_json(package_dir: str | Path) -> dict[str, Any]:
    manifest = Path(package_dir) / "package.json"
    if not manifest.exists():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("[DSH] %s: package.json 读不出来: %s", package_dir, e)
        return {}
    return data if isinstance(data, dict) else {}


class JsExpr(str):
    """``!!js "..."`` 这类自定义标签的值。

    真实 DSH 补丁大量在配置里写运行时表达式，例如
    ``command: !!js ctx.cuaComputerUse.command``。这类值**无法静态求值**，
    既不能当普通字符串塞进配置（会把表达式文本当命令跑），也不该让整份文档解析失败。
    因此解析时保留字面值并打上类型标记，分类阶段按"需要宿主求值"处理。
    """


def _yaml_loader():
    """容忍 ``!!js`` / ``!js`` 等自定义标签的 SafeLoader。"""
    import yaml

    class _TolerantLoader(yaml.SafeLoader):
        pass

    def _any_tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return JsExpr(loader.construct_scalar(node))
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    # ``!!js`` 解析成 tag:yaml.org,2002:js；本地标签 ``!js`` 走前缀 "!"
    _TolerantLoader.add_multi_constructor("tag:yaml.org,2002:js", _any_tag)
    _TolerantLoader.add_multi_constructor("!", _any_tag)
    return _TolerantLoader


def _load_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:  # pragma: no cover - 环境相关
        # 没有 PyYAML 时退化成 JSON，至少能让 .json 形式的补丁可用
        return json.loads(text or "[]")
    return yaml.load(text, Loader=_yaml_loader())  # noqa: S506 - 用的是 SafeLoader 子类


def flatten_patch_rows(path: str | Path) -> list[dict[str, Any]]:
    """把补丁文档摊平成行列表（``insert`` 新增 + ``patch`` 覆盖），容错解析。

    畸形文档返回 ``[]``，调用方据此给出"清单不可读"的 reason。
    """
    p = Path(path)
    try:
        data = _load_yaml(p)
    except Exception as e:
        logger.debug("[DSH] %s 解析失败: %s", p, e)
        return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    rows: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        for op in ("insert", "patch"):
            payload = entry.get(op)
            if isinstance(payload, dict):
                payload = [payload]
            if isinstance(payload, list):
                rows.extend(r for r in payload if isinstance(r, dict))
    return rows


def _declared_patch_path(manifest: dict[str, Any]) -> str:
    """``package.json`` 里可能显式声明补丁文件：``dsh.bundle.patch`` 等。"""
    for key in ("dsh", "cordis"):
        section = manifest.get(key)
        if not isinstance(section, dict):
            continue
        bundle = section.get("bundle")
        if isinstance(bundle, dict) and isinstance(bundle.get("patch"), str):
            return bundle["patch"]
        if isinstance(section.get("patch"), str):
            return section["patch"]
    return ""


def find_patch_file(package_dir: Path, manifest: dict[str, Any]) -> Path | None:
    declared = _declared_patch_path(manifest)
    if declared:
        candidate = package_dir / declared
        if candidate.exists():
            return candidate
    for name in PATCH_FILE_NAMES:
        candidate = package_dir / name
        if candidate.exists():
            return candidate
    return None


# ─────────────────────────────────────────
# MCP 行 → server 条目
# ─────────────────────────────────────────


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _spec_from_mapping(
    mapping: dict[str, Any],
    fallback_name: str,
    source_row: str = "",
) -> MCPServerSpec | None:
    """一个映射 → 一个 server。既认 ``command/args``，也认 ``url``。"""
    command = mapping.get("command")
    args_raw = mapping.get("args")
    args = _as_str_list(args_raw)
    url = mapping.get("url") or mapping.get("endpoint") or ""

    # !!js 表达式：值本身是真的（字符串），但它是运行时的求值源，静态用不了
    runtime_expr = (
        isinstance(command, JsExpr)
        or isinstance(url, JsExpr)
        or isinstance(args_raw, JsExpr)
        or (isinstance(args_raw, list) and any(isinstance(a, JsExpr) for a in args_raw))
    )

    if isinstance(command, list):
        parts = [str(x) for x in command]
        command = parts[0] if parts else ""
        args = parts[1:] + args

    if not command and not url:
        return None

    # dsh-mcp-client 用 config.serverName 做工具命名空间（工具名 mcp__<serverName>__<tool>），
    # 不是 name。实测 zego/cua 都是这个写法。
    name = str(mapping.get("serverName") or mapping.get("name") or fallback_name)
    env = mapping.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    try:
        timeout = int(mapping.get("timeout") or 30)
    except (TypeError, ValueError):
        timeout = 30

    transport = mapping.get("transport")
    if not isinstance(transport, str) or not transport:
        transport = "sse" if url and not command else "stdio"

    return MCPServerSpec(
        name=name,
        transport=transport,
        command=str(command or ""),
        args=args,
        env={str(k): str(v) for k, v in env.items()},
        url=str(url),
        timeout=timeout,
        source_row=source_row,
        runtime_expr=runtime_expr,
    )


def _specs_from_config(
    config: dict[str, Any],
    plugin_name: str,
    source_row: str = "",
) -> list[MCPServerSpec]:
    """mcp-client 行的 config 有三种常见写法，逐个摊开。"""
    servers = config.get("servers")
    if isinstance(servers, list):
        out = []
        for i, item in enumerate(servers):
            if isinstance(item, dict):
                spec = _spec_from_mapping(item, f"{plugin_name}-{i + 1}", source_row)
                if spec:
                    out.append(spec)
        return out

    server = config.get("server")
    if isinstance(server, dict):
        spec = _spec_from_mapping(server, plugin_name, source_row)
        return [spec] if spec else []

    spec = _spec_from_mapping(config, plugin_name, source_row)
    return [spec] if spec else []


def _looks_like_mcp_row(row: dict[str, Any]) -> bool:
    name = str(row.get("name") or "")
    if _MCP_ROW_RE.search(name):
        return True
    config = row.get("config")
    if not isinstance(config, dict):
        return False
    # 没写 mcp-client 名字，但 config 长得像 server 定义
    if isinstance(config.get("servers"), list) and config["servers"]:
        return True
    return bool(config.get("command") or config.get("url"))


def extract_mcp_servers(rows: Iterable[dict[str, Any]], plugin_name: str) -> list[MCPServerSpec]:
    specs: list[MCPServerSpec] = []
    for row in rows:
        if not _looks_like_mcp_row(row):
            continue
        config = row.get("config")
        if not isinstance(config, dict):
            continue
        row_id = str(row.get("id") or row.get("name") or "")
        specs.extend(_specs_from_config(config, plugin_name, row_id))
    return specs


# ─────────────────────────────────────────
# 原生 MCP server 包的推断
# ─────────────────────────────────────────

#: 出现这些依赖，说明包自己就是 MCP server 实现（不需要任何补丁清单）。
_MCP_SDK_DEPS = (
    "@modelcontextprotocol/sdk",
    "@modelcontextprotocol/server",
    "fastmcp",
    "mcp",
)


def infer_native_mcp_server(
    manifest: dict[str, Any],
    *,
    is_dsh_plugin: bool = False,
) -> tuple[bool, str]:
    """判断一个包是不是"装上就能当 MCP server 用"。

    只有拿到证据才算，不做"名字里有 mcp 就算"的猜测——误判会把普通的 CLI 工具
    当成 server 塞进配置，比漏判更难查。

    ``is_dsh_plugin`` 为真时**一律不推断**：声明了 ``dsh`` / ``cordis`` 字段或带
    ``cordis.patch.yml`` 的包，其入口是 cordis 插件模块（``lib/index.js``），由宿主
    加载；``npx -y <pkg>`` 既没有 bin 也不是 MCP server 协议，跑出来只会是垃圾。
    实测：18 个真实 DSH 插件里 8 个名字/关键词带 mcp，全部属于这种误判。
    """
    if is_dsh_plugin:
        return False, ""

    name = str(manifest.get("name") or "")

    keywords = manifest.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    if any("mcp" in str(k).lower() for k in keywords if k):
        return True, "keywords 里声明了 mcp"

    for field in ("dependencies", "peerDependencies"):
        deps = manifest.get(field)
        if isinstance(deps, dict) and any(dep in deps for dep in _MCP_SDK_DEPS):
            return True, f"{field} 里有 MCP SDK"

    has_bin = bool(manifest.get("bin"))
    if has_bin and re.search(r"mcp", name, re.IGNORECASE):
        return True, "包名含 mcp 且声明了 bin"

    return False, ""


def _short_name(package_name: str) -> str:
    return package_name.rsplit("/", 1)[-1] or package_name


def _npx_server_spec(package_name: str) -> MCPServerSpec:
    """原生 server 包的标准启动方式：``npx -y <pkg>``。"""
    return MCPServerSpec(
        name=_short_name(package_name),
        transport="stdio",
        command="npx",
        args=["-y", package_name],
        source_row="(inferred)",
    )


# ─────────────────────────────────────────
# SKILL.md
# ─────────────────────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    head, body = parts[1], parts[2]
    meta: dict[str, Any] = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        meta[key.strip()] = value
    return meta, body.lstrip("\n")


#: SKILL.md 出现在这几层以内都算（实测布局：``SKILL.md`` / ``skill/SKILL.md`` /
#: ``skills/<name>/SKILL.md``，嵌套更深的通常是 vendored 的第三方目录，不算本插件的技能）。
SKILL_MAX_DEPTH = 3


def find_skill_files(package_dir: Path) -> list[Path]:
    """在包内找所有 ``SKILL.md``，深度受限、跳过 node_modules。"""
    found: list[Path] = []
    base_depth = len(package_dir.parts)
    for path in sorted(package_dir.rglob("SKILL.md")):
        if any(part in ("node_modules", ".git") for part in path.parts):
            continue
        if len(path.parts) - base_depth > SKILL_MAX_DEPTH:
            continue
        found.append(path)
    return found


def read_skill(path: str | Path) -> SkillSpec:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = ""
    meta, _body = _parse_frontmatter(text)
    name = str(meta.get("name") or p.parent.name)
    description = str(meta.get("description") or "").strip()
    return SkillSpec(name=name, description=description, path=p)


# ─────────────────────────────────────────
# 分类
# ─────────────────────────────────────────


def classify_package(package_dir: str | Path) -> PluginInfo:
    """判定一个已解包的插件属于哪一类。永远不抛异常。"""
    pkg = Path(package_dir).resolve()
    manifest = read_package_json(pkg)

    name = str(manifest.get("name") or pkg.name)
    version = str(manifest.get("version") or "")
    engines_raw = manifest.get("engines")
    engines = (
        {str(k): str(v) for k, v in engines_raw.items() if isinstance(v, (str, int, float))}
        if isinstance(engines_raw, dict)
        else {}
    )
    info = PluginInfo(name=name, version=version, path=pkg, engines=engines)

    if not manifest:
        info.reasons.append("没有可用的 package.json，无法确认它是个插件包")

    patch_file = find_patch_file(pkg, manifest)
    rows: list[dict[str, Any]] = []
    if patch_file:
        info.patch_file = patch_file.name
        rows = flatten_patch_rows(patch_file)
        if not rows:
            info.reasons.append(f"{patch_file.name} 存在但解析不出任何插件行")

    if rows:
        info.mcp_servers = extract_mcp_servers(rows, name)
        hosted = [
            str(r.get("name") or r.get("id") or "")
            for r in rows
            if not _looks_like_mcp_row(r)
        ]
        info.unhosted_rows = [h for h in hosted if h]

    info.skills = [read_skill(f) for f in find_skill_files(pkg)]

    static_servers = info.static_servers
    expr_servers = [s for s in info.mcp_servers if s.runtime_expr]
    bad_transport = [s for s in info.mcp_servers if not s.runtime_expr and not s.writable]
    is_dsh_plugin = bool(manifest.get("dsh") or manifest.get("cordis") or patch_file)

    if bad_transport:
        info.reasons.append(
            "transport 不受客户端支持: "
            + ", ".join(f"{s.name}={s.transport}" for s in bad_transport[:3])
        )

    if static_servers:
        info.kind = PluginKind.MCP
        if info.unhosted_rows:
            info.reasons.append(
                "补丁里还有非 MCP 行，本方案不执行它们: "
                + ", ".join(info.unhosted_rows[:5])
                + (" …" if len(info.unhosted_rows) > 5 else "")
            )
    elif info.skills:
        info.kind = PluginKind.SKILL
        if expr_servers:
            info.reasons.append(
                "另有 MCP 行，但 command/url 是 !!js 运行时表达式，静态读取拿不到"
            )
    elif info.mcp_servers:
        # MCP 行确实存在，但值是运行时的或传输不受支持：分类上仍算 MCP，
        # 可产出上算 0——写进配置只会得到一条永远连不上的 server。
        info.kind = PluginKind.MCP
        if expr_servers and not bad_transport:
            info.reasons.append(
                "MCP 行存在，但 command/url 是 !!js 运行时表达式，"
                "静态读取拿不到真值（需要宿主求值）"
            )
    else:
        native, evidence = infer_native_mcp_server(manifest, is_dsh_plugin=is_dsh_plugin)
        if native:
            # 包自己就是 MCP server（MCP 生态里这类包不属于 DSH 插件），不需要补丁。
            info.kind = PluginKind.MCP
            info.mcp_servers = [_npx_server_spec(name)]
            info.reasons.append(f"没有补丁清单，按 {evidence} 推断为原生 MCP server，用 npx 启动")
        else:
            info.kind = PluginKind.UNSUPPORTED
            if info.unhosted_rows:
                info.reasons.append(
                    "只声明了需要宿主服务的行（如 " + ", ".join(info.unhosted_rows[:3]) + "），"
                    "没有 mcp-client 也没有 SKILL.md"
                )
            elif not info.reasons:
                info.reasons.append("含运行时代码，需要 Cordis 宿主提供 ctx 服务")

    return info


def _version_key(name: str) -> tuple:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"[.\-+]", name)
    )


def _latest_version_dir(package_dir: Path) -> Path | None:
    """在 ``<name>/<version>/`` 布局里取版本最高的那个目录。"""
    versions = [
        d for d in package_dir.iterdir()
        if d.is_dir() and (d / "package.json").exists()
    ]
    if not versions:
        return None
    return max(versions, key=lambda d: _version_key(d.name))


def scan(paths: Sequence[str | Path]) -> list[PluginInfo]:
    """扫描若干路径：既可以直接是插件目录，也可以是装着插件目录的缓存根。

    兼容三种布局：直接是包目录、``<root>/<name>/``、以及我们自己的缓存布局
    ``<root>/<name>/<version>/``（同名多版本只取最高版本）。
    另支持 npm 的 ``@scope/name`` 两级命名。
    """
    found: list[Path] = []
    for raw in paths:
        root = Path(raw)
        if not root.exists():
            logger.warning("[DSH] 路径不存在，跳过: %s", root)
            continue
        if (root / "package.json").exists():
            found.append(root)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if (child / "package.json").exists():
                found.append(child)
                continue
            # @scope/name 再下一层是包目录（或包目录的版本层）
            depth_one = sorted(c for c in child.iterdir() if c.is_dir()) if child.name.startswith("@") else [child]
            for pkg_dir in depth_one:
                if (pkg_dir / "package.json").exists():
                    found.append(pkg_dir)
                    continue
                latest = _latest_version_dir(pkg_dir)
                if latest:
                    found.append(latest)
    return [classify_package(p) for p in found]


# ─────────────────────────────────────────
# 报表
# ─────────────────────────────────────────


@dataclass
class Report:
    plugins: list[PluginInfo] = field(default_factory=list)

    def count(self, kind: PluginKind) -> int:
        return sum(1 for p in self.plugins if p.kind is kind)

    @property
    def total(self) -> int:
        return len(self.plugins)

    @property
    def usable(self) -> int:
        return sum(1 for p in self.plugins if p.usable)

    @property
    def support_rate(self) -> float:
        return (self.usable / self.total) if self.total else 0.0

    @property
    def mcp_server_count(self) -> int:
        """可直接写进配置的 server 数（不含 !js 运行时表达式）。"""
        return sum(len(p.static_servers) for p in self.plugins)

    @property
    def skill_count(self) -> int:
        return sum(len(p.skills) for p in self.plugins)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "usable": self.usable,
            "support_rate": round(self.support_rate, 4),
            "mcp": self.count(PluginKind.MCP),
            "skill": self.count(PluginKind.SKILL),
            "unsupported": self.count(PluginKind.UNSUPPORTED),
            "mcp_servers": self.mcp_server_count,
            "skills": self.skill_count,
            "plugins": [p.to_dict() for p in self.plugins],
        }

    def format_text(self) -> str:
        lines = [
            f"{_cell('插件', 30)} {_cell('版本', 10)} {_cell('分类', 12)} {_cell('可用', 5)} 说明",
            "-" * 96,
        ]
        for p in self.plugins:
            detail = "; ".join(p.reasons) if p.reasons else _default_detail(p)
            lines.append(
                f"{_cell(p.name, 30)} {_cell(p.version or '-', 10)} "
                f"{_cell(p.kind.value, 12)} {_cell('是' if p.usable else '否', 5)} {detail}"
            )
        lines.append("-" * 96)
        lines.append(
            f"合计 {self.total} 个：MCP {self.count(PluginKind.MCP)} / "
            f"SKILL {self.count(PluginKind.SKILL)} / "
            f"UNSUPPORTED {self.count(PluginKind.UNSUPPORTED)}"
        )
        lines.append(
            f"支持率 {self.usable}/{self.total} = {self.support_rate:.0%}"
            f"（可产出 {self.mcp_server_count} 个 MCP server、{self.skill_count} 个技能）"
        )
        if self.total and self.support_rate < 0.4:
            lines.append(
                "提示：支持率低于 40% —— 这批插件的价值主要在代码里（ctx 服务、JS 实现），"
                "静态读取搬不过来。先锁定真正要的那几个目标插件，再决定是否为它们做宿主方案；"
                "把预算压在「通用插件加载」上不划算。"
            )
        return "\n".join(lines)


def _default_detail(p: PluginInfo) -> str:
    if p.static_servers:
        names = ", ".join(s.name for s in p.static_servers)
        return f"{len(p.static_servers)} 个 MCP server: {names}"
    if p.skills:
        names = ", ".join(s.name for s in p.skills)
        return f"{len(p.skills)} 个技能: {names}"
    return ""


def build_report(plugins: Iterable[PluginInfo]) -> Report:
    return Report(plugins=list(plugins))


def _cell(text: str, width: int) -> str:
    text = str(text)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


# ─────────────────────────────────────────
# 安装（本地目录 / .tgz / npm 包名）
# ─────────────────────────────────────────


def _cache_path(dest_root: Path, name: str, version: str) -> Path:
    safe = name.replace("/", "__").replace("@", "")
    return dest_root / safe / (version or "0.0.0")


def _extract_tarball(tarball: Path, target: Path) -> None:
    with tarfile.open(tarball) as tar:
        members = tar.getmembers()
        # npm 包的路径都在 package/ 下，剥掉一层
        stripped = []
        for m in members:
            if m.name == "package" or m.name.startswith("package/"):
                m.name = m.name[len("package"):].lstrip("/")
                if m.name:
                    stripped.append(m)
        target.mkdir(parents=True, exist_ok=True)
        tar.extractall(target, members=stripped)


def _npm_executable() -> str | None:
    """Windows 上 npm 是 ``npm.cmd``，直接传 "npm" 给 CreateProcess 会 FileNotFoundError。"""
    return shutil.which("npm") or shutil.which("npm.cmd")


def _looks_like_local_path(spec: str) -> bool:
    if spec.startswith("@"):          # npm 作用域包名，不是路径
        return False
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", spec)
        or spec.startswith((".", "/", "\\"))
        or "/" in spec
        or "\\" in spec
    )


def _npm_pack(spec: str, workdir: Path) -> Path:
    npm = _npm_executable()
    if npm is None:
        raise InstallError(
            f"没有 npm，无法拉取 {spec}。装 Node.js 后重试，或手动下载解包到 {DEFAULT_CACHE}"
        )
    proc = subprocess.run(
        [npm, "pack", spec, "--pack-destination", str(workdir), "--silent"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise InstallError(f"npm pack {spec} 失败: {(proc.stderr or proc.stdout).strip()[:500]}")
    names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip().endswith(".tgz")]
    if not names:
        tarballs = sorted(workdir.glob("*.tgz"))
        if not tarballs:
            raise InstallError(f"npm pack {spec} 没有产出 .tgz")
        return tarballs[-1]
    return workdir / names[-1]


def install(
    spec: str | Path,
    dest_root: str | Path = DEFAULT_CACHE,
    *,
    with_node_modules: bool = False,
) -> PluginInfo:
    """把插件放进缓存目录并分类。

    ``spec`` 可以是：本地目录、``.tgz`` 包、或 npm 包名（走 ``npm pack``）。
    """
    dest_root = Path(dest_root)
    src = Path(spec)

    ignore = _IGNORE_GIT
    if not with_node_modules:
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo", "node_modules")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        if src.is_dir():
            staged = src
        elif src.is_file() and src.suffix in (".tgz", ".tar", ".gz"):
            staged = tmp_path / "pkg"
            _extract_tarball(src, staged)
        elif _looks_like_local_path(str(spec)):
            raise InstallError(f"路径不存在: {spec}")
        else:
            tarball = _npm_pack(str(spec), tmp_path)
            staged = tmp_path / "pkg"
            _extract_tarball(tarball, staged)

        manifest = read_package_json(staged)
        if not manifest:
            raise InstallError(f"{spec}: 解包后没有 package.json，不是可识别的插件包")

        name = str(manifest.get("name") or staged.name)
        version = str(manifest.get("version") or "0.0.0")
        target = _cache_path(dest_root, name, version)

        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, target, ignore=ignore, dirs_exist_ok=True)
        logger.info("[DSH] 已安装 %s@%s → %s", name, version, target)

    return classify_package(target)


# ─────────────────────────────────────────
# 落到 mcp_servers.json
# ─────────────────────────────────────────


def build_server_configs(plugins: Iterable[PluginInfo]) -> list[dict[str, Any]]:
    """把所有插件产出的 server 摊成 ``mcp_servers.json`` 的 ``servers`` 数组。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for plugin in plugins:
        for spec in plugin.static_servers:
            entry = spec.to_config_dict()
            name = str(entry["name"])
            if name in seen:
                # 同名 server 来自不同插件：用插件名限定，别静默覆盖
                qualified = f"{_short_name(plugin.name)}-{name}"
                if qualified in seen:
                    logger.warning("[DSH] server 名冲突，跳过: %s (%s)", name, plugin.name)
                    continue
                logger.info("[DSH] server 名冲突，重命名: %s → %s", name, qualified)
                entry["name"] = qualified
                name = qualified
            seen.add(name)
            out.append(entry)
    return out


@dataclass
class ExportResult:
    path: Path
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)

    def format_text(self) -> str:
        verb = "将写入" if self.dry_run else "已写入"
        lines = [f"{verb} {self.path}"]
        if self.added:
            lines.append(f"  新增: {', '.join(self.added)}")
        if self.updated:
            lines.append(f"  更新: {', '.join(self.updated)}")
        if self.skipped:
            lines.append(f"  跳过（已存在同名，用 --force 覆盖）: {', '.join(self.skipped)}")
        if not self.changed:
            lines.append("  无变化")
        return "\n".join(lines)


def export_config(
    plugins: Iterable[PluginInfo],
    config_path: str | Path = Path(".gangge") / "mcp_servers.json",
    *,
    force: bool = False,
    dry_run: bool = False,
) -> ExportResult:
    """把插件产出的 server 条目并入 ``mcp_servers.json``。

    默认**不覆盖**已存在的同名条目——用户可能手改过 command/args，拿插件的默认值
    盖掉他是在帮倒忙。要更新就显式 ``force=True``。
    """
    path = Path(config_path)
    result = ExportResult(path=path, dry_run=dry_run)

    document: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise InstallError(f"{path} 不是合法 JSON，先修好再导: {e}") from e
        if isinstance(loaded, dict):
            document = loaded
        elif isinstance(loaded, list):
            document = {"servers": loaded}

    servers = document.get("servers")
    if not isinstance(servers, list):
        servers = []
    document["servers"] = servers

    by_name: dict[str, dict[str, Any]] = {
        str(s.get("name")): s for s in servers if isinstance(s, dict) and s.get("name")
    }

    for entry in build_server_configs(plugins):
        name = str(entry["name"])
        if name in by_name:
            if not force or by_name[name] == entry:
                result.skipped.append(name)
                continue
            by_name[name].update(entry)
            result.updated.append(name)
        else:
            servers.append(entry)
            by_name[name] = entry
            result.added.append(name)

    if result.changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return result


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gangge-dsh",
        description="DSH / Cordis 插件导入与分类：只读清单，不实现宿主",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="分类已解包的插件（目录或缓存根）")
    p_scan.add_argument("paths", nargs="+", help="插件目录，或装着插件的缓存根")
    p_scan.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")

    p_install = sub.add_parser("install", help="安装到缓存后分类（本地目录 / .tgz / npm 包名）")
    p_install.add_argument("specs", nargs="+")
    p_install.add_argument("--dest", default=str(DEFAULT_CACHE), help=f"缓存根（默认 {DEFAULT_CACHE}）")
    p_install.add_argument("--with-node-modules", action="store_true", help="连 node_modules 一起复制")
    p_install.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")

    p_export = sub.add_parser("export", help="把插件产出的 MCP server 写进 mcp_servers.json")
    p_export.add_argument("paths", nargs="+", help="插件目录，或装着插件的缓存根")
    p_export.add_argument(
        "--config",
        default=str(Path(".gangge") / "mcp_servers.json"),
        help="目标配置文件（默认 .gangge/mcp_servers.json）",
    )
    p_export.add_argument("--force", action="store_true", help="覆盖已存在的同名条目")
    p_export.add_argument("--dry-run", action="store_true", help="只打印将要写入什么")
    p_export.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args.command == "export":
        plugins = scan(args.paths)
        report = build_report(plugins)
        try:
            result = export_config(
                plugins, args.config, force=args.force, dry_run=args.dry_run
            )
        except InstallError as e:
            print(f"[错误] {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(
                {
                    "path": str(result.path),
                    "added": result.added,
                    "updated": result.updated,
                    "skipped": result.skipped,
                    "dry_run": result.dry_run,
                    "usable_plugins": report.usable,
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print(result.format_text())
            if not report.usable:
                print(
                    "提示: 没有插件产出 MCP server，配置不会变；先跑 scan 看分类原因。",
                    file=sys.stderr,
                )
        return 0 if report.usable else 1

    if args.command == "scan":
        plugins = scan(args.paths)
    else:
        plugins = []
        for spec in args.specs:
            try:
                plugins.append(
                    install(spec, args.dest, with_node_modules=args.with_node_modules)
                )
            except InstallError as e:
                print(f"[跳过] {e}", file=sys.stderr)

    report = build_report(plugins)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.format_text())
    return 0 if report.total else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
