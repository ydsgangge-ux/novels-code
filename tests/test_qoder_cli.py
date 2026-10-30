"""Qoder CLI 委派层契约测试 —— 不需要装 CLI，也不烧 credit。

覆盖三块：
* 参数翻译（模型/强度/权限 → argv），这是最容易悄悄错的地方；
* 委派契约（scope / dod / prompt），这是"做到什么程度"的定义；
* 结果判定（done / partial / violated / failed），用假可执行文件跑真流程。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gangge.layer4_tools.qoder_cli import runner
from gangge.layer4_tools.qoder_cli.config import (
    DEFAULT_MODEL,
    PERMISSIONS,
    STRENGTHS,
    QoderConfig,
    QoderNotAvailable,
    resolve_executable,
)
from gangge.layer4_tools.qoder_cli.runner import (
    build_argv,
    clip,
    delegate_sync,
    parse_output,
)
from gangge.layer4_tools.qoder_cli.spec import CheckSpec, DelegateSpec


# ── argv 翻译 ───────────────────────────────────────────────────

def test_build_argv_follows_strength_preset(tmp_path: Path):
    cfg = QoderConfig(strength="low", workspace=tmp_path)
    spec = DelegateSpec(goal="改个名字", mode="analyze")
    argv = build_argv(cfg, spec, executable="qoderclicn")
    assert argv[0] == "qoderclicn"
    assert argv[argv.index("-m") + 1] == DEFAULT_MODEL
    assert argv[argv.index("--reasoning-effort") + 1] == STRENGTHS["low"]["effort"]
    assert argv[argv.index("--max-output-tokens") + 1] == STRENGTHS["low"]["max_output_tokens"]
    assert argv[argv.index("-w") + 1] == str(tmp_path.resolve())
    assert "-p" in argv and "--no-session-persistence" in argv


def test_max_turns_only_sent_when_cli_supports_it(tmp_path: Path):
    """v1.1.80 的 --help 里没有 --max-turns，下发会被当成未知参数直接报错。"""
    cfg = QoderConfig(model="DeepSeek-V4-Pro", max_turns=7, workspace=tmp_path)
    spec = DelegateSpec(goal="x", mode="analyze")

    v1180 = build_argv(cfg, spec, executable="qoderclicn",
                       supported=frozenset({"--reasoning-effort", "--print"}))
    assert v1180[v1180.index("-m") + 1] == "DeepSeek-V4-Pro"
    assert "--max-turns" not in v1180

    newer = build_argv(cfg, spec, executable="qoderclicn",
                       supported=frozenset({"--max-turns"}))
    assert newer[newer.index("--max-turns") + 1] == "7"


def test_analyze_mode_cannot_write_even_if_permission_allows(tmp_path: Path):
    cfg = QoderConfig(permission="full", workspace=tmp_path)
    argv = build_argv(cfg, DelegateSpec(goal="看一眼", mode="analyze"), executable="qodercli")
    assert "--permission-mode" not in argv
    assert argv[argv.index("--allowed-tools") + 1] == PERMISSIONS["analyze"]["allowed_tools"]


def test_edit_and_full_permissions_map_to_flags(tmp_path: Path):
    edit = build_argv(
        QoderConfig(permission="edit", workspace=tmp_path),
        DelegateSpec(goal="x", mode="edit", scope=("a.py",)),
        executable="qodercli",
    )
    assert edit[edit.index("--permission-mode") + 1] == "accept_edits"

    full = build_argv(
        QoderConfig(permission="full", workspace=tmp_path),
        DelegateSpec(goal="x", mode="verify", scope=("a.py",)),
        executable="qodercli",
    )
    assert full[full.index("--permission-mode") + 1] == "bypass_permissions"
    assert "--allowed-tools" not in full


# ── 契约 ────────────────────────────────────────────────────────

def test_edit_requires_scope_and_analyze_rejects_dod():
    assert DelegateSpec(goal="改", mode="edit").validate()
    assert DelegateSpec(goal="改", mode="edit", scope=("a.py",)).validate() == []
    assert DelegateSpec(goal="看", mode="analyze", dod=(CheckSpec("pytest"),)).validate()
    assert DelegateSpec(goal="", mode="analyze").validate()


def test_prompt_carries_scope_dod_and_no_commit_rule():
    spec = DelegateSpec(
        goal="加 align 参数",
        mode="edit",
        scope=("src/a.py",),
        dod=(CheckSpec("pytest -q"),),
        current_state={"pytest -q": "17 passed"},
    )
    prompt = spec.prompt()
    assert "加 align 参数" in prompt
    assert "src/a.py" in prompt
    assert "pytest -q" in prompt
    assert "17 passed" in prompt
    assert "不要 git commit" in prompt


def test_prompt_does_not_ask_remote_to_run_checks_without_shell():
    """没有 Bash 权限却要求它自验，会让它连着几轮撞墙并把"请批准"当结论。"""
    spec = DelegateSpec(goal="加测试", mode="edit", scope=("a.py",), dod=(CheckSpec("pytest -q"),))
    offline = spec.prompt(can_run_commands=False)
    assert "不要尝试运行" in offline
    assert "请自己跑一遍" not in offline

    capable = spec.prompt(can_run_commands=True)
    assert "请自己跑一遍" in capable


def test_shell_argv_matches_platform_shell():
    """dod 必须和项目 bash 工具用同一个壳，否则 Windows 上 test/grep 必挂。"""
    argv = runner.shell_argv("echo hi")
    if os.name == "nt":
        assert argv[0] == "powershell"
        assert any("chcp 65001" in part for part in argv)
        assert argv[-1].endswith("echo hi")
    else:
        assert argv[:2] == ["bash", "-c"]


def test_prompt_names_the_shell_used_for_checks():
    prompt = DelegateSpec(
        goal="x", mode="edit", scope=("a.py",), dod=(CheckSpec("echo 1"),)
    ).prompt()
    assert "PowerShell" in prompt if os.name == "nt" else "bash" in prompt


def test_tool_prefixes_partial_output(monkeypatch):
    """模型看到 partial 会直接宣布完成，所以输出必须带上不可忽略的告警。"""
    from gangge.layer3_agent.tools import qoder as qtool
    from gangge.layer4_tools.qoder_cli.runner import DelegationResult

    async def fake_delegate(spec, cfg):
        return DelegationResult(
            status="partial", summary="改了一半", error="验收未全绿: pytest 退出码 1"
        )

    monkeypatch.setattr(qtool, "delegate", fake_delegate)
    res = asyncio.run(qtool.QoderDelegateTool().execute(goal="x", mode="analyze"))
    assert res.is_error is False
    assert "⚠️" in res.output and "partial" in res.output
    assert "不算完成" in res.output


def test_prompt_requires_chinese_answer():
    prompt = DelegateSpec(goal="x", mode="analyze").prompt()
    assert "中文" in prompt


def test_violations_cover_glob_and_directory_prefix():
    spec = DelegateSpec(goal="x", mode="edit", scope=("src/a.py", "tests/*"))
    assert spec.violations(["src/a.py", "tests/test_x.py"]) == []
    assert spec.violations(["src/b.py"]) == ["src/b.py"]
    # 空 scope 表示不检查（analyze 模式）
    assert DelegateSpec(goal="x", mode="analyze").violations(["任意.txt"]) == []


def test_spec_from_dict_accepts_string_dod():
    spec = DelegateSpec.from_dict(
        {"goal": "g", "mode": "edit", "scope": "a.py", "dod": ["pytest -q"]}
    )
    assert spec.scope == ("a.py",)
    assert spec.dod[0].cmd == "pytest -q"
    assert spec.dod[0].expect_exit == 0


# ── 输出解析 ────────────────────────────────────────────────────

def test_parse_output_reads_result_key():
    summary, payload = parse_output(json.dumps({"result": "全绿", "session_id": "s1"}))
    assert summary == "全绿"
    assert payload["session_id"] == "s1"


def test_parse_output_falls_back_to_text():
    summary, payload = parse_output("plain answer\nline2")
    assert summary.startswith("plain answer")
    assert payload == {}


def test_parse_output_reads_last_stream_json_line():
    stream = "\n".join(
        [
            json.dumps({"type": "assistant", "message": "working"}),
            json.dumps({"type": "result", "result": "做完了"}),
        ]
    )
    summary, _ = parse_output(stream)
    assert summary == "做完了"


def test_clip_keeps_head_and_tail():
    text = "A" * 400 + "B" * 400
    out = clip(text, 120)
    assert len(out) <= 140
    assert out.startswith("A")
    assert out.endswith("B")


# ── 可执行文件解析 ──────────────────────────────────────────────

def test_resolve_executable_accepts_path_without_suffix(tmp_path: Path, monkeypatch):
    """用户手填绝对路径时常漏 .cmd 后缀（Windows 装出来就是 qodercli.cmd）。"""
    name = "qodercli.cmd" if os.name == "nt" else "qodercli"
    exe = tmp_path / name
    exe.write_text("echo hi", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert Path(resolve_executable(str(tmp_path / "qodercli"))) == exe


def test_resolve_executable_missing_raises(monkeypatch):
    from gangge.layer4_tools.qoder_cli import config as qconfig

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(qconfig, "_extra_search_dirs", lambda: [])
    with pytest.raises(QoderNotAvailable, match="找不到 Qoder CLI"):
        resolve_executable("qodercli-not-installed")


CN_EXE = Path.home() / ".qoder-cn" / "bin" / "qoderclicn" / "qoderclicn.exe"


@pytest.mark.skipif(not CN_EXE.is_file(), reason="本机未安装 Qoder CLI CN 版")
def test_discovers_cn_install_without_config(monkeypatch):
    """CN 版 installer 不放 PATH，靠搜索目录兜底（无需用户填路径）。"""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert Path(resolve_executable()) == CN_EXE


# ── 端到端（假 CLI） ────────────────────────────────────────────

def _fake_argv(script: str):
    """返回一个写文件/打印的 python 一行程序，冒充 qoderclicn。"""
    return lambda cfg, spec, executable=None, supported=None: [
        sys.executable, "-c", script,
    ]


def _no_help(monkeypatch):
    """跳过 --help 探测（测试里没有真 CLI）。"""
    monkeypatch.setattr(runner, "help_flags", lambda _exe: frozenset())


def test_preflight_stops_when_workspace_looks_wrong(tmp_path: Path, monkeypatch):
    """工作区选错时，别让它在错误目录里"新建"一个同名空文件假装完成。"""
    monkeypatch.setattr(runner, "build_argv", _fake_argv("print('x')"))
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="给 tests 加用例", mode="edit", scope=("tests/test_x.py",)),
        QoderConfig(workspace=tmp_path),  # 这里没有 tests/ 目录
        executable=sys.executable,
    )
    assert result.status == "failed"
    assert "工作区" in result.error
    assert result.files_changed == []


def test_preflight_allows_new_file_in_existing_dir(tmp_path: Path, monkeypatch):
    """新建文件是合法任务：父目录在就放行。"""
    monkeypatch.setattr(runner, "build_argv", _fake_argv("print('{}')"))
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="新建一个模块", mode="edit", scope=("new_mod.py",)),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.status != "failed"
    assert json.loads(result.for_model())["workspace"] == str(tmp_path)


def test_delegate_returns_done_when_checks_pass(tmp_path: Path, monkeypatch):
    script = f"print({json.dumps(json.dumps({'result': '搞定'}))})"
    monkeypatch.setattr(runner, "build_argv", _fake_argv(script))
    _no_help(monkeypatch)
    spec = DelegateSpec(
        goal="x",
        mode="edit",
        scope=("a.py",),
        # 用两边壳都认的命令：bash 与 PowerShell 都有 echo / exit
        dod=(CheckSpec("echo ok"),),
    )
    result = delegate_sync(spec, QoderConfig(workspace=tmp_path), executable=sys.executable)
    assert result.status in ("done", "partial")
    assert result.summary.startswith("搞定")
    assert result.checks and result.checks[0].ok


def test_delegate_marks_partial_when_check_fails(tmp_path: Path, monkeypatch):
    script = f"print({json.dumps(json.dumps({'result': '大概好了'}))})"
    monkeypatch.setattr(runner, "build_argv", _fake_argv(script))
    _no_help(monkeypatch)
    spec = DelegateSpec(
        goal="x",
        mode="edit",
        scope=("a.py",),
        dod=(CheckSpec("exit 3", expect_exit=0),),
    )
    result = delegate_sync(spec, QoderConfig(workspace=tmp_path), executable=sys.executable)
    assert result.status == "partial"
    assert "验收未全绿" in result.error


def test_delegate_trusts_payload_is_error_over_exit_code(tmp_path: Path, monkeypatch):
    """CLI 自报失败时仍可能退出 0（实测结构里有 is_error / subtype）。"""
    payload = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "num_turns": 2, "result": "没搞定"}
    )
    monkeypatch.setattr(runner, "build_argv", _fake_argv(f"print({json.dumps(payload)})"))
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="analyze"),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.status == "failed"
    assert result.turns == 2
    assert "自报失败" in result.error


def test_delegate_records_turn_count(tmp_path: Path, monkeypatch):
    payload = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False,
         "num_turns": 3, "result": "好了"}
    )
    monkeypatch.setattr(runner, "build_argv", _fake_argv(f"print({json.dumps(payload)})"))
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="analyze"),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.status == "done"
    assert result.turns == 3
    assert json.loads(result.for_model())["turns"] == 3


def test_delegate_marks_failed_on_nonzero_exit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runner, "build_argv", _fake_argv('import sys; print("boom", file=sys.stderr); sys.exit(2)')
    )
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="analyze"),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.status == "failed"
    assert result.exit_code == 2


def _git_repo(tmp_path: Path) -> None:
    git = shutil.which("git")
    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([git, "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run([git, "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=tmp_path, check=True)
    subprocess.run([git, "commit", "-qm", "init"], cwd=tmp_path, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_delegate_detects_scope_violation(tmp_path: Path, monkeypatch):
    _git_repo(tmp_path)

    monkeypatch.setattr(
        runner, "build_argv", _fake_argv("open('evil.txt','w').write('x')")
    )
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="edit", scope=("a.py",)),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.status == "violated"
    assert "evil.txt" in result.scope_violations


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_delegate_ignores_preexisting_dirty_files(tmp_path: Path, monkeypatch):
    """工作区本来就脏时，不许把别人的改动算到这次委派头上（否则误判 violated）。"""
    _git_repo(tmp_path)
    (tmp_path / "b.py").write_text("dirty = 1\n", encoding="utf-8")  # 未提交

    monkeypatch.setattr(runner, "build_argv", _fake_argv("print('只读')"))
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="edit", scope=("a.py",)),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.files_changed == []
    assert result.scope_violations == []
    assert result.status == "done"


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_delegate_detects_change_to_already_dirty_file(tmp_path: Path, monkeypatch):
    """本来就脏、这次又被改：状态码不变，必须靠内容哈希发现。"""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("dirty = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        runner, "build_argv", _fake_argv("open('a.py','a').write('more = 2\\n')")
    )
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="edit", scope=("a.py",)),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.files_changed == ["a.py"]
    assert result.status == "done"


def test_delegate_writes_log_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "build_argv", _fake_argv("print('hello')"))
    _no_help(monkeypatch)
    result = delegate_sync(
        DelegateSpec(goal="x", mode="analyze"),
        QoderConfig(workspace=tmp_path),
        executable=sys.executable,
    )
    assert result.log_path and Path(result.log_path).is_file()
    assert ".gangge" in result.log_path


# ── 工具壳 ──────────────────────────────────────────────────────

def test_tool_rejects_edit_without_scope():
    from gangge.layer3_agent.tools.qoder import QoderDelegateTool

    tool = QoderDelegateTool()
    res = asyncio.run(tool.execute(goal="随便改点东西", mode="edit"))
    assert res.is_error
    assert "scope" in res.output


def test_tool_schema_is_valid_and_registered():
    from gangge.layer3_agent.tools.qoder import QoderDelegateTool
    from gangge.layer3_agent.tools.registry import (
        AGENT_PROFILES,
        PROFILE_CORE,
        create_tool_registry,
    )

    tool = QoderDelegateTool()
    assert tool.name == "qoder_delegate"
    assert tool.input_schema["required"] == ["goal"]
    assert "qoder_delegate" in AGENT_PROFILES["coding"]["tools"]
    assert "qoder_delegate" in PROFILE_CORE["coding"]

    registry = create_tool_registry(load_plugins=False)
    assert "qoder_delegate" in registry


def test_one_plus_one_equals_two():
    assert 1 + 1 == 2
