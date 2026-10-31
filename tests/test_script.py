"""现成文案 → 分镜：切段、起标题，全程离线。"""

from __future__ import annotations

import re

import pytest

from gangge.layer4_tools.storyboard.script import (
    MAX_CHARS,
    chunk_script,
    heading_of,
    split_paragraphs,
    storyboard_from_script,
)

COPY = """今天所有人的 AI 都是同一个 AI。可产出的东西天差地别。

没有记录，你得到的永远是通用建议：「保持规律作息」「建议复盘目标」。有记录，它才能说：「你过去 6 个月启动了 7 个计划。」

第二种回答，模型编不出来。"""


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_split_paragraphs_by_blank_line():
    blocks = split_paragraphs(COPY)
    assert len(blocks) == 3
    assert blocks[0].startswith("今天所有人的 AI")


def test_split_paragraphs_falls_back_to_lines():
    assert split_paragraphs("甲\n乙\n丙") == ["甲", "乙", "丙"]


def test_chunks_keep_every_character():
    """切法可以随便调，但一个字都不能丢——这是"原文照念"的底线。"""
    chunks = chunk_script(COPY)
    assert len(chunks) >= 3
    assert _flat("".join(chunks)) == _flat(COPY)


def test_long_paragraph_is_split_on_punctuation():
    block = "这是第一句话。" * 12
    chunks = chunk_script(block)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHARS for c in chunks)
    assert _flat("".join(chunks)) == _flat(block)


def test_short_piece_merges_into_previous():
    chunks = chunk_script("这是一个足够长的段落，用来当上一段。\n嗯。")
    assert len(chunks) == 1
    assert chunks[0].endswith("嗯。")


def test_heading_prefers_punctuation_boundary():
    assert heading_of("今天所有人的 AI 都是同一个 AI。可产出的东西天差地别。") == (
        "今天所有人的 AI 都是同一个 AI"
    )
    assert heading_of("短句") == "短句"
    long_line = "这一句话特别特别长没有标点可以断开只能硬切掉一部分内容"
    head = heading_of(long_line)
    assert head.endswith("…") and len(head) <= 18


def test_storyboard_from_script():
    sb = storyboard_from_script(COPY, style="neon")
    assert sb.style == "neon"
    assert len(sb.scenes) == 3
    assert sb.title == "今天所有人的 AI 都是同一个 AI"
    # 每个镜头都得有旁白，否则配音会跳过这一段
    assert all(scene.voiceover.strip() for scene in sb.scenes)
    assert sb.timeline(), "分镜必须能排出时间轴"


def test_empty_script_is_rejected():
    with pytest.raises(ValueError):
        storyboard_from_script("   \n\n  ")
