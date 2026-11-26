"""针对一次真实长任务暴露出的四类浪费。

来源：龟壳算卦那个任务（60 轮、¥0.66、三轮全部未完成）。日志里能直接读出：

* 收尾说"最后一次验证未通过 lint 行 409"，下一轮 ``lint_check`` 直接通过
  —— 验证结论过期；
* 模型把带行号的 read_file 输出写回源码，428 行文件报语法错误，然后整文件
  重写一遍 —— 行号前缀污染；
* 连着 4 轮读同一段代码，内容没变却每轮整段进上下文 —— 重复读取；
* 60 轮里 17 轮是 429 重试，照样吃掉轮次配额。

费用那条单独说：只有内置表里的模型算得出钱，换个模型就空白。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from gangge.layer3_agent.loop import AgenticLoop
from gangge.layer3_agent.tools.file_ops import looks_like_numbered_dump
from gangge.pricing import estimate, estimate_cost, reset_custom_cache


# ── 行号前缀污染 ──
def test_numbered_dump_is_detected():
    dump = "\n".join(f"{i}: x{i} = {i}" for i in range(1, 7))
    assert looks_like_numbered_dump(dump), "带行号的回写必须被拦住"


def test_normal_code_is_not_flagged():
    code = "a = 1\nb = 2\nc = 3\ndef f():\n    return a\n"
    assert not looks_like_numbered_dump(code)


def test_yaml_and_dicts_are_not_flagged():
    text = "name: demo\nversion: 1.0\nports: 8080\ntags: [a, b]\n"
    assert not looks_like_numbered_dump(text), "`key: value` 不是行号前缀"


def test_short_snippet_is_not_flagged():
    assert not looks_like_numbered_dump("1: only one line"), "太短的片段误报率高，不拦"


# ── 重复读取 ──
def _bare_loop() -> AgenticLoop:
    """只测判重逻辑，绕开完整构造。"""
    loop = AgenticLoop.__new__(AgenticLoop)
    loop._recent_reads = {}
    return loop


def test_second_identical_read_is_skipped(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    loop = _bare_loop()
    args = {"path": str(target), "offset": 0, "limit": 50}

    assert loop._repeated_read_note("read_file", args, 0) == "", "第一次当然要读"
    loop._remember_read(args, 0, "x = 1\n")

    note = loop._repeated_read_note("read_file", args, 1)
    assert "完全相同" in note
    assert "x = 1" in note, "要让模型看见上次的内容，而不是只说'省略了'"


def test_read_after_edit_is_allowed(tmp_path):
    target = tmp_path / "b.py"
    target.write_text("x = 1\n", encoding="utf-8")
    loop = _bare_loop()
    args = {"path": str(target)}

    loop._remember_read(args, 0, "x = 1\n")

    import os
    import time

    time.sleep(0.01)
    os.utime(target, None)          # 文件改了，重读是合理的
    assert loop._repeated_read_note("read_file", args, 1) == ""


def test_read_after_a_few_rounds_is_allowed(tmp_path):
    target = tmp_path / "c.py"
    target.write_text("x = 1\n", encoding="utf-8")
    loop = _bare_loop()
    args = {"path": str(target)}

    loop._remember_read(args, 0, "x = 1\n")
    # 第 3 轮之后放行，避免把"确实需要重看"的路堵死
    assert loop._repeated_read_note("read_file", args, 4) == ""


def test_other_tools_are_not_affected(tmp_path):
    loop = _bare_loop()
    assert loop._repeated_read_note("bash", {"command": "ls"}, 0) == ""


# ── 验证结论过期 ──
@pytest.mark.asyncio
async def test_stale_lint_failure_is_cleared_once_fixed(tmp_path):
    target = tmp_path / "fixed.py"
    target.write_text("x = 1\n", encoding="utf-8")

    loop = AgenticLoop.__new__(AgenticLoop)
    loop._last_verification_note = f"❌ 写入 {target} 后自动检查未通过\n[lint] 行 409: F821"
    loop._auto_lint_check = AsyncMock(return_value="")       # 这次检查通过
    loop._auto_save_progress = lambda *a, **k: None

    await loop._record_file_change("edit_file", {"path": str(target)}, {}, 0, "")

    assert loop._last_verification_note == "", "修好之后不能还留着旧的失败结论"


@pytest.mark.asyncio
async def test_new_lint_failure_is_recorded(tmp_path):
    target = tmp_path / "broken.py"
    target.write_text("x = 1\n", encoding="utf-8")

    loop = AgenticLoop.__new__(AgenticLoop)
    loop._last_verification_note = ""
    loop._auto_lint_check = AsyncMock(return_value="[lint] 行 1: F821")
    loop._auto_save_progress = lambda *a, **k: None

    await loop._record_file_change("edit_file", {"path": str(target)}, {}, 0, "")

    assert "未通过" in loop._last_verification_note


# ── 费用：任意模型都要有数 ──
def test_exact_match_has_no_tilde():
    assert estimate_cost("deepseek", "deepseek-chat", 1000, 1000) == "¥0.003"


def test_version_variant_falls_back_to_prefix():
    # glm-4.7-flash 没在表里，但 glm-4-flash 在
    cost = estimate_cost("zhipu", "glm-4.7-flash", 1_000_000, 0)
    assert cost.startswith("~¥"), "非精确匹配要标成估算"
    assert "0.100" in cost


def test_unknown_provider_still_gets_an_estimate():
    cost = estimate_cost("brand-new-vendor", "whatever-model", 1000, 1000)
    assert cost.startswith("~¥"), "认不出 provider 也要给兜底数，不能空白"


def test_ollama_is_free():
    assert estimate_cost("ollama", "llama3", 1000, 1000) == "免费"


def test_custom_pricing_file_overrides(tmp_path, monkeypatch):
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps({"myvendor": {"m1": {"input_cny": 0.01, "output_cny": 0.02}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GANGGE_PRICING_FILE", str(path))
    reset_custom_cache()
    try:
        assert estimate_cost("myvendor", "m1", 1000, 0) == "¥0.010"
    finally:
        reset_custom_cache()
        monkeypatch.delenv("GANGGE_PRICING_FILE", raising=False)


def test_estimate_exposes_source_for_ui():
    exact = estimate("deepseek", "deepseek-chat", 1000, 1000)
    assert exact.source == "exact" and not exact.is_estimate

    fallback = estimate("nobody", "nothing", 1000, 1000)
    assert fallback.source == "fallback" and fallback.is_estimate


# ── 缓存命中 ──
def test_cached_input_is_billed_cheaper():
    hit = estimate("deepseek", "deepseek-chat", 1000, 0, cached_tokens=1000)
    miss = estimate("deepseek", "deepseek-chat", 1000, 0)
    assert hit.amount < miss.amount, "命中缓存必须比没命中便宜"
    assert hit.amount == pytest.approx(miss.amount * 0.02, rel=0.01), "DeepSeek 缓存价约 1/50"


def test_anthropic_cache_ratio_is_about_a_tenth():
    hit = estimate("anthropic", "claude-sonnet-4-20250514", 1000, 0, cached_tokens=1000)
    miss = estimate("anthropic", "claude-sonnet-4-20250514", 1000, 0)
    assert hit.amount == pytest.approx(miss.amount * 0.1, rel=0.05)


def test_cached_tokens_are_clamped_to_input():
    # 报上来 cached > input 时按 input 算，不能出现负的未命中量
    e = estimate("deepseek", "deepseek-chat", 100, 0, cached_tokens=99999)
    assert e.amount == pytest.approx(100 / 1000 * 0.001 * 0.02, rel=0.01)


def test_partial_cache_hit_mixes_both_prices():
    half = estimate("deepseek", "deepseek-chat", 1000, 0, cached_tokens=500)
    miss = estimate("deepseek", "deepseek-chat", 1000, 0)
    hit = estimate("deepseek", "deepseek-chat", 1000, 0, cached_tokens=1000)
    assert hit.amount < half.amount < miss.amount


def test_usage_totals_keeps_cached_count():
    from gangge.layer3_agent.loop import usage_totals

    out = usage_totals({"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 60})
    assert out == {"input": 100, "output": 10, "cached": 60}


def test_user_supplied_price_roundtrip(tmp_path, monkeypatch):
    """用户在设置面板里填的价格要落盘并立刻生效。"""
    from gangge.pricing import set_model_price

    monkeypatch.setenv("GANGGE_PRICING_FILE", str(tmp_path / "pricing.json"))
    reset_custom_cache()
    try:
        set_model_price("myvendor", "m1", 0.01, 0.02, 0.001)
        assert estimate_cost("myvendor", "m1", 1000, 0) == "¥0.010"
        assert estimate_cost("myvendor", "m1", 1000, 0, 1000) == "¥0.001", "缓存价要按填的值生效"
    finally:
        reset_custom_cache()
        monkeypatch.delenv("GANGGE_PRICING_FILE", raising=False)


def test_remove_custom_price_falls_back_to_builtin(tmp_path, monkeypatch):
    """填错价要能退回去：删掉自定义项后重新走内置价表。"""
    import json

    from gangge.pricing import (
        PRICING_TABLE,
        builtin_price,
        remove_model_price,
        set_model_price,
    )

    path = tmp_path / "pricing.json"
    monkeypatch.setenv("GANGGE_PRICING_FILE", str(path))
    reset_custom_cache()
    try:
        set_model_price("deepseek", "deepseek-chat", 9.9, 9.9)
        assert estimate_cost("deepseek", "deepseek-chat", 1000, 0) == "¥9.900"

        remove_model_price("deepseek", "deepseek-chat")
        builtin = PRICING_TABLE["deepseek"]["deepseek-chat"]["input_cny"]
        assert estimate_cost("deepseek", "deepseek-chat", 1000, 0) == f"¥{builtin:.3f}"
        assert json.loads(path.read_text(encoding="utf-8")) == {}
        # 内置价不受自定义文件影响，任何时候都能当参照
        assert builtin_price("deepseek", "deepseek-chat")[0]["input_cny"] == builtin
    finally:
        reset_custom_cache()
        monkeypatch.delenv("GANGGE_PRICING_FILE", raising=False)
