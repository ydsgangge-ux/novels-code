"""长文案 → 分镜：一段口播稿，切成能配音、能断章的镜头。

和 ``demo_video`` 那条"给主题让模型写"的路不同，这里**一个字都不改**用户的稿子：
只负责切段、给每段起个短标题、凑成一条合法分镜。之所以要切，是因为配音是按镜头
合成的，一个镜头一句话才能让画面跟着声音走。

切法很朴素，但都是踩过的坑：

* 先按空行分段——写稿的人用空行分段，比任何正则都准；
* 段里句子太多或太长，再按句号/问号/分号切成几个镜头：一个镜头念太多句，
  配音听感会散，画面也跟不上；
* 太短的碎句并回上一段，否则会出现"嗯。"这种一秒闪过的镜头。
"""

from __future__ import annotations

import re

from gangge.layer4_tools.storyboard.schema import Scene, Storyboard

#: 单镜头台词上限。中文 TTS 约 4.5 字/秒，110 字 ≈ 24 秒，是"一口气讲完"的上限。
MAX_CHARS = 110
#: 一个镜头最多讲几句。四句中文口播约 15~20 秒，再多画面就跟不上声音了。
MAX_SENTENCES = 4
#: 短于这个长度的段会并进上一段，避免"嗯。"这种一秒钟就跳过的碎镜头。
MIN_CHARS = 8
#: 章节标题的字数上限（HUD 里一行放得下的量）。
TITLE_CHARS = 18

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"[^。！？!?；;]*[。！？!?；;]?")


def split_paragraphs(text: str) -> list[str]:
    """按空行分段；没有空行就按单换行分。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    blocks = [b.strip() for b in _PARAGRAPH.split(text) if b.strip()]
    if len(blocks) == 1:
        blocks = [b.strip() for b in text.split("\n") if b.strip()]
    return blocks


def _sentences(block: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(block) if s.strip()]


def _pack(block: str, limit: int, max_sentences: int = MAX_SENTENCES) -> list[str]:
    """把一段按句子装进"不超过 limit 字、不超过 max_sentences 句"的块。"""
    out: list[str] = []
    buf = ""
    count = 0
    for sentence in _sentences(block):
        if buf and (len(buf) + len(sentence) > limit or count >= max_sentences):
            out.append(buf)
            buf = ""
            count = 0
        # 单句本身就超长：只能硬切，否则这个镜头永远超时
        while len(sentence) > limit:
            out.append(sentence[:limit])
            sentence = sentence[limit:]
            count = 0
        buf += sentence
        count += 1
    if buf:
        out.append(buf)
    return out


def chunk_script(
    text: str,
    *,
    limit: int = MAX_CHARS,
    minimum: int = MIN_CHARS,
    max_sentences: int = MAX_SENTENCES,
) -> list[str]:
    """文案 → 若干段台词（顺序不变、内容不改）。"""
    chunks: list[str] = []
    for block in split_paragraphs(text):
        for piece in _pack(block, limit, max_sentences):
            piece = piece.strip()
            if not piece:
                continue
            # 碎句就并进上一段，别让画面一秒钟就跳
            if chunks and len(piece) < minimum and len(chunks[-1]) + len(piece) <= limit:
                chunks[-1] = f"{chunks[-1]}{piece}"
            else:
                chunks.append(piece)
    return chunks


def heading_of(chunk: str, limit: int = TITLE_CHARS) -> str:
    """取一句能在 HUD 里当小标题的话：优先在标点处断开。"""
    text = " ".join((chunk or "").split())
    if not text:
        return ""
    for sep in "。！？；，,、":
        cut = text.find(sep)
        if 2 <= cut <= limit:
            return text[:cut]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def storyboard_from_script(
    text: str, *, title: str = "", style: str = "dark"
) -> Storyboard:
    """一段文案 → 一条分镜：一段一个镜头，旁白就是原文。"""
    chunks = chunk_script(text)
    if not chunks:
        raise ValueError("文案是空的")
    scenes = [
        Scene(
            template="title",
            title=heading_of(chunk),
            text="",
            voiceover=chunk,
        )
        for chunk in chunks
    ]
    return Storyboard(
        title=title or heading_of(chunks[0]) or "口播视频",
        style=style,
        scenes=scenes,
    )
