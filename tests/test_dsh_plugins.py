"""阶段 1：DSH 插件分类与支持率报表。

这些用例锁定的是**分类判据**，而不是 DSH 的上游格式细节：
凡是我们声称"能走通"的包，必须能落到 mcp / skill 两类之一；凡是要宿主服务的，
必须落进 unsupported 并带上原因——否则支持率报表就是在自我欺骗。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gangge.layer4_tools.dsh_plugins import (
    PluginKind,
    InstallError,
    build_report,
    build_server_configs,
    classify_package,
    export_config,
    flatten_patch_rows,
    install,
    main,
    scan,
)

MCP_PATCH = """
- insert:
    - id: mcp
      name: dsh-mcp-client
      config:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_TOKEN: "$GITHUB_TOKEN"
"""

HOSTED_ONLY_PATCH = """
- insert:
    - id: fs-helper
      name: dsh-plugin-fs-helper
      config:
        root: "."
"""

MIXED_PATCH = """
- insert:
    - id: mcp
      name: dsh-mcp-client
      config:
        command: node
        args: ["./server.js"]
    - id: watcher
      name: dsh-plugin-watcher
      config:
        debounce: 200
"""

SKILL_MD = """---
name: code-review
description: 审查 diff 并给出可执行修改建议
---

# Code Review
按文件逐个看，先看正确性再看风格。
"""


def make_package(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    patch: str | None = None,
    patch_name: str = "cordis.patch.yml",
    skill: str | None = None,
    skills: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
    engines: dict[str, str] | None = None,
    declared_patch: bool = True,
    deps: dict[str, str] | None = None,
    keywords: list[str] | None = None,
    bin_field: dict[str, str] | None = None,
    flat: bool = False,
) -> Path:
    pkg = root if flat else root / name.replace("/", "__")
    pkg.mkdir(parents=True, exist_ok=True)

    manifest: dict = {"name": name, "version": version}
    if engines:
        manifest["engines"] = engines
    if deps:
        manifest["dependencies"] = deps
    if keywords:
        manifest["keywords"] = keywords
    if bin_field:
        manifest["bin"] = bin_field
    if patch and declared_patch:
        manifest["dsh"] = {"bundle": {"patch": patch_name}}
    (pkg / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    if patch:
        patch_path = pkg / patch_name
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch, encoding="utf-8")
    if skill:
        (pkg / "SKILL.md").write_text(skill, encoding="utf-8")
    for sub, body in (skills or {}).items():
        target = pkg / "skills" / sub
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(body, encoding="utf-8")
    for rel, body in (extra_files or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return pkg


# ── 分类：MCP ────────────────────────────────────────

def test_mcp_client_row_becomes_server_spec(tmp_path):
    pkg = make_package(tmp_path, "mcp-github", patch=MCP_PATCH, engines={"dsh": ">=0.2.0-rc.2"})
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP
    assert info.usable
    assert len(info.mcp_servers) == 1

    spec = info.mcp_servers[0]
    assert spec.transport == "stdio"
    assert spec.command == "npx"
    assert spec.args == ["-y", "@modelcontextprotocol/server-github"]
    assert spec.env == {"GITHUB_TOKEN": "$GITHUB_TOKEN"}
    assert spec.source_row == "mcp"
    # engines 只是读出来记录，不参与任何判断
    assert info.engines == {"dsh": ">=0.2.0-rc.2"}


def test_server_spec_converts_to_config_json_entry(tmp_path):
    pkg = make_package(tmp_path, "mcp-github", patch=MCP_PATCH)
    entry = classify_package(pkg).mcp_servers[0].to_config_dict()

    assert entry["enabled"] is True
    assert entry["transport"] == "stdio"
    assert entry["command"] == "npx"
    assert "url" not in entry
    json.dumps(entry)  # 必须能直接塞进 .gangge/mcp_servers.json


def test_multiple_servers_in_one_row(tmp_path):
    patch = """
- insert:
    - id: mcp
      name: dsh-mcp-client
      config:
        servers:
          - name: github
            command: npx
            args: ["-y", "server-github"]
          - name: fs
            command: npx
            args: ["-y", "server-fs"]
