"""网址 → 分镜：抓一个页面，抽出它到底在说什么，再让模型写成 20-45 秒分镜。

分两步，边界很清楚：

* :func:`fetch_page` 只用标准库（``urllib`` + ``html.parser``）抓取和清洗，不依赖
  模型、不依赖浏览器，拿到的是标题、摘要、小标题和正文节选——一个
  :class:`PageDigest`。
* :func:`draft_from_url` 把这份摘要塞进提示词，让主模型输出跟 ``demo_video``
  完全同构的分镜 JSON，于是后面的配音、渲染、合成一行都不用改。

抓取失败一律抛 :class:`SourceError`，让调用方决定是降级还是退出——不在库里
吞异常，否则"为什么视频只有一句空话"会很难查。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from gangge.layer4_tools.storyboard.schema import Storyboard

#: 有些站点对没有 UA 的请求直接返回 403，这里伪装成普通浏览器。
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
#: 抓多少字节就停——分镜只需要卖点，不需要把整站搬下来。
MAX_BYTES = 600_000
#: 正文节选上限（字符）。够模型抓到 3-5 个卖点，又不会把上下文撑爆。
MAX_CHARS = 6000

#: 这些标签里的字对分镜没有意义（导航、页脚、脚本），还会把正文淹掉。
_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template",
              "nav", "header", "footer", "aside", "form", "button", "select",
              "dialog", "menu"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2",
               "h3", "h4", "main", "blockquote", "pre", "td"}
_HEADING_TAGS = {"h1", "h2", "h3"}
#: 正文容器的线索。命中就先只用它的文字，比全页正文干净得多
#: （GitHub 那种站，导航能占掉一半字符）。
_MAIN_HINTS = ("readme", "markdown-body", "article-body", "post-content",
               "entry-content", "doc-content", "main-content")


class SourceError(RuntimeError):
    """页面抓不下来，或者内容里没有任何可用于写分镜的东西。"""


@dataclass(frozen=True)
class PageDigest:
    """一个页面的"摘要"——写分镜真正需要的全部素材。"""

    url: str
    title: str
    description: str = ""
    headings: tuple[str, ...] = ()
    text: str = ""

    def slug(self) -> str:
        """输出目录名：优先用域名 + 路径最后一段。"""
        parts = [p for p in self.url.split("://", 1)[-1].split("/") if p]
        host = parts[0].replace("www.", "") if parts else "page"
        tail = parts[-1] if len(parts) > 1 else ""
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", f"{host}-{tail}").strip("-")
        return (cleaned[:40] or "page").lower()

    def prompt_block(self) -> str:
        lines = [f"页面地址：{self.url}"]
        if self.title:
            lines.append(f"页面标题：{self.title}")
        if self.description:
            lines.append(f"页面摘要：{self.description}")
        if self.headings:
            lines.append("页面小标题：")
            lines.extend(f"- {h}" for h in self.headings)
        if self.text:
            lines.append("正文节选：")
            lines.append(self.text)
        return "\n".join(lines)


class _DigestParser(HTMLParser):
    """只关心四样东西：title、meta description、小标题、正文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self._chunks: list[str] = []      # 全页正文（兜底）
        self._main: list[str] = []        # main/article 里的字
        self._prose: list[str] = []       # readme / markdown-body 这类正文容器
        self._open: list[tuple[str, str]] = []   # (tag, kind)，kind ∈ main/prose
        self._skip = 0
        self._in_title = False
        self._heading: str | None = None

    # -- helpers ---------------------------------------------------

    @staticmethod
    def _kind(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        """这个标签是不是正文容器；空串表示不是。"""
        if tag in ("article", "main"):
            return "main"
        data = {k.lower(): (v or "") for k, v in attrs}
        ident = f"{data.get('id', '')} {data.get('class', '')}".lower()
        return "prose" if any(h in ident for h in _MAIN_HINTS) else ""

    def _in_prose(self) -> bool:
        return any(kind == "prose" for _, kind in self._open)

    def _push(self, text: str) -> None:
        text = " ".join(text.split())
        if not text:
            return
        self._chunks.append(text)
        if self._open:
            (self._prose if self._in_prose() else self._main).append(text)

    def _br(self) -> None:
        self._chunks.append("\n")
        if self._open:
            (self._prose if self._in_prose() else self._main).append("\n")

    # -- HTMLParser ------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            data = {k.lower(): (v or "") for k, v in attrs}
            key = data.get("name", "").lower() or data.get("property", "").lower()
            if key in ("description", "og:description") and not self.description:
                self.description = " ".join(data.get("content", "").split())
        elif tag in _HEADING_TAGS:
            self._heading = ""
        elif tag in _BLOCK_TAGS:
            self._br()
        kind = self._kind(tag, attrs)
        if kind:
            self._open.append((tag, kind))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in _HEADING_TAGS and self._heading is not None:
            heading = " ".join(self._heading.split())
            if 1 < len(heading) <= 40:
                self.headings.append(heading)
            self._heading = None
            self._br()
        elif tag in _BLOCK_TAGS:
            self._br()
        if self._open and tag == self._open[-1][0]:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title = " ".join((self.title + " " + data).split())
            return
        if self._heading is not None:
            self._heading += data
        self._push(data)

    # -- result ----------------------------------------------------

    @staticmethod
    def _join(chunks: list[str], limit: int) -> str:
        raw = " ".join(chunks)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        kept: list[str] = []
        for line in lines:
            if len(line) < 2:
                continue
            if kept and kept[-1] == line:      # 导航栏会把同一个词重复许多遍
                continue
            kept.append(line)
        return "\n".join(kept)[:limit]

    def text(self, limit: int) -> str:
        # 优先级：readme 类正文 > main/article > 全页。太短说明抓错了（比如只有
        # 一句署名），退回上一层。
        for chunks in (self._prose, self._main, self._chunks):
            joined = self._join(chunks, limit)
            if len(joined) >= 200:
                return joined
        return self._join(self._chunks, limit)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise SourceError("网址是空的")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise SourceError(f"只支持 http/https：{url}")
    return url


#: GitHub 仓库页：正文（README）是前端渲染的，服务端 HTML 里只有文件列表。
_GITHUB_RE = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.I)
_FENCE_RE = re.compile(r"```.*?```", re.S)
_BADGE_RE = re.compile(r"^\s*\[?!\[.*?$", re.M)
_IMG_RE = re.compile(r"<img[^>]*>", re.I)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG_RE = re.compile(r"</?[a-zA-Z][^<>]*>")
_HEAD_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.M)


