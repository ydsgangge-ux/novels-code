"""委派契约：把"做到什么程度"变成机器可判的东西。

委派最怕两件事：模型自己说"完成了"，以及改动跑到范围之外。这里用三个字段
把两者都堵住：

* ``scope``  —— 允许改的文件（白名单，git 变更做集合比较）
* ``dod``    —— 完成定义，一组可执行命令 + 期望退出码
* ``current_state`` —— 动手前的基线（命令 + 期望输出），让远端自己先确认前提

spec 是**主 agent 写的**，不是远端写的；远端只负责满足它。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Iterable

VALID_MODES = ("analyze", "edit", "verify")


def _shell_note() -> str:
    """告诉远端验收命令在哪个壳里跑。

    不写清楚它就会按 Linux 习惯写 ``test -f``，而 Windows 侧是 PowerShell ——
    命令本身没错，壳不支持，结果把正确产出判成 partial，还顺带骗过一轮结论。
    """
    if os.name == "nt":
        return (
            "验收命令在 **Windows PowerShell** 中执行：不要写 `test -f` / `grep` / `touch`，"
            "改用 `Test-Path x` / `Select-String -Path x -Pattern y` / `New-Item`。"
        )
    return "验收命令在 **bash** 中执行，可以直接用 `test -f` / `grep`。"


@dataclass
class CheckSpec:
    """一条可执行的验收命令。"""

    cmd: str
    expect_exit: int = 0
    timeout_s: int = 300

    @classmethod
    def from_dict(cls, data: Any) -> CheckSpec:
        if isinstance(data, str):
            return cls(cmd=data)
        if not isinstance(data, dict):
            raise ValueError("dod 条目必须是字符串或 {cmd, expect_exit}")
        cmd = str(data.get("cmd") or "").strip()
        if not cmd:
            raise ValueError("dod 条目缺少 cmd")
        return cls(
            cmd=cmd,
            expect_exit=int(data.get("expect_exit", 0)),
            timeout_s=int(data.get("timeout_s", 300)),
        )


@dataclass
class DelegateSpec:
    """一次委派要做什么、改哪里、怎么算完成。"""

    goal: str
    mode: str = "edit"
    scope: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    dod: tuple[CheckSpec, ...] = ()
    current_state: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    max_turns: int = 0
    timeout_s: int = 0

    # ── 校验 ────────────────────────────────────────────────
    def validate(self) -> list[str]:
        """返回问题列表（空 = 通过）。调用方决定是拒绝还是只告警。"""
        problems: list[str] = []
        if not self.goal.strip():
            problems.append("goal 不能为空")
        if self.mode not in VALID_MODES:
            problems.append(f"mode 只能是 {'/'.join(VALID_MODES)}")
        if self.mode in ("edit", "verify") and not self.scope:
            problems.append("edit/verify 模式必须给出 scope（允许改动的文件白名单）")
        if self.mode == "analyze" and any(c.cmd for c in self.dod):
            problems.append("analyze 模式不该带 dod（它不改东西，没东西可验）")
        if self.max_turns < 0 or self.timeout_s < 0:
            problems.append("max_turns / timeout_s 不能为负")
        return problems

    # ── 提示词 ──────────────────────────────────────────────
    def prompt(self, *, can_run_commands: bool = False) -> str:
        """渲染成给远端 CLI 的指令。

        刻意写得像工单而不是像聊天：范围、禁止项、验收命令都写死，远端越少
        发挥空间，主 agent 事后要读的东西就越少。

        ``can_run_commands`` 反映**当前权限档位是否真的允许跑命令**。不给它
        Bash 却要求它"自己跑一遍验收"，它会连着几轮撞权限墙，最后把"请批准我
        执行"当成结论回传——白烧轮数，还污染摘要。
        """
        lines = [f"# 任务\n{self.goal.strip()}"]

        if self.scope:
            lines.append("# 允许修改的文件\n" + "\n".join(f"- {p}" for p in self.scope))
            lines.append("只改上面列出的文件。需要改别的，停下来在结论里说明，不要动手。")

        if self.forbidden:
            lines.append("# 禁止修改\n" + "\n".join(f"- {p}" for p in self.forbidden))

        if self.current_state:
            lines.append("# 动手前的现状")
            for cmd, expect in self.current_state.items():
                lines.append(f"- `{cmd}` → {expect}")

        if self.dod:
            lines.append("# 完成定义（由调用方执行判定）")
            for chk in self.dod:
                lines.append(f"- `{chk.cmd}` 退出码应为 {chk.expect_exit}")
            if can_run_commands:
                lines.append("完成前请自己跑一遍这些命令，失败就继续修，不要报完成。")
            else:
                lines.append(
                    "当前权限不允许执行命令，**不要尝试运行**这些命令，"
                    "调用方会自己执行并判定。也不要因此请求批准或把未验证当失败。"
                )
            lines.append(_shell_note())

        lines.append(
            "# 交付要求\n"
            "- 不要 git commit / push，不要改 CI 配置，不要新增依赖除非任务要求。\n"
            "- 结论只写三段：改了什么、你还验证了什么、还有什么风险没覆盖。\n"
            "- 结论用**中文**，不要输出英文，不要写“请批准我执行”这类交互话术。"
        )
        if self.notes.strip():
            lines.append(f"# 补充\n{self.notes.strip()}")
        return "\n\n".join(lines)

    # ── 范围合规 ────────────────────────────────────────────
    def violations(self, changed: Iterable[str]) -> list[str]:
        """改动文件里越界的部分。``scope`` 为空时视为不检查。"""
        if not self.scope:
            return []
        allowed = [p.replace("\\", "/").lstrip("./") for p in self.scope]
        bad: list[str] = []
        for raw in changed:
            path = raw.replace("\\", "/").lstrip("./")
            if not path:
                continue
            if any(fnmatch(path, pat) or path == pat or path.startswith(pat.rstrip("/") + "/")
                   for pat in allowed):
                continue
            if any(fnmatch(path, pat) or path == pat for pat in self.forbidden):
                bad.append(path)
                continue
            bad.append(path)
        return bad

    # ── 构造 ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DelegateSpec:
        if not isinstance(data, dict):
            raise ValueError("spec 必须是对象")
        scope = data.get("scope") or ()
        forbidden = data.get("forbidden") or ()
        if isinstance(scope, str):
            scope = (scope,)
        if isinstance(forbidden, str):
            forbidden = (forbidden,)
        raw_state = data.get("current_state") or {}
        if isinstance(raw_state, str):
            raw_state = {}
        return cls(
            goal=str(data.get("goal") or ""),
            mode=str(data.get("mode") or "edit").lower(),
            scope=tuple(str(s) for s in scope),
            forbidden=tuple(str(s) for s in forbidden),
            dod=tuple(CheckSpec.from_dict(d) for d in (data.get("dod") or ())),
            current_state={str(k): str(v) for k, v in raw_state.items()},
            notes=str(data.get("notes") or ""),
            max_turns=int(data.get("max_turns") or 0),
            timeout_s=int(data.get("timeout_s") or 0),
        )