"""
    pkg = make_package(tmp_path, "multi", patch=patch)
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP
    assert [s.name for s in info.mcp_servers] == ["github", "fs"]
    assert all(s.command == "npx" for s in info.mcp_servers)


def test_url_only_server_is_sse(tmp_path):
    patch = """
- insert:
    - id: mcp
      name: dsh-mcp-client
      config:
        url: http://127.0.0.1:3000/sse
"""
    pkg = make_package(tmp_path, "remote", patch=patch)
    spec = classify_package(pkg).mcp_servers[0]

    assert spec.transport == "sse"
    assert spec.url == "http://127.0.0.1:3000/sse"
    assert spec.to_config_dict()["url"] == "http://127.0.0.1:3000/sse"


def test_patch_declared_only_in_manifest_field(tmp_path):
    """补丁文件不叫 cordis.patch.yml，靠 package.json 里的 dsh.bundle.patch 找到。"""
    pkg = make_package(tmp_path, "odd-name", patch=MCP_PATCH, patch_name="config/bundle.yml")
    info = classify_package(pkg)

    assert info.patch_file == "bundle.yml"
    assert info.kind is PluginKind.MCP


def test_mcp_row_without_client_in_name_but_server_shaped_config(tmp_path):
    patch = """
- insert:
    - id: bridge
      name: some-tool-bridge
      config:
        command: python
        args: ["server.py"]
"""
    pkg = make_package(tmp_path, "shaped", patch=patch)
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP
    assert info.mcp_servers[0].command == "python"


def test_mixed_patch_is_mcp_but_flags_unhosted_rows(tmp_path):
    pkg = make_package(tmp_path, "mixed", patch=MIXED_PATCH)
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP
    assert info.unhosted_rows == ["dsh-plugin-watcher"]
    assert any("非 MCP 行" in r for r in info.reasons), "部分可用的插件必须说明缺口"


# ── 分类：SKILL ──────────────────────────────────────

def test_skill_package_reads_frontmatter(tmp_path):
    pkg = make_package(tmp_path, "skill-review", skill=SKILL_MD)
    info = classify_package(pkg)

    assert info.kind is PluginKind.SKILL
    assert info.usable
    assert [s.name for s in info.skills] == ["code-review"]
    assert "diff" in info.skills[0].description
    assert info.skills[0].path.name == "SKILL.md"


def test_skill_bundle_with_multiple_skills(tmp_path):
    pkg = make_package(
        tmp_path,
        "skill-bundle",
        skills={
            "alpha": "---\nname: alpha\ndescription: 第一个\n---\n",
            "beta": "---\nname: beta\ndescription: 第二个\n---\n",
        },
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.SKILL
    assert sorted(s.name for s in info.skills) == ["alpha", "beta"]


def test_skill_without_frontmatter_falls_back_to_dir_name(tmp_path):
    pkg = make_package(tmp_path, "bare", skills={"unnamed": "没有 frontmatter 的正文\n"})
    info = classify_package(pkg)

    assert [s.name for s in info.skills] == ["unnamed"]
    assert info.skills[0].description == ""


# ── 分类：UNSUPPORTED ────────────────────────────────

def test_code_only_package_is_unsupported(tmp_path):
    pkg = make_package(
        tmp_path,
        "ui-plugin",
        extra_files={"index.js": "export function apply(ctx) {}\n"},
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.UNSUPPORTED
    assert not info.usable
    assert info.reasons and "Cordis" in info.reasons[0]


def test_patch_with_only_hosted_rows_is_unsupported_and_names_them(tmp_path):
    pkg = make_package(tmp_path, "fs-helper", patch=HOSTED_ONLY_PATCH)
    info = classify_package(pkg)

    assert info.kind is PluginKind.UNSUPPORTED
    assert info.unhosted_rows == ["dsh-plugin-fs-helper"]
    assert any("dsh-plugin-fs-helper" in r for r in info.reasons)


def test_broken_patch_degrades_instead_of_raising(tmp_path):
    pkg = make_package(tmp_path, "broken", patch="- insert: [oops\n", patch_name="cordis.patch.yml")
    info = classify_package(pkg)

    assert info.kind is PluginKind.UNSUPPORTED
    assert info.reasons


def test_native_mcp_server_package_is_mcp_without_any_patch(tmp_path):
    """真实生态里大量包自己就是 MCP server，没有任何补丁清单。"""
    pkg = make_package(
        tmp_path,
        "@modelcontextprotocol/server-github",
        deps={"@modelcontextprotocol/sdk": "^1.0.0"},
        bin_field={"mcp-server-github": "dist/index.js"},
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP
    assert info.mcp_servers[0].name == "server-github"
    assert info.mcp_servers[0].command == "npx"
    assert info.mcp_servers[0].args == ["-y", "@modelcontextprotocol/server-github"]
    assert any("推断" in r for r in info.reasons), "推断出来的必须有标注，方便人工复核"


def test_mcp_keyword_also_counts_as_evidence(tmp_path):
    pkg = make_package(tmp_path, "some-server", keywords=["mcp", "filesystem"])
    assert classify_package(pkg).kind is PluginKind.MCP


def test_plain_cli_tool_with_bin_is_not_mistaken_for_mcp_server(tmp_path):
    pkg = make_package(
        tmp_path,
        "prettier-ish",
        bin_field={"fmt": "bin/fmt.js"},
        extra_files={"bin/fmt.js": "#!/usr/bin/env node\n"},
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.UNSUPPORTED, "误判成 MCP server 比漏判更难查"


def test_explicit_skill_beats_native_server_inference(tmp_path):
    pkg = make_package(
        tmp_path,
        "both",
        skill=SKILL_MD,
        deps={"@modelcontextprotocol/sdk": "^1.0.0"},
    )
    assert classify_package(pkg).kind is PluginKind.SKILL


def test_real_dsh_mcp_client_row_is_extracted(tmp_path):
    """真实写法：name 是 @deepseek-ai/dsh-mcp-client，config 里给 command/args/url。"""
    patch = """