def _strip_markdown(text: str) -> tuple[str, list[str]]:
    """README 的 markdown → 纯文本 + 小标题。只做到"模型看得懂"这一层。"""
    text = _FENCE_RE.sub("\n", text)          # 代码块对写分镜没用，还特别长
    text = _BADGE_RE.sub("", text)            # 徽章是图，留着只是一堆括号
    text = _IMG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    headings = [" ".join(m.group(2).split()) for m in _HEAD_RE.finditer(text)]
    text = _HEAD_RE.sub(r"\2", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    kept = [line for line in lines if len(line) > 1]
    return "\n".join(kept), [h for h in headings if 1 < len(h) <= 40]


def _github_slug(match: re.Match[str]) -> str:
    owner, repo = match.group(1), match.group(2).removesuffix(".git")
    return f"{owner}/{repo}"


def _github_readme(match: re.Match[str], timeout: float) -> tuple[str, list[str]]:
    """拿 README 原文：比解析仓库页干净得多（服务端那版只有文件列表）。

    三个来源依次试，因为它们在各地可达性不一样：raw 最通用、API 能自动认
    README 的文件名（README.rst 也行）、jsDelivr 在某些网络下是唯一通的。
    """
    slug = _github_slug(match)
    sources_ = (
        (f"https://raw.githubusercontent.com/{slug}/HEAD/README.md", None),
        (f"https://api.github.com/repos/{slug}/readme", "application/vnd.github.raw"),
        (f"https://cdn.jsdelivr.net/gh/{slug}@HEAD/README.md", None),
    )
    for url, accept in sources_:
        try:
            body = _http_get(url, timeout, accept=accept)
        except SourceError:
            continue
        text, headings = _strip_markdown(body.decode("utf-8", "replace"))
        if len(text) > 200:
            return text, headings
    return "", []


_CHARSET_RE = re.compile(rb"charset=[\"']?([\w-]+)", re.I)


def _decode(raw: bytes) -> str:
    """按 meta charset 解码；猜不到就依次试 utf-8 / gb18030。"""
    found = _CHARSET_RE.search(raw[:4096])
    charset = (found.group(1).decode("ascii", "ignore") if found else "") or "utf-8"
    for name in (charset, "utf-8", "gb18030"):
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _pretty_github_title(title: str) -> str:
    """``GitHub - owner/repo: 一句话 · GitHub`` → ``owner/repo: 一句话``。"""
    text = re.sub(r"^GitHub\s*-\s*", "", title or "")
    return re.sub(r"\s*·\s*GitHub$", "", text).strip()


def _http_get(url: str, timeout: float, *, accept: str | None = None) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": accept or "text/html,text/plain,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 不声明 gzip：自己解压是白写的复杂度，分镜不需要省这点流量
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response:
            return response.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        raise SourceError(f"抓取失败（HTTP {e.code}）：{url}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"抓取失败（网络）：{e.reason}") from e
    except OSError as e:
        raise SourceError(f"抓取失败：{e}") from e


def _ssl_context() -> Any:
    """用 certifi 的根证书建上下文。

    很多 Windows 的 Python 装完找不到系统根证书（urllib 会直接
    ``CERTIFICATE_VERIFY_FAILED``），而项目本来就要装 httpx，certifi 一定在。
    """
    try:
        import ssl

        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - 没有 certifi 就退回系统默认
        return None


def fetch_page(url: str, *, timeout: float = 15.0, max_chars: int = MAX_CHARS) -> PageDigest:
    """抓取 ``url`` 并抽成 :class:`PageDigest`；失败抛 :class:`SourceError`。"""
    target = normalize_url(url)
    raw = _http_get(target, timeout)

    parser = _DigestParser()
    try:
        parser.feed(_decode(raw))
        parser.close()
    except Exception as e:  # noqa: BLE001 - 畸形 HTML 不该让整条链路失败
        raise SourceError(f"页面解析失败：{e}") from e

    digest = PageDigest(
        url=target,
        title=parser.title,
        description=parser.description,
        headings=tuple(dict.fromkeys(parser.headings))[:12],
        text=parser.text(max_chars),
    )

    # GitHub 仓库页的 README 是前端渲染的，服务端只给文件列表。直接取 raw
    # README 的收益极大：拿到的是"这个项目是什么"本身。
    github = _GITHUB_RE.match(target)
    if github:
        body, headings = _github_readme(github, timeout)
        if body:
            digest = PageDigest(
                url=target,
                title=_pretty_github_title(digest.title) or _github_slug(github),
                description=digest.description,
                headings=tuple(headings) or digest.headings,
                text=body[:max_chars],
            )
    if not (digest.title or digest.description or digest.text):
        raise SourceError(
            f"这个页面没抽出可用的内容（可能需要登录或是纯前端渲染）：{target}"
        )
    return digest


def _draft_prompt(page: PageDigest) -> str:
    return f"""你是短视频分镜师。下面是一个网页抓下来的内容，把它写成一条 20-45 秒的介绍短视频分镜，只输出 JSON。

JSON 结构：
{{"title": "视频标题", "scenes": [{{...}}, ...]}}

template 只能取以下五个值，字段含义如下：
- "title"   标题卡：title（主标题 ≤14字）、text（副标题 ≤24字）
- "bullets" 要点：  title（≤16字）、items（2-4 条，每条 ≤16字）
- "flow"    流程：  title（≤16字）、nodes（2-4 个，每个 ≤8字）
- "quote"   金句：  text（≤36字）、title（出处或作者）
- "outro"   结尾：  title（≤14字）、text（≤24字）、items（第一个元素是行动号召 ≤10字）

规则：
1. 共 4-6 个镜头。第一个必须是 title，最后一个必须是 outro，中间用 bullets / flow / quote。
2. 每个镜头必须有 voiceover：口语化讲解，25-45 个汉字，承接上一镜头的语义，念出来要通顺。
3. 只讲这个页面真正写出来的东西：它解决什么问题、怎么用、有哪些特点。
   不要编造文档里没有的功能、数字或评价。
4. 文案要具体，避免「赋能」「抓手」这类空话。
5. 只输出 JSON 本身，不要解释，不要 markdown 代码块。

{page.prompt_block()}"""


def _loads(text: str) -> Any:
    """从可能带说明文字或 ``` 围栏的回答里取出 JSON。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出里找不到合法 JSON")


async def draft_from_url(
    url: str,
    llm: Any,
    *,
    timeout: float = 15.0,
    max_chars: int = MAX_CHARS,
) -> Storyboard:
    """抓页面 → 让模型写分镜 → 返回可直接渲染的 :class:`Storyboard`。"""
    if llm is None:
        raise SourceError("当前没有可用的主模型，无法从网址写分镜")

    # llm 相关类型在这里才导入：只抓页面时不该因为没配模型而导入失败。
    from gangge.layer5_llm.base import ContentBlock, ContentType, Message, Role

    page = fetch_page(url, timeout=timeout, max_chars=max_chars)
    prompt = _draft_prompt(page)
    last_error = ""
    for attempt in range(2):
        ask = prompt if attempt == 0 else (
            f"{prompt}\n\n上一次输出无法解析（{last_error}），请只输出合法 JSON。"
        )
        message = Message(
            role=Role.USER,
            content=[ContentBlock(type=ContentType.TEXT, text=ask)],
        )
        response = await llm.chat(messages=[message])
        try:
            storyboard = Storyboard.from_dict(_loads(response.text))
        except ValueError as e:  # 解析/校验失败：把原因带回给模型重试一次
            last_error = str(e)
            continue
        return storyboard
    raise SourceError(f"模型两次都没有给出合法分镜：{last_error}")
