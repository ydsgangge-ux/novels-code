"""委派的超时语义：按「沉默」判死，不按总时长。

这条规则是用户提的：跑大任务要很久，只要它还在输出就不该被砍；真正该判死的是
"进程还活着却一个字都不吐"。所以这里用真实子进程验证三种情形。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from gangge.layer4_tools.qoder_cli import runner
from gangge.layer4_tools.qoder_cli.config import QoderConfig
from gangge.layer4_tools.qoder_cli.spec import DelegateSpec

ENV = dict(os.environ)


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_keeps_running_while_it_keeps_talking(tmp_path: Path):
    """总耗时超过 idle 阈值，但每 0.3s 有输出 → 不许判死。"""
    code = "import time\nfor i in range(8):\n print(i, flush=True)\n time.sleep(0.3)\n"
    run = runner._run_process(
        _python(code), tmp_path, ENV, idle_timeout_s=1, poll_s=0.05
    )

    assert run.reason == ""
    assert run.exit_code == 0
    assert run.elapsed_s > 1.0, "这条进程本来就跑得比空闲阈值久"
    assert "7" in run.stdout


def test_silent_process_is_killed(tmp_path: Path):
    """一个字都不吐超过阈值 → 判死，并且真的杀掉。"""
    run = runner._run_process(
        _python("import time; time.sleep(30)"),
        tmp_path,
        ENV,
        idle_timeout_s=1,
        poll_s=0.05,
    )

    assert run.reason == "idle"
    assert run.exit_code != 0
    assert run.elapsed_s < 10, "不该等满 30 秒"


def test_hard_cap_still_available_when_asked(tmp_path: Path):
    """想给整件事设天花板仍然可以：总时长上限优先。"""
    code = "import time\nfor i in range(40):\n print(i, flush=True)\n time.sleep(0.2)\n"
    run = runner._run_process(
        _python(code), tmp_path, ENV, idle_timeout_s=5, hard_timeout_s=1, poll_s=0.05
    )

    assert run.reason == "hard"
    # 不卡具体耗时：杀进程树（taskkill）在负载高的机器上本身要几秒
    assert run.exit_code != 0


def test_live_output_is_trimmed_to_the_tail(tmp_path: Path, monkeypatch):
    """长任务输出几十 MB，内存里只留尾巴，但结论那几行必须在。"""
    monkeypatch.setattr(runner, "MAX_LIVE_OUTPUT", 1000)
    code = "for i in range(80):\n print('y' * 100, flush=True)\nprint('FINAL-MARK', flush=True)\n"
    run = runner._run_process(_python(code), tmp_path, ENV, idle_timeout_s=5, poll_s=0.05)

    assert run.reason == ""
    assert len(run.stdout) < 3000, "超过上限太多，说明没裁剪"
    assert "FINAL-MARK" in run.stdout, "裁掉的应该是开头，不是结尾"


def test_defaults_are_idle_based(monkeypatch):
    monkeypatch.delenv("QODER_TIMEOUT_S", raising=False)
    monkeypatch.delenv("QODER_IDLE_TIMEOUT_S", raising=False)
    cfg = QoderConfig.from_env()

    assert cfg.idle_timeout == 600, "10 分钟没输出才判死"
    assert cfg.timeout == 0, "总时长默认不限，大任务不该被总时长砍"


def test_spec_rejects_negative_idle(monkeypatch):
    spec = DelegateSpec.from_dict(
        {"goal": "x", "mode": "analyze", "idle_timeout_s": -1}
    )
    assert any("idle_timeout_s" in p for p in spec.validate())