- insert:
    - id: mcp-prism
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        command: node
        args: ['./server/prism.js']
"""
    pkg = make_package(tmp_path, "dsh-plugin-prism-design", patch=patch)
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP
    assert info.usable
    spec = info.mcp_servers[0]
    assert spec.command == "node"
    assert spec.source_row == "mcp-prism"
    assert spec.runtime_expr is False


def test_js_expression_command_is_parsed_but_not_usable(tmp_path):
    """实测高频写法：command: !!js ctx.xxx——值是运行时求值，静态拿不到。"""
    patch = """
- insert:
    - id: mcp-cua-driver
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        command: !!js ctx.cuaComputerUse.command
        args: ['--stdio']
"""
    pkg = make_package(tmp_path, "cua-like", patch=patch)
    info = classify_package(pkg)

    assert info.kind is PluginKind.MCP, "MCP 行确实存在，不能因为求值不了就报 unsupported"
    assert not info.usable, "表达式文本写进配置只会得到一条永远连不上的 server"
    assert info.static_servers == []
    assert info.mcp_servers[0].runtime_expr is True
    assert any("运行时表达式" in r for r in info.reasons)


def test_js_expression_elsewhere_in_patch_does_not_break_parsing(tmp_path):
    """!!js 出现在 disabled 里（dsh-llm-provider 的写法）不能让整份文档解析失败。"""
    patch = """
- id: llm-pi-ai
  disabled: !!js "(() => { try { return true } catch { return false } })()"

- insert:
    - id: provider
      name: '@dsh-one/dsh-llm-provider'
"""
    pkg = make_package(tmp_path, "dsh-llm-provider-like", patch=patch)
    info = classify_package(pkg)

    assert info.patch_file == "cordis.patch.yml"
    assert info.unhosted_rows == ["@dsh-one/dsh-llm-provider"]
    assert info.kind is PluginKind.UNSUPPORTED
    assert not any("解析不出" in r for r in info.reasons)


def test_dsh_plugin_with_mcp_keyword_is_never_npx_inferred(tmp_path):
    """实测 8 例误判的根源：关键词带 mcp 的 DSH 插件不是 MCP server。"""
    pkg = make_package(
        tmp_path,
        "dsh-mcp-manager-plus",
        patch="""
