"""Package info tool — structured API access for GitHub/npm/PyPI.

与 web_fetch 的分工：
  - web_fetch: 通用网页正文提取（有损，依赖提取算法）
  - package_info: 结构化 API 直连（无损，直接拿 JSON）

覆盖的服务：
  - GitHub: api.github.com / raw.githubusercontent.com
  - npm: registry.npmjs.org
  - PyPI: pypi.org/pypi/{package}/json

所有 API 均为公开接口，无需认证（GitHub 未认证有 60次/小时限制）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 从环境变量读取 GitHub Token（可选，用于提升速率限制）
_GITHUB_TOKEN = os.environ.get("GANGGE_GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")

# 请求超时
_TIMEOUT = 15

# 返回内容最大字符数
_MAX_RESULT_CHARS = 8000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... (已截断，原始共 {len(text)} 字符)"


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GanggeBot/1.0",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    return headers


class PackageInfoTool(BaseTool):
    """结构化 API 直连工具 — 查 GitHub/npm/PyPI 包信息。

    比 web_fetch 精准（直接拿 JSON，无正文提取损失）。
    用于查包版本、依赖、README、repo 信息、issue 等。
    """

    @property
    def name(self) -> str:
        return "package_info"

    @property
    def description(self) -> str:
        return (
            "结构化 API 直连工具 — 直接查询 GitHub/npm/PyPI 的官方 API，返回 JSON。"
            "比 web_fetch 精准（无正文提取损失）。"
            "支持 action:\n"
            "  github_repo(owner, repo): 查 GitHub 仓库信息（stars/description/默认分支）\n"
            "  github_file(owner, repo, path, ref?): 拿仓库内文件的原始内容\n"
            "  github_issues(owner, repo, state?): 查 issue 列表（state: open/closed/all）\n"
            "  npm_info(package): 查 npm 包信息（版本/依赖/README）\n"
            "  pypi_info(package): 查 PyPI 包信息（版本/依赖/简介）"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["github_repo", "github_file", "github_issues",
                             "npm_info", "pypi_info"],
                    "description": "要执行的查询",
                },
                "owner": {
                    "type": "string",
                    "description": "[github_*] 仓库 owner",
                },
                "repo": {
                    "type": "string",
                    "description": "[github_*] 仓库名",
                },
                "path": {
                    "type": "string",
                    "description": "[github_file] 文件路径，如 'src/index.ts'",
                },
                "ref": {
                    "type": "string",
                    "description": "[github_file] 分支/tag/commit，默认 main",
                    "default": "main",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "[github_issues] issue 状态，默认 open",
                    "default": "open",
                },
                "package": {
                    "type": "string",
                    "description": "[npm_info/pypi_info] 包名",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs["action"]

        try:
            if action == "github_repo":
                return await self._github_repo(kwargs.get("owner", ""), kwargs.get("repo", ""))
            elif action == "github_file":
                return await self._github_file(
                    kwargs.get("owner", ""), kwargs.get("repo", ""),
                    kwargs.get("path", ""), kwargs.get("ref", "main"),
                )
            elif action == "github_issues":
                return await self._github_issues(
                    kwargs.get("owner", ""), kwargs.get("repo", ""),
                    kwargs.get("state", "open"),
                )
            elif action == "npm_info":
                return await self._npm_info(kwargs.get("package", ""))
            elif action == "pypi_info":
                return await self._pypi_info(kwargs.get("package", ""))
            else:
                return ToolResult(output=f"未知 action: {action}", is_error=True)
        except asyncio.TimeoutError:
            return ToolResult(output=f"请求超时 ({_TIMEOUT}s)", is_error=True)
        except Exception as e:
            return ToolResult(output=f"查询失败: {e}", is_error=True)

    # ── GitHub ──

    async def _github_repo(self, owner: str, repo: str) -> ToolResult:
        if not owner or not repo:
            return ToolResult(output="github_repo 需要 owner 和 repo 参数", is_error=True)

        url = f"https://api.github.com/repos/{owner}/{repo}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_github_headers(),
                                   timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
                if resp.status == 404:
                    return ToolResult(output=f"仓库不存在: {owner}/{repo}", is_error=True)
                if resp.status != 200:
                    body = await resp.text(errors="replace")
                    return ToolResult(output=f"GitHub API 错误 {resp.status}: {body[:200]}", is_error=True)

                data = await resp.json()

        # 提取关键字段（原始 JSON 字段太多）
        result = {
            "full_name": data.get("full_name"),
            "description": data.get("description"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "default_branch": data.get("default_branch"),
            "language": data.get("language"),
            "license": (data.get("license") or {}).get("spdx_id"),
            "homepage": data.get("homepage"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "topics": data.get("topics", []),
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return ToolResult(output=f"GitHub 仓库信息:\n{_truncate(text)}")

    async def _github_file(self, owner: str, repo: str, path: str, ref: str) -> ToolResult:
        """获取 GitHub 仓库文件内容 — 三层降级保证可用性。

        降级顺序（针对国内网络环境优化）：
          1. jsdelivr CDN (cdn.jsdelivr.net/gh/) — 国内有节点，最快最稳
          2. GitHub Contents API (api.github.com) — 已测通稳定，但 1MB 限制 + base64
          3. raw.githubusercontent.com — 国内不稳定，最后兜底

        每层超时 10s，任一成功即返回，全部失败才报错。
        """
        if not owner or not repo or not path:
            return ToolResult(output="github_file 需要 owner, repo, path 参数", is_error=True)

        errors: list[str] = []

        async with aiohttp.ClientSession() as session:
            # ── 第 1 层: jsdelivr CDN ──
            # 格式: https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}
            # 优点: 国内有节点，速度快，无文件大小限制
            # 限制: 仅公开仓库，ref 必须是真实存在的分支/tag/commit
            jsdelivr_url = f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}"
            try:
                async with session.get(
                    jsdelivr_url,
                    headers={"User-Agent": "GanggeBot/1.0", "Accept": "text/plain,*/*"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        content = await resp.text(errors="replace")
                        if content and len(content.strip()) > 0:
                            text = f"[来源: {jsdelivr_url} | via jsdelivr]\n{content}"
                            return ToolResult(output=_truncate(text))
                    elif resp.status == 404:
                        # jsdelivr 404 说明路径/分支确实不存在，不用再试其他源
                        return ToolResult(
                            output=f"文件不存在: {owner}/{repo}@{ref}/{path}",
                            is_error=True,
                        )
                    else:
                        errors.append(f"jsdelivr HTTP {resp.status}")
            except asyncio.TimeoutError:
                errors.append("jsdelivr 超时")
            except Exception as e:
                errors.append(f"jsdelivr 异常: {e}")

            # ── 第 2 层: GitHub Contents API ──
            # 格式: GET api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}
            # 优点: 走 api.github.com（已测通稳定），返回 JSON 含 base64 content
            # 限制: 文件最大 1MB（API 限制），超过会返回 encoding="none"
            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
            try:
                async with session.get(
                    api_url,
                    headers=_github_headers(),
                    params={"ref": ref},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        encoding = data.get("encoding", "")
                        content_b64 = data.get("content", "")
                        if encoding == "base64" and content_b64:
                            import base64
                            content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                            text = f"[来源: {api_url} | via github-api]\n{content}"
                            return ToolResult(output=_truncate(text))
                        elif encoding == "none":
                            errors.append("github-api: 文件超过 1MB 限制")
                        else:
                            errors.append(f"github-api: 未知 encoding={encoding}")
                    elif resp.status == 404:
                        return ToolResult(
                            output=f"文件不存在: {owner}/{repo}@{ref}/{path}",
                            is_error=True,
                        )
                    else:
                        body = await resp.text(errors="replace")
                        errors.append(f"github-api HTTP {resp.status}: {body[:100]}")
            except asyncio.TimeoutError:
                errors.append("github-api 超时")
            except Exception as e:
                errors.append(f"github-api 异常: {e}")

            # ── 第 3 层: raw.githubusercontent.com（最后兜底）──
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
            try:
                async with session.get(
                    raw_url,
                    headers={**_github_headers(), "Accept": "text/plain, */*"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        content = await resp.text(errors="replace")
                        text = f"[来源: {raw_url} | via raw]\n{content}"
                        return ToolResult(output=_truncate(text))
                    elif resp.status == 404:
                        return ToolResult(
                            output=f"文件不存在: {owner}/{repo}@{ref}/{path}",
                            is_error=True,
                        )
                    else:
                        errors.append(f"raw HTTP {resp.status}")
            except asyncio.TimeoutError:
                errors.append("raw 超时")
            except Exception as e:
                errors.append(f"raw 异常: {e}")

        # 三层全部失败
        return ToolResult(
            output=(
                f"获取文件失败，三层降级全部不可用:\n"
                f"  文件: {owner}/{repo}@{ref}/{path}\n"
                f"  尝试: {'; '.join(errors)}\n"
                f"建议: 检查网络连接，或稍后重试。"
            ),
            is_error=True,
        )

    async def _github_issues(self, owner: str, repo: str, state: str) -> ToolResult:
        if not owner or not repo:
            return ToolResult(output="github_issues 需要 owner 和 repo 参数", is_error=True)

        url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        params = {"state": state, "per_page": 20}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_github_headers(), params=params,
                                   timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
                if resp.status != 200:
                    body = await resp.text(errors="replace")
                    return ToolResult(output=f"GitHub API 错误 {resp.status}: {body[:200]}", is_error=True)

                data = await resp.json()

        if not data:
            return ToolResult(output=f"无 {state} issue ({owner}/{repo})")

        lines = [f"GitHub Issues ({owner}/{repo}, state={state}, {len(data)} 条):\n"]
        for issue in data[:20]:
            # 跳过 PR（GitHub API 把 PR 也算 issue）
            if "pull_request" in issue:
                continue
            num = issue.get("number")
            title = issue.get("title", "")
            state_emoji = "🟢" if issue.get("state") == "open" else "🔴"
            labels = ", ".join(l["name"] for l in issue.get("labels", []))
            label_str = f" [{labels}]" if labels else ""
            lines.append(f"  {state_emoji} #{num} {title}{label_str}")

        return ToolResult(output=_truncate("\n".join(lines)))

    # ── npm ──

    async def _npm_info(self, package: str) -> ToolResult:
        if not package:
            return ToolResult(output="npm_info 需要 package 参数", is_error=True)

        url = f"https://registry.npmjs.org/{package}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
                if resp.status == 404:
                    return ToolResult(output=f"npm 包不存在: {package}", is_error=True)
                if resp.status != 200:
                    return ToolResult(output=f"npm registry 错误 {resp.status}", is_error=True)

                data = await resp.json()

        # 提取关键字段（npm 返回的数据非常大）
        latest_version = data.get("dist-tags", {}).get("latest", "")
        latest_info = data.get("versions", {}).get(latest_version, {})

        result = {
            "name": data.get("name"),
            "latest_version": latest_version,
            "description": latest_info.get("description") or data.get("description"),
            "homepage": latest_info.get("homepage"),
            "license": latest_info.get("license"),
            "dependencies": latest_info.get("dependencies", {}),
            "dev_dependencies": latest_info.get("devDependencies", {}),
            "keywords": latest_info.get("keywords", []),
            "maintainers": [m.get("name", "") for m in data.get("maintainers", [])],
            "versions_count": len(data.get("versions", {})),
            "created": data.get("time", {}).get("created"),
            "modified": data.get("time", {}).get("modified"),
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return ToolResult(output=f"npm 包信息:\n{_truncate(text)}")

    # ── PyPI ──

    async def _pypi_info(self, package: str) -> ToolResult:
        if not package:
            return ToolResult(output="pypi_info 需要 package 参数", is_error=True)

        url = f"https://pypi.org/pypi/{package}/json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
                if resp.status == 404:
                    return ToolResult(output=f"PyPI 包不存在: {package}", is_error=True)
                if resp.status != 200:
                    return ToolResult(output=f"PyPI 错误 {resp.status}", is_error=True)

                data = await resp.json()

        info = data.get("info", {})
        result = {
            "name": info.get("name"),
            "version": info.get("version"),
            "summary": info.get("summary"),
            "description": (info.get("description") or "")[:2000],  # 限制描述长度
            "homepage": info.get("home_page") or info.get("project_url"),
            "license": info.get("license"),
            "requires_python": info.get("requires_python"),
            "dependencies": info.get("requires_dist", []),
            "classifiers": info.get("classifiers", [])[:10],  # 只保留前 10 个
            "author": info.get("author"),
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return ToolResult(output=f"PyPI 包信息:\n{_truncate(text)}")