- insert:
    - id: mcp-manager-plus
      name: dsh-mcp-manager-plus
""",
        keywords=["mcp", "dsh"],
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.UNSUPPORTED
    assert info.mcp_servers == []
    assert info.unhosted_rows == ["dsh-mcp-manager-plus"]


def test_skill_in_singular_skill_dir_is_found(tmp_path):
    """实测布局之一：包根下的 skill/SKILL.md（单数目录）。"""
    pkg = make_package(
        tmp_path,
        "cua-like-skill",
        extra_files={"skill/SKILL.md": "---\nname: cua\ndescription: 操作 GUI\n---\n正文\n"},
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.SKILL
    assert [s.name for s in info.skills] == ["cua"]


def test_skill_and_runtime_mcp_row_reports_skill_first(tmp_path):
    """有技能可交付时优先交付技能，同时把 MCP 行的缺口写进 reason。"""
    pkg = make_package(
        tmp_path,
        "both-ways",
        patch="""
- insert:
    - id: mcp-x
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        command: !!js ctx.x.command
""",
        extra_files={"skill/SKILL.md": "---\nname: x-skill\ndescription: 说明\n---\n"},
    )
    info = classify_package(pkg)

    assert info.kind is PluginKind.SKILL
    assert info.usable
    assert any("运行时表达式" in r for r in info.reasons)


def test_export_skips_runtime_expression_servers(tmp_path):
    pkg = make_package(
        tmp_path,
        "expr-only",
        patch="""
- insert:
    - id: mcp-x
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        url: !!js ctx.x.url
""",
    )
    info = classify_package(pkg)
    cfg = tmp_path / "mcp_servers.json"

    result = export_config([info], cfg)

    assert result.added == []
    assert not cfg.exists(), "拿不到真值的 server 不能写进配置"


def test_server_name_field_wins_over_name(tmp_path):
    """dsh-mcp-client 用 serverName 做工具命名空间：mcp__<serverName>__<tool>。"""
    patch = """
- insert:
    - id: mcp-zego
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ZEGO
        transport: sse
        url: https://doc-ai.zego.im/mcp/
"""
    pkg = make_package(tmp_path, "dsh-zego-assistant", patch=patch)
    spec = classify_package(pkg).mcp_servers[0]

    assert spec.name == "ZEGO"
    assert spec.transport == "sse"
    assert spec.command == ""


def test_streamable_http_transport_is_not_writable(tmp_path):
    """实测 DSH 用 transport: streamable-http，我们的客户端只认 stdio/sse。"""
    patch = """
- insert:
    - id: mcp-zego
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ZEGO
        transport: streamable-http
        url: https://doc-ai.zego.im/mcp/
"""
    pkg = make_package(tmp_path, "zego-http", patch=patch)
    info = classify_package(pkg)
    cfg = tmp_path / "mcp_servers.json"

    assert info.kind is PluginKind.MCP
    assert not info.usable, "写进配置只会在连接时报不支持的传输类型"
    assert info.static_servers == []
    assert any("transport 不受客户端支持" in r for r in info.reasons)
    assert export_config([info], cfg).added == []


def test_runtime_expr_args_make_server_unwritable(tmp_path):
    """实测 prism：command 是 node，但 args 里是 !!js 计算出来的路径。"""
    patch = """
- insert:
    - id: mcp-prism
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: prism
        transport: stdio
        command: node
        args:
          - !!js ctx.plugin.resolve('./server/index.js')
"""
    pkg = make_package(tmp_path, "prism-like", patch=patch)
    info = classify_package(pkg)

    assert info.mcp_servers[0].command == "node"
    assert info.mcp_servers[0].runtime_expr is True
    assert not info.usable


def test_scan_reads_own_cache_layout(tmp_path):
    """我们自己的缓存布局是 <root>/<name>/<version>/，scan 必须认得。"""
    make_package(tmp_path / "cache" / "pkg-a" / "1.0.0", "pkg-a", version="1.0.0", patch=MCP_PATCH, flat=True)
    make_package(tmp_path / "cache" / "pkg-a" / "2.0.0", "pkg-a", version="2.0.0", patch=MCP_PATCH, flat=True)
    make_package(tmp_path / "cache" / "pkg-b" / "0.1.0", "pkg-b", version="0.1.0", skill=SKILL_MD, flat=True)

    report = build_report(scan([tmp_path / "cache"]))

    assert report.total == 2, "同名多版本只取一个，否则报表会重复计数"
    versions = {p.name: p.version for p in report.plugins}
    assert versions["pkg-a"] == "2.0.0"


def test_missing_package_json_is_reported(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    info = classify_package(empty)

    assert info.kind is PluginKind.UNSUPPORTED
    assert any("package.json" in r for r in info.reasons)


def test_flatten_patch_rows_handles_remove_and_patch_ops(tmp_path):
    doc = tmp_path / "p.yml"
    doc.write_text(
        """
- insert:
    - id: a
      name: plugin-a
- patch:
    - id: a
      config: {x: 1}
- remove: [b]
""",
        encoding="utf-8",
    )
    rows = flatten_patch_rows(doc)

    assert [r["id"] for r in rows] == ["a", "a"]
    assert rows[1]["config"] == {"x": 1}


# ── 扫描与报表 ───────────────────────────────────────

def test_scan_walks_cache_root(tmp_path):
    make_package(tmp_path, "one", patch=MCP_PATCH)
    make_package(tmp_path, "two", skill=SKILL_MD)
    make_package(tmp_path, "three", extra_files={"index.js": ""})

    report = build_report(scan([tmp_path]))

    assert report.total == 3
    assert report.count(PluginKind.MCP) == 1
    assert report.count(PluginKind.SKILL) == 1
    assert report.count(PluginKind.UNSUPPORTED) == 1
    assert report.usable == 2
    assert report.support_rate == pytest.approx(2 / 3)


def test_report_text_states_rate_and_breakdown(tmp_path):
    make_package(tmp_path, "one", patch=MCP_PATCH)
    make_package(tmp_path, "two", skill=SKILL_MD)
    make_package(tmp_path, "three", extra_files={"index.js": ""})

    text = build_report(scan([tmp_path])).format_text()

    assert "支持率 2/3 = 67%" in text
    assert "MCP 1 / SKILL 1 / UNSUPPORTED 1" in text
    assert "1 个 MCP server" in text


def test_report_warns_when_rate_is_low(tmp_path):
    for i in range(5):
        make_package(tmp_path, f"code-{i}", extra_files={"index.js": ""})
    make_package(tmp_path, "one", patch=MCP_PATCH)

    text = build_report(scan([tmp_path])).format_text()

    assert "支持率低于 40%" in text, "支持率低时必须提示退出条件，而不是让人误以为可以继续投 B 方案"


def test_empty_scan_is_not_a_crash(tmp_path):
    report = build_report(scan([tmp_path / "nope"]))
    assert report.total == 0
    assert report.support_rate == 0.0
    assert "合计 0 个" in report.format_text()


# ── 安装 ─────────────────────────────────────────────

def test_install_local_dir_into_cache(tmp_path):
    src = make_package(tmp_path / "src", "mcp-github", patch=MCP_PATCH)
    dest = tmp_path / "cache"

    info = install(src, dest)

    assert info.kind is PluginKind.MCP
    assert (dest / "mcp-github" / "1.0.0" / "package.json").exists()
    assert info.path == (dest / "mcp-github" / "1.0.0").resolve()


def test_install_is_idempotent(tmp_path):
    src = make_package(tmp_path / "src", "mcp-github", patch=MCP_PATCH)
    dest = tmp_path / "cache"

    install(src, dest)
    info = install(src, dest)

    assert info.kind is PluginKind.MCP
    assert len(list((dest / "mcp-github").iterdir())) == 1


def test_install_skips_node_modules_by_default(tmp_path):
    src = make_package(
        tmp_path / "src",
        "with-deps",
        patch=MCP_PATCH,
        extra_files={"node_modules/dep/index.js": "module.exports = 1"},
    )
    dest = tmp_path / "cache"

    install(src, dest)

    assert not (dest / "with-deps" / "1.0.0" / "node_modules").exists()


def test_install_rejects_non_package(tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "readme.txt").write_text("hi", encoding="utf-8")

    with pytest.raises(InstallError):
        install(junk, tmp_path / "cache")


def test_install_rejects_missing_path(tmp_path):
    with pytest.raises(InstallError):
        install(tmp_path / "does-not-exist", tmp_path / "cache")


# ── 导出到 mcp_servers.json ──────────────────────────

def _mcp_info(tmp_path: Path, name: str = "mcp-github", patch: str = MCP_PATCH):
    return classify_package(make_package(tmp_path, name, patch=patch))


def test_export_creates_config(tmp_path):
    info = _mcp_info(tmp_path)
    cfg = tmp_path / ".gangge" / "mcp_servers.json"

    result = export_config([info], cfg)

    assert result.added == ["github"] or result.added == ["mcp-github"]
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["servers"][0]["command"] == "npx"
    assert payload["servers"][0]["enabled"] is True


def test_export_is_idempotent(tmp_path):
    info = _mcp_info(tmp_path)
    cfg = tmp_path / ".gangge" / "mcp_servers.json"

    export_config([info], cfg)
    second = export_config([info], cfg)

    assert second.added == []
    assert second.skipped, "第二次导出应当报告跳过而不是重复写入"


def test_export_does_not_clobber_user_edits_without_force(tmp_path):
    info = _mcp_info(tmp_path)
    cfg = tmp_path / ".gangge" / "mcp_servers.json"
    export_config([info], cfg)

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    server_name = payload["servers"][0]["name"]
    payload["servers"][0]["args"] = ["手写覆盖"]
    cfg.write_text(json.dumps(payload), encoding="utf-8")

    export_config([info], cfg)
    assert json.loads(cfg.read_text(encoding="utf-8"))["servers"][0]["args"] == ["手写覆盖"]

    forced = export_config([info], cfg, force=True)
    assert forced.updated == [server_name]
    assert json.loads(cfg.read_text(encoding="utf-8"))["servers"][0]["args"] != ["手写覆盖"]


def test_export_dry_run_leaves_file_alone(tmp_path):
    info = _mcp_info(tmp_path)
    cfg = tmp_path / "mcp_servers.json"

    result = export_config([info], cfg, dry_run=True)

    assert result.changed
    assert not cfg.exists()


def test_export_renames_conflicting_server_names(tmp_path):
    patch_a = """
- insert:
    - id: mcp
      name: dsh-mcp-client
      config: {name: github, command: npx, args: [a]}
"""
    patch_b = """
- insert:
    - id: mcp
      name: dsh-mcp-client
      config: {name: github, command: npx, args: [b]}
"""
    a = classify_package(make_package(tmp_path, "plug-a", patch=patch_a))
    b = classify_package(make_package(tmp_path, "plug-b", patch=patch_b))

    configs = build_server_configs([a, b])

    assert [c["name"] for c in configs] == ["github", "plug-b-github"], "同名 server 必须被限定，不能静默丢弃"


def test_export_rejects_broken_json(tmp_path):
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text("{not json", encoding="utf-8")

    with pytest.raises(InstallError):
        export_config([_mcp_info(tmp_path)], cfg)


def test_cli_export_writes_config(tmp_path, capsys):
    pkg_root = tmp_path / "pkgs"
    make_package(pkg_root, "mcp-github", patch=MCP_PATCH)
    cfg = tmp_path / "out" / "mcp_servers.json"

    code = main(["export", str(pkg_root), "--config", str(cfg)])

    assert code == 0
    assert "新增" in capsys.readouterr().out
    assert json.loads(cfg.read_text(encoding="utf-8"))["servers"]


# ── CLI ──────────────────────────────────────────────

def test_cli_scan_json(tmp_path, capsys):
    make_package(tmp_path, "one", patch=MCP_PATCH)

    code = main(["scan", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["total"] == 1
    assert payload["plugins"][0]["kind"] == "mcp"
    assert payload["plugins"][0]["mcp_servers"][0]["command"] == "npx"


def test_cli_scan_table_and_empty_result(tmp_path, capsys):
    assert main(["scan", str(tmp_path)]) == 1, "扫不到任何插件时返回非 0，CI 才能发现路径写错"
    assert "合计 0 个" in capsys.readouterr().out
